from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from .paper_journal import (
    FILLS_PATH, add_event, enum_value, flatten_orders, is_bot_entry, journal,
    linked_orders, quantity, timestamp,
)


TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
WORKING = {"new", "accepted", "pending_new", "partially_filled", "done_for_day",
           "accepted_for_bidding", "held", "pending_cancel", "pending_replace", "stopped", "suspended",
           "calculated"}


def fetch_paper_orders(trading, *, status="all"):
    from alpaca.common.enums import Sort
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = {}
    until = None
    while True:
        page = trading.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus(status), nested=True, limit=500,
            direction=Sort.DESC, until=until,
        ))
        fresh = [order for order in page if str(order.id) not in orders]
        orders.update((str(order.id), order) for order in page)
        if len(page) < 500:
            return list(orders.values())
        if not fresh:
            raise ValueError("Paper order pagination made no progress; new entries blocked")
        created = [order.created_at for order in page]
        for value in created:
            timestamp(value)
        # Overlap the boundary so equal submission times cannot silently lose orders.
        until = min(created) + timedelta(microseconds=1)


def position_context(position, orders, path=FILLS_PATH):
    linked = linked_orders(orders, path)
    by_entry = {}
    for entry_id, symbol, order in linked:
        if symbol != position.symbol:
            continue
        net = by_entry.setdefault(entry_id, Decimal(0))
        qty = quantity(getattr(order, "filled_qty", None) or 0)
        by_entry[entry_id] = net + (qty if enum_value(order.side) == "buy" else -qty)
    candidates = [entry_id for entry_id, qty in by_entry.items() if qty > 0]
    if any(qty < 0 for qty in by_entry.values()):
        raise ValueError(f"{position.symbol}: unmatched bot sales; automatic recovery blocked")
    if len(candidates) != 1:
        raise ValueError(f"{position.symbol}: cannot uniquely establish bot ownership; entries paused")
    entry_id = candidates[0]
    if quantity(position.qty) == 0 or by_entry[entry_id] != quantity(position.qty):
        raise ValueError(f"{position.symbol}: broker quantity differs from bot fills; entries paused")
    entry = next((order for order in orders if str(order.id) == entry_id and is_bot_entry(order)), None)
    if entry is None:
        raise ValueError(f"{position.symbol}: bot entry missing from broker history")
    if enum_value(entry.type) != "limit" or enum_value(entry.order_class) != "bracket":
        raise ValueError(f"{position.symbol}: unexpected bot entry type; recovery blocked")
    related = [order for parent_id, _, order in linked if parent_id == entry_id]
    return entry, related


def protection_valid(position, related, now) -> bool:
    exits = [order for order in related if enum_value(order.side) == "sell"
             and enum_value(order.status) not in TERMINAL]
    stops = [order for order in exits if enum_value(order.type) == "stop"]
    targets = [order for order in exits if enum_value(order.type) == "limit"]
    if len(exits) != 2 or len(stops) != 1 or len(targets) != 1:
        return False
    exit_ids = {str(order.id) for order in exits}
    paired = False
    for root in related:
        members = flatten_orders(getattr(root, "legs", None) or [])
        if enum_value(root.side) == "sell" and enum_value(root.order_class) == "oco":
            members = [root, *members]
        elif not (is_bot_entry(root) and enum_value(root.order_class) == "bracket"):
            continue
        if {str(member.id) for member in members} == exit_ids:
            paired = True
            break
    if not paired:
        return False
    if any(enum_value(order.status) not in {"new", "accepted", "held", "partially_filled"}
           for order in exits):
        return False
    if any(quantity(order.qty) - quantity(order.filled_qty or 0) != quantity(position.qty)
           for order in exits):
        return False
    stop, target = stops[0], targets[0]
    if not 0 < quantity(stop.stop_price) < quantity(target.limit_price):
        return False
    for order in exits:
        expiry = getattr(order, "expires_at", None)
        created = getattr(order, "created_at", None)
        if expiry is not None:
            timestamp(expiry)
            if expiry - now <= timedelta(days=5):
                return False
        else:
            if created is None:
                return False
            timestamp(created)
            if now - created >= timedelta(days=80):
                return False
    return True


def _original_prices(entry, path):
    legs = flatten_orders(getattr(entry, "legs", None) or [])
    stops = [leg for leg in legs if enum_value(leg.type) == "stop"]
    targets = [leg for leg in legs if enum_value(leg.type) == "limit"]
    if len(stops) == 1 and len(targets) == 1:
        if stops[0].stop_price is None or targets[0].limit_price is None:
            add_event("ERROR", f"{entry.symbol}: original exit prices unavailable; recovery will close exposure", path)
            return None
        try:
            stop, target = quantity(stops[0].stop_price), quantity(targets[0].limit_price)
        except ValueError:
            add_event("ERROR", f"{entry.symbol}: original exit prices invalid; recovery will close exposure", path)
            return None
    else:
        with journal(path) as database:
            row = database.execute(
                "SELECT plan_json FROM entry_plans WHERE client_order_id = ?",
                (entry.client_order_id,),
            ).fetchone()
        if row is None:
            return None
        import json

        plan = json.loads(row["plan_json"])
        stop, target = quantity(plan["stop_price"]), quantity(plan["target_price"])
    return (stop, target) if 0 < stop < target else None


def _recover_position(trading, position, entry, related, now, cfg, orders, path):
    from alpaca.common.exceptions import APIError
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest

    if not trading.get_clock().is_open:
        add_event("WARNING", f"{position.symbol}: recovery pending regular market hours", path)
        return
    with journal(path) as database:
        latest = database.execute(
            "SELECT * FROM recovery_actions WHERE entry_id = ? ORDER BY rowid DESC LIMIT 1",
            (str(entry.id),),
        ).fetchone()
    working = [order for order in related if enum_value(order.status) not in TERMINAL]
    if latest and latest["kind"] == "close" and any(
        order.client_order_id == latest["client_order_id"] for order in working
    ):
        return
    for order in working:
        if enum_value(order.status) not in WORKING:
            raise ValueError(f"{position.symbol}: unsupported broker order status; recovery blocked")
    if working:
        if any(enum_value(order.status) in {"pending_cancel", "pending_replace"} for order in working):
            return
        # Cancel one root/leg, then require a fresh broker snapshot before selling or re-protecting.
        trading.cancel_order_by_id(working[0].id)
        add_event("WARNING", f"{position.symbol}: cancellation requested before recovery", path)
        return
    action = dict(latest) if latest else None
    known = {order.client_order_id: order for order in flatten_orders(orders)}
    if action and action["state"] == "requested":
        broker_order = known.get(action["client_order_id"])
        if broker_order is None:
            try:
                broker_order = trading.get_order_by_client_id(action["client_order_id"])
            except APIError as exc:
                if exc.status_code != 404:
                    raise
        if broker_order is not None:
            if enum_value(broker_order.status) not in TERMINAL:
                return
            if enum_value(broker_order.status) == "filled":
                raise ValueError(f"{position.symbol}: recovery fill not reconciled; waiting for fresh state")
            action = None
        # A missing acknowledgement retries the persisted client ID, never a new order ID.
    elif action:
        action = None
    if action is None:
        prices = _original_prices(entry, path)
        entered_at = getattr(entry, "filled_at", None) or entry.created_at
        timestamp(entered_at)
        force_close = now - entered_at >= timedelta(days=cfg.bot_max_hold_days)
        prior_order = known.get(latest["client_order_id"]) if latest else None
        repair_failed = latest is not None and latest["kind"] == "protect" and (
            latest["state"] == "rejected"
            or (prior_order is not None and enum_value(prior_order.status) == "rejected")
        )
        kind = "close" if force_close or prices is None or repair_failed else "protect"
        action = {
            "client_order_id": f"swing-recover-{uuid4().hex}", "entry_id": str(entry.id),
            "symbol": position.symbol, "kind": kind, "created_at": timestamp(now),
            "stop_price": str(prices[0]) if prices else None,
            "target_price": str(prices[1]) if prices else None,
            "qty": str(quantity(position.qty)), "state": "requested",
        }
        with journal(path) as database:
            database.execute(
                """INSERT INTO recovery_actions
                (client_order_id, entry_id, symbol, kind, created_at, stop_price, target_price, qty, state)
                VALUES (:client_order_id, :entry_id, :symbol, :kind, :created_at,
                        :stop_price, :target_price, :qty, :state)""", action,
            )
    if quantity(action["qty"]) != quantity(position.qty):
        raise ValueError(f"{position.symbol}: quantity changed during recovery; entries paused")
    if action["kind"] == "protect":
        request = LimitOrderRequest(
            symbol=position.symbol, qty=float(quantity(position.qty)), side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, order_class=OrderClass.OCO,
            limit_price=float(action["target_price"]),
            take_profit=TakeProfitRequest(limit_price=float(action["target_price"])),
            stop_loss=StopLossRequest(stop_price=float(action["stop_price"])),
            client_order_id=action["client_order_id"],
        )
    else:
        request = MarketOrderRequest(
            symbol=position.symbol, qty=float(quantity(position.qty)),
            side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            client_order_id=action["client_order_id"],
        )
    try:
        trading.submit_order(order_data=request)
    except APIError as exc:
        if exc.status_code not in {400, 403, 422}:
            raise
        # Confirm absence before classifying a rejected request; timeouts remain unresolved.
        try:
            trading.get_order_by_client_id(action["client_order_id"])
        except APIError as lookup:
            if lookup.status_code != 404:
                raise
            with journal(path) as database:
                database.execute(
                    "UPDATE recovery_actions SET state = 'rejected' WHERE client_order_id = ?",
                    (action["client_order_id"],),
                )
        add_event("ERROR", f"{position.symbol}: {action['kind']} request rejected; will reconcile and retry safely", path)
        raise
    add_event("WARNING", f"{position.symbol}: submitted {action['kind']} recovery; awaiting broker confirmation", path)


def reconcile_paper_risk(trading, positions, orders, now, cfg, path=FILLS_PATH) -> bool:
    linked = linked_orders(orders, path)
    owned_ids = {str(order.id) for _, _, order in linked}
    all_orders = flatten_orders(orders)
    contexts = []
    # Validate every position before changing any orders; unknown/manual positions are never liquidated.
    for position in positions:
        entry, related = position_context(position, orders, path)
        if any(order.symbol == position.symbol and str(order.id) not in owned_ids
               and enum_value(order.status) not in TERMINAL for order in all_orders):
            raise ValueError(f"{position.symbol}: unrelated working orders prevent automatic recovery")
        contexts.append((position, entry, related))
    safe = True
    held_symbols = {position.symbol for position in positions}
    net_by_symbol = {}
    filled_entries = set()
    for entry_id, symbol, order in linked:
        net_by_symbol.setdefault(symbol, Decimal(0))
        qty = quantity(getattr(order, "filled_qty", None) or 0)
        if enum_value(order.side) == "buy":
            net_by_symbol[symbol] += qty
            if qty > 0:
                filled_entries.add(entry_id)
        else:
            net_by_symbol[symbol] -= qty
    if any(qty != 0 and symbol not in held_symbols for symbol, qty in net_by_symbol.items()):
        raise ValueError("Broker positions and bot fills disagree; waiting for reconciliation")
    for entry_id, symbol, order in linked:
        if (entry_id in filled_entries and symbol not in held_symbols
                and enum_value(order.side) == "sell" and enum_value(order.status) not in TERMINAL):
            if enum_value(order.status) not in {"pending_cancel", "pending_replace"}:
                trading.cancel_order_by_id(order.id)
                add_event("WARNING", f"{symbol}: canceling orphaned bot exit while flat", path)
            return False
    for entry in [order for order in orders if is_bot_entry(order)]:
        status = enum_value(entry.status)
        if status not in TERMINAL:
            timestamp(entry.created_at)
            if status in {"pending_cancel", "pending_replace"}:
                safe = False
            elif quantity(entry.filled_qty or 0) > 0 or now - entry.created_at >= timedelta(minutes=10):
                trading.cancel_order_by_id(entry.id)
                add_event("WARNING", f"{entry.symbol}: canceling partial or stale entry before reconciliation", path)
                safe = False
    for position, entry, related in contexts:
        if enum_value(entry.status) not in TERMINAL:
            safe = False
            continue
        entered_at = getattr(entry, "filled_at", None) or entry.created_at
        timestamp(entered_at)
        try:
            protected = protection_valid(position, related, now)
        except ValueError as exc:
            add_event("WARNING", f"{position.symbol}: invalid protection requires recovery: {exc}", path)
            protected = False
        if protected and now - entered_at < timedelta(days=cfg.bot_max_hold_days):
            continue
        safe = False
        _recover_position(trading, position, entry, related, now, cfg, orders, path)
    return safe
