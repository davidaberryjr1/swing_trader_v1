import json
import math
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


FILLS_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_fills.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    order_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL, filled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fill_metadata (
    order_id TEXT PRIMARY KEY, order_type TEXT NOT NULL, client_order_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_plans (
    client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT NOT NULL,
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    config_json TEXT NOT NULL, candidate_json TEXT NOT NULL, plan_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_actions (
    client_order_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, symbol TEXT NOT NULL,
    kind TEXT NOT NULL, created_at TEXT NOT NULL, stop_price TEXT, target_price TEXT,
    qty TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'requested'
);
CREATE TABLE IF NOT EXISTS account_observations (
    observed_at TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL,
    positions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
"""


@contextmanager
def journal(path: Path = FILLS_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as database, database:
        database.row_factory = sqlite3.Row
        database.executescript(SCHEMA)
        yield database


def enum_value(value) -> str:
    return str(getattr(value, "value", value))


def timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Paper journal requires timezone-aware broker timestamps")
    return value.astimezone(timezone.utc).isoformat()


def quantity(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid nonnegative paper quantity or price") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid nonnegative paper quantity or price")
    return result


def flatten_orders(orders):
    unique = {}
    for order in orders:
        unique[str(order.id)] = order
        for leg in flatten_orders(getattr(order, "legs", None) or []):
            unique[str(leg.id)] = leg
    return list(unique.values())


def is_bot_entry(order) -> bool:
    return (
        str(getattr(order, "client_order_id", "") or "").startswith("swing-paper-")
        and enum_value(order.side) == "buy"
    )


def recovery_links(path: Path = FILLS_PATH) -> dict[str, str]:
    with journal(path) as database:
        return {
            row["client_order_id"]: row["entry_id"]
            for row in database.execute("SELECT client_order_id, entry_id FROM recovery_actions")
        }


def linked_orders(orders, path: Path = FILLS_PATH):
    links = recovery_links(path)
    result = {}
    for order in orders:
        entry_id = str(order.id) if is_bot_entry(order) else links.get(order.client_order_id)
        if entry_id is not None:
            for item in flatten_orders([order]):
                result[str(item.id)] = (entry_id, order.symbol, item)
    return list(result.values())


def record_paper_fills(orders, path: Path = FILLS_PATH) -> int:
    changes = 0
    with journal(path) as database:
        for entry_id, symbol, order in linked_orders(orders, path):
            qty = quantity(getattr(order, "filled_qty", None) or 0)
            if qty == 0:
                continue
            price = quantity(order.filled_avg_price)
            if price == 0:
                raise ValueError(f"{symbol}: broker fill has no positive average price")
            side = enum_value(order.side)
            if side not in {"buy", "sell"}:
                raise ValueError(f"{symbol}: unexpected fill side")
            # These are cumulative order fills, not individual execution events.
            observed = getattr(order, "filled_at", None) or getattr(order, "updated_at", None)
            row = (str(order.id), entry_id, symbol, side, str(qty), str(price), timestamp(observed))
            previous = database.execute(
                "SELECT qty, price FROM fills WHERE order_id = ?", (str(order.id),)
            ).fetchone()
            if previous and quantity(previous["qty"]) > qty:
                raise ValueError(f"{symbol}: broker cumulative fill quantity moved backwards")
            database.execute(
                """INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET qty=excluded.qty, price=excluded.price,
                    filled_at=excluded.filled_at
                WHERE fills.qty != excluded.qty OR fills.price != excluded.price
                    OR fills.filled_at != excluded.filled_at""",
                row,
            )
            database.execute(
                "INSERT OR REPLACE INTO fill_metadata VALUES (?, ?, ?)",
                (str(order.id), enum_value(getattr(order, "type", "unknown")),
                 str(getattr(order, "client_order_id", "") or "")),
            )
            if previous is None or (quantity(previous["qty"]), quantity(previous["price"])) != (qty, price):
                changes += 1
    return changes


def save_entry_plan(client_order_id, candidate, plan, cfg, path: Path = FILLS_PATH) -> None:
    from .scanner import STRATEGY_ID, STRATEGY_VERSION

    with journal(path) as database:
        inserted = database.execute(
            "INSERT OR IGNORE INTO entry_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (client_order_id, candidate.symbol, timestamp(datetime.now(timezone.utc)),
             STRATEGY_ID, STRATEGY_VERSION, json.dumps(asdict(cfg), sort_keys=True),
             json.dumps(asdict(candidate), sort_keys=True), json.dumps(asdict(plan), sort_keys=True)),
        ).rowcount
        if not inserted:
            raise ValueError(f"{candidate.symbol}: local entry already attempted today; no duplicate submitted")


def add_event(level: str, message: str, path: Path = FILLS_PATH) -> None:
    with journal(path) as database:
        database.execute(
            "INSERT INTO bot_events(observed_at, level, message) VALUES (?, ?, ?)",
            (timestamp(datetime.now(timezone.utc)), level, message),
        )


def synchronize_journal(account, positions, orders, now: datetime, path: Path = FILLS_PATH) -> int:
    account_id = str(account.id)
    if not account_id:
        raise ValueError("Broker account identity is missing")
    equity, cash = float(account.equity), float(account.cash)
    if not all(math.isfinite(value) for value in (equity, cash)) or equity <= 0:
        raise ValueError("Invalid paper account observation")
    with journal(path) as database:
        previous = database.execute(
            "SELECT value FROM journal_state WHERE key = 'account_id'"
        ).fetchone()
        if previous and previous["value"] != account_id:
            raise ValueError("Paper journal belongs to another account; use a separate database")
        database.execute(
            "INSERT OR IGNORE INTO journal_state VALUES ('account_id', ?)", (account_id,)
        )
    changes = record_paper_fills(orders, path)
    position_data = [
        {name: str(getattr(position, name)) for name in
         ("symbol", "qty", "avg_entry_price", "market_value", "unrealized_pl")}
        for position in positions
    ]
    with journal(path) as database:
        database.execute(
            "INSERT OR REPLACE INTO account_observations VALUES (?, ?, ?, ?)",
            (timestamp(now), equity, cash, json.dumps(position_data)),
        )
        database.execute(
            "INSERT OR REPLACE INTO journal_state VALUES ('last_sync', ?)", (timestamp(now),)
        )
    return changes


def format_paper_performance(path: Path = FILLS_PATH) -> str:
    if not path.is_file():
        return "No paper journal exists yet. Run the paper bot to synchronize broker state.\n"
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = database.execute("SELECT * FROM fills ORDER BY filled_at, order_id").fetchall() if "fills" in tables else []
        state = dict(database.execute("SELECT key, value FROM journal_state")) if "journal_state" in tables else {}
        observations = database.execute(
            "SELECT * FROM account_observations ORDER BY observed_at"
        ).fetchall() if "account_observations" in tables else []
        events = database.execute(
            "SELECT observed_at, level, message FROM bot_events ORDER BY id DESC LIMIT 10"
        ).fetchall() if "bot_events" in tables else []
        metadata = {
            row["order_id"]: row for row in database.execute("SELECT * FROM fill_metadata")
        } if "fill_metadata" in tables else {}
    trades = {}
    for row in rows:
        trade = trades.setdefault(row["entry_id"], {
            "symbol": row["symbol"], "buy_qty": Decimal(0), "sell_qty": Decimal(0),
            "buy_value": Decimal(0), "sell_value": Decimal(0),
            "entry_at": row["filled_at"], "last_exit_at": None, "exits": set(),
        })
        side = row["side"]
        if side not in {"buy", "sell"}:
            raise ValueError("Journal contains an unsupported fill side")
        qty, price = quantity(row["qty"]), quantity(row["price"])
        trade[f"{side}_qty"] += qty
        trade[f"{side}_value"] += qty * price
        if side == "buy":
            trade["entry_at"] = max(trade["entry_at"], row["filled_at"])
        else:
            trade["last_exit_at"] = row["filled_at"]
            detail = metadata.get(row["order_id"])
            if detail is None:
                trade["exits"].add("unknown (legacy fill)")
            elif detail["client_order_id"].startswith("swing-recover-"):
                trade["exits"].add(f"recovery {detail['order_type']}")
            else:
                trade["exits"].add({"stop": "stop", "limit": "take-profit"}.get(detail["order_type"], "unknown"))
    lines = [
        "# Paper Trading Performance", "",
        f"Last successful broker synchronization: {state.get('last_sync', 'not recorded')}",
        "Local observations only; this report does not query current broker state.",
        "Gross results only: fees are not imported. Historical entry rationale may be unavailable.",
        "Fills are cumulative broker order observations; partial-fill timestamps may be observation times.",
        "", "| Symbol | Bought | Sold | Remaining | Realized gross P&L | Realized return | Closed holding days | Exit | State |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    closed = []
    realized_total = Decimal(0)
    for trade in trades.values():
        bought, sold = trade["buy_qty"], trade["sell_qty"]
        if bought == 0 or sold > bought:
            raise ValueError(f"{trade['symbol']}: unmatched paper fills; reconcile before reporting P&L")
        pnl = trade["sell_value"] - trade["buy_value"] / bought * sold
        realized_total += pnl
        state_label = "closed" if sold == bought else "open / partially exited"
        if sold == bought:
            closed.append(pnl)
        cost = trade["buy_value"] / bought * sold
        realized_return = f"{pnl / cost:.2%}" if cost > 0 else "N/A"
        holding = (
            f"{(datetime.fromisoformat(trade['last_exit_at']) - datetime.fromisoformat(trade['entry_at'])).total_seconds() / 86400:.2f}"
            if sold == bought else "N/A"
        )
        lines.append(
            f"| {trade['symbol']} | {bought} | {sold} | {bought - sold} | ${pnl:.2f} | "
            f"{realized_return} | {holding} | {', '.join(sorted(trade['exits'])) or 'none'} | {state_label} |"
        )
    lines.extend(["", f"Realized gross P&L (including partial exits): ${realized_total:.2f}",
                  f"Closed trades: {len(closed)}"])
    if closed:
        wins = [pnl for pnl in closed if pnl > 0]
        losses = [pnl for pnl in closed if pnl < 0]
        profit_factor = f"{sum(wins) / abs(sum(losses)):.2f}" if losses else "N/A (no losses)"
        lines.extend([
            f"Win rate: {len(wins) / len(closed):.1%}",
            f"Average win: ${sum(wins) / len(wins):.2f}" if wins else "Average win: N/A",
            f"Average loss: ${sum(losses) / len(losses):.2f}" if losses else "Average loss: N/A",
            f"Expectancy per closed trade: ${sum(closed) / len(closed):.2f}",
            f"Profit factor: {profit_factor}",
        ])
    if observations:
        latest = observations[-1]
        peak = observations[0]["equity"]
        drawdown = 0.0
        for observation in observations:
            peak = max(peak, observation["equity"])
            drawdown = max(drawdown, 1 - observation["equity"] / peak)
        lines.extend([
            "", "## Account-wide observations (not bot-only returns)",
            f"Observed equity: ${latest['equity']:,.2f}; cash: ${latest['cash']:,.2f}",
            f"Maximum observed account drawdown: {drawdown:.2%}",
            "Includes other account activity and cash flows; not a time-weighted return.",
            "Drawdown covers sampled observations only, not unobserved intracycle moves.",
            "", "| Open symbol | Quantity | Market value | Unrealized P&L |",
            "|---|---:|---:|---:|",
        ])
        for position in json.loads(latest["positions_json"]):
            lines.append(
                f"| {position['symbol']} | {position['qty']} | ${float(position['market_value']):.2f} "
                f"| ${float(position['unrealized_pl']):.2f} |"
            )
    if events:
        lines.extend(["", "## Recent operational events"])
        lines.extend(f"- {row['observed_at']} [{row['level']}] {row['message']}" for row in events)
    return "\n".join(lines) + "\n"
