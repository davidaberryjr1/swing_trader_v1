from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4


DEFAULT_ASSET_SNAPSHOT_DB = Path("data") / "alpaca_assets.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    retrieved_at TEXT NOT NULL,
    source TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    asset_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_observations (
    snapshot_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    exchange TEXT,
    status TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    tradable INTEGER NOT NULL,
    marginable INTEGER NOT NULL,
    shortable INTEGER NOT NULL,
    easy_to_borrow INTEGER NOT NULL,
    fractionable INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, symbol),
    FOREIGN KEY (snapshot_id) REFERENCES asset_snapshots(snapshot_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_asset_observations_symbol_status
    ON asset_observations(symbol, status, snapshot_id);
"""


def persist_asset_snapshot(
    assets,
    *,
    retrieved_at: datetime | None = None,
    path: str | Path = DEFAULT_ASSET_SNAPSHOT_DB,
) -> tuple[str, int]:
    normalized = []
    seen_symbols = set()
    for asset in assets:
        symbol = getattr(asset, "symbol", None)
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("Alpaca asset snapshot contains an asset without a symbol")
        symbol = symbol.strip().upper()
        if symbol in seen_symbols:
            raise ValueError(f"Alpaca asset snapshot contains duplicate symbol: {symbol}")
        seen_symbols.add(symbol)
        status = _value(getattr(asset, "status", None))
        if status not in {"active", "inactive"}:
            raise ValueError(f"{symbol}: unsupported Alpaca asset status: {status!r}")
        asset_class = _value(getattr(asset, "asset_class", None))
        if asset_class != "us_equity":
            raise ValueError(f"{symbol}: expected US equity, received asset class {asset_class!r}")
        normalized.append((
            symbol,
            _optional_text(getattr(asset, "name", None)),
            _optional_text(_value(getattr(asset, "exchange", None))),
            status,
            asset_class,
            _boolean(asset, "tradable", symbol),
            _boolean(asset, "marginable", symbol),
            _boolean(asset, "shortable", symbol),
            _boolean(asset, "easy_to_borrow", symbol),
            _boolean(asset, "fractionable", symbol),
        ))

    if not normalized:
        raise ValueError("Alpaca returned no US-equity assets; refusing to save an empty snapshot")

    timestamp = retrieved_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("Asset snapshot timestamp must include a timezone")
    timestamp = timestamp.astimezone(timezone.utc).isoformat()
    snapshot_id = str(uuid4())
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database_path)) as database:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript(_SCHEMA)
        with database:
            database.execute(
                """INSERT INTO asset_snapshots
                   (snapshot_id, retrieved_at, source, asset_class, asset_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (snapshot_id, timestamp, "alpaca_trading_api", "us_equity", len(normalized)),
            )
            database.executemany(
                """INSERT INTO asset_observations (
                    snapshot_id, symbol, name, exchange, status, asset_class, tradable,
                    marginable, shortable, easy_to_borrow, fractionable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(snapshot_id, *asset) for asset in normalized],
            )
    return snapshot_id, len(normalized)


def fetch_and_persist_alpaca_asset_snapshot(
    *,
    path: str | Path = DEFAULT_ASSET_SNAPSHOT_DB,
) -> tuple[str, int]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass
    from alpaca.trading.requests import GetAssetsRequest

    from .paper_trading import load_paper_credentials

    key, secret = load_paper_credentials()
    client = TradingClient(key, secret, paper=True)
    assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))
    return persist_asset_snapshot(assets, path=path)


def _value(value) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _optional_text(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Alpaca asset text fields must be strings")
    return value


def _boolean(asset, field: str, symbol: str) -> int:
    value = getattr(asset, field, None)
    if not isinstance(value, bool):
        raise ValueError(f"{symbol}: Alpaca asset field {field} must be boolean")
    return int(value)
