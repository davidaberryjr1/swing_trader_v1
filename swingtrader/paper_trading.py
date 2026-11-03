import math
import runpy
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import TradingConfig
from .correlations import highest_absolute_correlation, validate_correlation_config
from .market_data import AlpacaMarketDataProvider
from .paper_journal import FILLS_PATH, record_paper_fills, save_entry_plan
from .paper_recovery import (
    TERMINAL, WORKING, fetch_paper_orders, position_context, protection_valid, reconcile_paper_risk,
)
from .scanner import TradeCandidate
from .sectors import sector_for_symbol


CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "config.py"
ENTRY_TIMEOUT = timedelta(minutes=10)
QUOTE_CLOCK_SKEW_TOLERANCE = timedelta(seconds=10)


@dataclass(frozen=True)
class PaperOrderPlan:
    symbol: str
    shares: int
    limit_price: float
    stop_price: float
    target_price: float
    risk_dollars: float


@dataclass(frozen=True)
class BotCapacity:
    slots: int
    remaining_risk: float
    symbols: frozenset[str]


def enforce_sector_exposure_limit(
    symbol: str,
    proposed_market_value: float,
    positions,
    open_orders,
    cfg: TradingConfig,
) -> None:
    limit_fraction = cfg.max_sector_exposure_fraction
    if not math.isfinite(limit_fraction) or not 0 < limit_fraction <= 1:
        raise ValueError("Maximum sector exposure fraction must be greater than 0 and at most 1")
    if not math.isfinite(proposed_market_value) or proposed_market_value <= 0:
        raise ValueError("Proposed sector market value must be positive and finite")

    target_sector = sector_for_symbol(symbol)
    exposure = 0.0
    for position in positions:
        position_symbol = getattr(position, "symbol", None)
        if not isinstance(position_symbol, str) or not position_symbol:
            raise ValueError("Cannot verify sector exposure for a position without a symbol")
        if sector_for_symbol(position_symbol) != target_sector:
            continue
        market_value = _finite_number(
            getattr(position, "market_value", None),
            f"{position_symbol}: market value unavailable for sector exposure check",
        )
        exposure += abs(market_value)

    active_buy_statuses = {
        "new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
        "done_for_day", "pending_cancel", "pending_replace", "held",
    }
    for order in open_orders:
        side = str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower()
        status = str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
        if side != "buy" or status not in active_buy_statuses:
            continue
        order_symbol = getattr(order, "symbol", None)
        if not isinstance(order_symbol, str) or not order_symbol:
            raise ValueError("Cannot verify sector exposure for a pending order without a symbol")
        if sector_for_symbol(order_symbol) != target_sector:
            continue
        quantity = _finite_number(
            getattr(order, "qty", None),
            f"{order_symbol}: pending buy quantity unavailable for sector exposure check",
        )
        filled_quantity = _finite_number(
            getattr(order, "filled_qty", 0),
            f"{order_symbol}: pending buy filled quantity unavailable",
        )
        limit_price = _finite_number(
            getattr(order, "limit_price", None),
            f"{order_symbol}: pending buy limit price unavailable for sector exposure check",
        )
        if quantity <= 0 or filled_quantity < 0 or filled_quantity > quantity or limit_price <= 0:
            raise ValueError(f"{order_symbol}: invalid pending buy values for sector exposure check")
        exposure += (quantity - filled_quantity) * limit_price

    cap = cfg.account_size * limit_fraction
    if exposure + proposed_market_value > cap:
        raise ValueError(
            f"{target_sector} sector exposure would exceed the configured "
            f"${cap:,.2f} cap (current exposure ${exposure:,.2f})"
        )


def enforce_correlation_limit(
    symbol: str,
    peer_symbols: set[str],
    histories,
    cfg: TradingConfig,
) -> None:
    validate_correlation_config(cfg)
    peers = sorted(peer_symbols - {symbol})
    if not peers:
        return
    required_symbols = [symbol, *peers]
    missing = [item for item in required_symbols if item not in histories or histories[item].empty]
    if missing:
        raise ValueError(
            "Cannot verify correlation with open paper exposure; missing daily bars for "
            + ", ".join(missing)
        )
    if any("Close" not in histories[item].columns for item in required_symbols):
        raise ValueError("Cannot verify correlation; daily bars are missing Close prices")

    correlation = highest_absolute_correlation(
        histories[symbol]["Close"].pct_change(),
        {
            peer: histories[peer]["Close"].pct_change()
            for peer in peers
        },
        window=cfg.correlation_lookback_sessions,
        minimum_observations=cfg.correlation_min_observations,
    )
    if (
        correlation is not None
        and correlation[1] >= cfg.correlation_absolute_threshold
    ):
        raise ValueError(
            f"{symbol} is too correlated with open {correlation[0]} exposure "
            f"({correlation[1]:.2f} absolute correlation over "
            f"{correlation[2]} shared sessions)"
        )


def fetch_correlation_histories(
    symbols: set[str],
    key: str,
    secret: str,
    cfg: TradingConfig,
    *,
    now: datetime | None = None,
):
    if not symbols:
        return {}
    local_now = now or datetime.now(ZoneInfo("America/New_York"))
    if local_now.tzinfo is None:
        raise ValueError("Correlation history end time must include a timezone")
    end_day = local_now.astimezone(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    start_day = end_day - timedelta(days=cfg.correlation_lookback_sessions * 2 + 20)
    provider = AlpacaMarketDataProvider(key, secret)
    return provider.get_bars(
        sorted(symbols),
        start=start_day.isoformat(),
        end=end_day.isoformat(),
    )


def _finite_number(value, error_message: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(error_message) from exc
    if not math.isfinite(number):
        raise ValueError(error_message)
    return number


def check_bot_daily_halt(account, cfg: TradingConfig, now: datetime, path: Path = FILLS_PATH) -> None:
    balance = min(float(account.equity), cfg.account_size)
    if not math.isfinite(balance) or balance <= 0 or not 0 < cfg.daily_loss_fraction < 1:
        raise ValueError("Invalid paper bot daily loss limit or balance")
    trading_day = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE IF NOT EXISTS daily_halts (trading_day TEXT PRIMARY KEY)")
        if float(account.last_equity) - float(account.equity) >= balance * cfg.daily_loss_fraction:
            database.execute("INSERT OR IGNORE INTO daily_halts VALUES (?)", (trading_day,))
        database.commit()
        if database.execute("SELECT 1 FROM daily_halts WHERE trading_day = ?", (trading_day,)).fetchone():
            raise ValueError("Daily paper loss limit reached; no new entries until next trading day")


def bot_capacity(account, positions, orders, cfg: TradingConfig, now: datetime) -> BotCapacity:
    if cfg.max_open_positions < 1 or not 0 < cfg.daily_loss_fraction < 1 or not 0 < cfg.max_combined_risk_fraction < 1:
        raise ValueError("Invalid paper bot risk limits")
    balance = min(float(account.equity), cfg.account_size)
    if not math.isfinite(balance) or balance <= 0:
        raise ValueError("Invalid paper account balance")
    if float(account.last_equity) - float(account.equity) >= balance * cfg.daily_loss_fraction:
        raise ValueError("Daily paper loss limit reached; no new entries")

    active = WORKING - {"pending_cancel", "pending_replace"}
    symbols = set()
    planned_risk = 0.0
    entries = [order for order in orders if str(order.client_order_id or "").startswith("swing-paper-")]
    if any(getattr(order.status, "value", order.status) in {"pending_cancel", "pending_replace"}
           for order in entries):
        raise ValueError("Paper order change pending; verify broker status before new entries")
    for position in positions:
        _, related = position_context(position, orders)
        if not protection_valid(position, related, now):
            raise ValueError(f"{position.symbol}: paper exits unverified; manual review required")
        stop = next(leg for leg in related if getattr(leg.type, "value", leg.type) == "stop"
                    and getattr(leg.status, "value", leg.status) not in TERMINAL)
        planned_risk += max(0.0, float(position.avg_entry_price) - float(stop.stop_price)) * float(position.qty)
        symbols.add(position.symbol)

    for order in orders:
        status = getattr(order.status, "value", order.status)
        if status not in active or getattr(order.side, "value", order.side) != "buy":
            continue
        if order not in entries or order.symbol in symbols or float(order.filled_qty or 0) > 0:
            raise ValueError(f"{order.symbol}: unverified pending paper entry; manual review required")
        if order.limit_price is None or order.legs is None:
            raise ValueError(f"{order.symbol}: pending entry has no verified stop")
        stops = [leg for leg in order.legs if getattr(leg.type, "value", leg.type) == "stop"]
        if len(stops) != 1 or stops[0].stop_price is None:
            raise ValueError(f"{order.symbol}: pending entry has no verified stop")
        symbols.add(order.symbol)
        planned_risk += max(0.0, float(order.limit_price) - float(stops[0].stop_price)) * float(order.qty)

    return BotCapacity(cfg.max_open_positions - len(symbols),
                       balance * cfg.max_combined_risk_fraction - planned_risk, frozenset(symbols))


def _cent(price: float, rounding: str) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=rounding))


def load_paper_credentials(path: Path = CREDENTIALS_PATH) -> tuple[str, str]:
    if not path.is_file():
        raise ValueError("Create config.py from config.example.py and add Alpaca paper credentials")
    settings = runpy.run_path(str(path))
    base_url = settings.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    if not isinstance(base_url, str) or base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        raise ValueError("Only the Alpaca paper endpoint is supported")
    key = settings.get("APCA_API_KEY_ID")
    secret = settings.get("APCA_API_SECRET_KEY")
    if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
        raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in config.py to Alpaca paper credentials")
    return key, secret


def paper_bot_snapshot():
    from alpaca.trading.client import TradingClient

    key, secret = load_paper_credentials()
    trading = TradingClient(key, secret, paper=True)
    orders = fetch_paper_orders(trading)
    return trading, orders


def bot_attempted_today(orders, symbol: str, now: datetime) -> bool:
    trading_day = now.astimezone(ZoneInfo("America/New_York")).date()
    for order in orders:
        if order.symbol != symbol or not str(order.client_order_id or "").startswith("swing-paper-"):
            continue
        if order.created_at is None or order.created_at.tzinfo is None:
            raise ValueError(f"{symbol}: bot order timestamp missing; manual review required")
        if order.created_at.astimezone(ZoneInfo("America/New_York")).date() == trading_day:
            return True
    return False


def review_paper_orders(trading, orders, now: datetime, *, cancel_stale: bool = False) -> list[str]:
    messages = []
    for order in orders:
        if not order.client_order_id.startswith("swing-paper-"):
            continue
        status = getattr(order.status, "value", order.status)
        side = getattr(order.side, "value", order.side)
        order_type = getattr(order.type, "value", order.type)
        order_class = getattr(order.order_class, "value", order.order_class)
        if side != "buy" or order_type != "limit" or order_class != "bracket":
            messages.append(f"{order.symbol} {order.id}: unexpected bot order; manual review required")
            continue

        filled_qty = Decimal(str(order.filled_qty or 0))
        if status == "filled":
            legs = order.legs or []
            leg_types = {getattr(leg.type, "value", leg.type) for leg in legs}
            if len(legs) < 2 or not {"limit", "stop"}.issubset(leg_types):
                messages.append(f"{order.symbol} {order.id}: filled but exits unverified; manual review required")
            else:
                messages.append(f"{order.symbol} {order.id}: filled; check bracket exit statuses in Alpaca")
        elif filled_qty > 0 or status == "partially_filled":
            messages.append(f"{order.symbol} {order.id}: partial fill; manual review required")
        elif status in {"new", "accepted", "pending_new"}:
            if order.created_at is None or order.created_at.tzinfo is None:
                messages.append(f"{order.symbol} {order.id}: missing order timestamp; manual review required")
            elif now - order.created_at >= ENTRY_TIMEOUT:
                if cancel_stale:
                    trading.cancel_order_by_id(order.id)
                    messages.append(f"{order.symbol} {order.id}: entry cancellation requested; verify in Alpaca")
                else:
                    messages.append(f"{order.symbol} {order.id}: stale unfilled entry; run --paper-reconcile to request cancellation")
            else:
                messages.append(f"{order.symbol} {order.id}: waiting for entry fill")
        else:
            messages.append(f"{order.symbol} {order.id}: {status}; review in Alpaca")
    return messages


def report_paper_status(*, cancel_stale: bool = False) -> None:
    trading, orders = paper_bot_snapshot()
    positions = trading.get_all_positions()
    print("\n## Alpaca Paper Status\n")
    for position in positions:
        print(f"Open position: {position.symbol} ({position.qty} shares)")
    for message in review_paper_orders(trading, orders, datetime.now(timezone.utc), cancel_stale=cancel_stale):
        print(message)
    if not positions and not any(order.client_order_id.startswith("swing-paper-") for order in orders):
        print("No open positions or bot orders found in the recent Alpaca order history.")


def plan_paper_order(candidate: TradeCandidate, bid: float, ask: float, cfg: TradingConfig) -> PaperOrderPlan:
    if not 0 <= cfg.estimated_exit_cost_fraction < 1 or cfg.target_percent <= 0:
        raise ValueError("Invalid paper target or exit cost allowance")
    if not all(math.isfinite(value) for value in (bid, ask, candidate.entry, candidate.stop)):
        raise ValueError("Invalid price or quote")
    if bid <= 0 or ask < bid:
        raise ValueError("Invalid bid/ask quote")
    if (ask - bid) / ((ask + bid) / 2) > cfg.max_spread_fraction:
        raise ValueError("Quote spread exceeds configured maximum")
    if candidate.entry <= 0 or abs(ask / candidate.entry - 1) > cfg.max_price_drift_fraction:
        raise ValueError("Ask drifted beyond configured maximum from scan price; rescan")

    limit_price = _cent(ask, ROUND_CEILING)
    stop_price = _cent(ask - (candidate.entry - candidate.stop), ROUND_FLOOR)
    if not cfg.minimum_price <= limit_price <= cfg.maximum_price or stop_price <= 0 or stop_price >= bid:
        raise ValueError("Price or stop outside safe range")
    risk_per_share = limit_price - stop_price
    shares = min(
        math.floor(cfg.account_size * cfg.risk_fraction / risk_per_share),
        math.floor(cfg.account_size * cfg.max_position_fraction / limit_price),
    )
    if shares < 1:
        raise ValueError("No whole shares fit the configured risk and position limits")
    target_price = _cent(
        limit_price * (1 + cfg.target_percent / 100) / (1 - cfg.estimated_exit_cost_fraction),
        ROUND_CEILING,
    )
    return PaperOrderPlan(candidate.symbol, shares, limit_price, stop_price, target_price, shares * risk_per_share)


def submit_paper_candidate(candidate: TradeCandidate, cfg: TradingConfig, *, execute: bool = False,
                           bot_mode: bool = False) -> PaperOrderPlan:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

    validate_correlation_config(cfg)
    key, secret = load_paper_credentials()

    trading = TradingClient(key, secret, paper=True)
    if not trading.get_clock().is_open:
        raise ValueError("Paper orders are limited to regular market hours")
    account = trading.get_account()
    if account.trading_blocked:
        raise ValueError("Paper account is blocked from trading")
    positions = trading.get_all_positions()
    if any(position.symbol == candidate.symbol for position in positions):
        raise ValueError(f"Already holding {candidate.symbol} in the paper account")
    open_orders = fetch_paper_orders(trading, status="open")
    if any(order.symbol == candidate.symbol for order in open_orders):
        raise ValueError(f"Open paper order already exists for {candidate.symbol}")
    open_buy_statuses = {
        "new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
        "done_for_day", "pending_cancel", "pending_replace", "held",
    }
    peer_symbols = {position.symbol for position in positions}
    peer_symbols.update(
        order.symbol
        for order in open_orders
        if str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).lower() == "buy"
        and str(getattr(getattr(order, "status", None), "value", getattr(order, "status", ""))).lower()
        in open_buy_statuses
    )
    peer_symbols.discard(candidate.symbol)
    if peer_symbols:
        histories = fetch_correlation_histories(peer_symbols | {candidate.symbol}, key, secret, cfg)
        enforce_correlation_limit(candidate.symbol, peer_symbols, histories, cfg)

    quote = StockHistoricalDataClient(key, secret).get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=candidate.symbol)
    ).get(candidate.symbol)
    if quote is None or quote.timestamp is None or quote.timestamp.tzinfo is None:
        raise ValueError("No timestamped Alpaca quote available")
    age = datetime.now(timezone.utc) - quote.timestamp.astimezone(timezone.utc)
    # Allow a few seconds of local clock skew; a quote far in the future is still rejected.
    if age < -QUOTE_CLOCK_SKEW_TOLERANCE or age > timedelta(seconds=cfg.max_quote_age_seconds):
        raise ValueError(
            f"Alpaca quote is stale; no order placed (quote age {age.total_seconds():.0f}s)"
        )
    plan = plan_paper_order(candidate, float(quote.bid_price), float(quote.ask_price), cfg)
    enforce_sector_exposure_limit(
        plan.symbol,
        plan.shares * plan.limit_price,
        positions,
        open_orders,
        cfg,
    )

    available_cash = min(float(account.cash), float(account.buying_power))
    if bot_mode:
        if not 0 < cfg.risk_fraction <= 0.02:
            raise ValueError("Paper bot per-trade risk must not exceed 2%")
        check_bot_daily_halt(account, cfg, datetime.now(timezone.utc))
        orders = fetch_paper_orders(trading)
        if bot_attempted_today(orders, candidate.symbol, datetime.now(timezone.utc)):
            raise ValueError(f"{candidate.symbol}: paper bot already attempted this symbol today")
        capacity = bot_capacity(account, positions, orders, cfg, datetime.now(timezone.utc))
        if cfg.bot_auto_recover and not reconcile_paper_risk(
            trading, positions, orders, datetime.now(timezone.utc), cfg,
        ):
            raise ValueError("Paper recovery in progress; no new entry placed")
        if capacity.slots <= 0:
            raise ValueError("Paper bot position limit reached")
        invested = sum(abs(float(position.market_value)) for position in positions)
        reserved = sum(float(order.limit_price) * (float(order.qty) - float(order.filled_qty or 0))
                       for order in orders if getattr(order.status, "value", order.status) in WORKING
                       and getattr(order.side, "value", order.side) == "buy")
        remaining_capital = max(0.0, min(float(account.equity), cfg.account_size) - invested - reserved)
        risk_per_share = plan.limit_price - plan.stop_price
        shares = min(plan.shares, math.floor(max(0.0, capacity.remaining_risk) / risk_per_share),
                     math.floor(remaining_capital / plan.limit_price))
        if shares < 1:
            raise ValueError("No whole shares fit remaining paper bot risk and capital limits")
        plan = replace(plan, shares=shares, risk_dollars=shares * risk_per_share)
    if plan.shares * plan.limit_price > available_cash:
        raise ValueError("Insufficient paper account cash for planned order")

    if execute:
        trading_day = datetime.now(ZoneInfo("America/New_York")).date()
        client_order_id = f"swing-paper-{candidate.symbol}-{trading_day:%Y%m%d}"
        save_entry_plan(client_order_id, candidate, plan, cfg)
        order = LimitOrderRequest(
            symbol=plan.symbol,
            qty=plan.shares,
            limit_price=plan.limit_price,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=plan.target_price),
            stop_loss=StopLossRequest(stop_price=plan.stop_price),
            client_order_id=client_order_id,
        )
        submitted = trading.submit_order(order_data=order)
        print(f"Submitted Alpaca paper bracket order {submitted.id} ({submitted.status})")
    return plan