import json
import urllib.request
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import yfinance as yf


def fetch_market_candidates(
    minimum_price: float = 5.0,
    maximum_price: float = 30.0,
    minimum_volume: int = 500_000,
    cache_path: str = "data/nasdaq_screener.json",
) -> list[str]:
    """Fetch all NYSE and NASDAQ common stocks in 1 instant request via Nasdaq official screener."""
    path = Path(cache_path)
    rows = []

    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data", {}).get("rows", [])
        if rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Warning: Nasdaq screener online fetch failed ({exc}). Trying local cache...")
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    if not rows:
        return []

    def parse_price(val):
        if not val:
            return 0.0
        s = str(val).replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0

    def parse_vol(val):
        if not val:
            return 0
        s = str(val).replace(",", "").strip()
        try:
            return int(s)
        except ValueError:
            return 0

    def is_valid_stock(row):
        sym = row.get("symbol", "")
        name = row.get("name", "").lower()
        if not sym or any(c in sym for c in ["^", ".", "/", " ", "$"]):
            return False
        # Filter out warrants, rights, units, preferreds, and funds
        if any(w in name for w in ["warrant", "right", "unit", "preferred", "etf", "fund", "trust", "note", "etn", "par $", "depository"]):
            return False
        price = parse_price(row.get("lastsale"))
        vol = parse_vol(row.get("volume"))
        return minimum_price <= price <= maximum_price and vol >= minimum_volume

    qualifying = [r["symbol"] for r in rows if is_valid_stock(r)]
    print(f"Discovered {len(qualifying)} qualifying ${minimum_price:.2f}-${maximum_price:.2f} stocks (> {minimum_volume:,} volume) across NYSE & NASDAQ.")
    return qualifying


def _cache_file(cache_dir: Path, symbol: str, period: str) -> Path:
    return cache_dir / f"{quote(symbol, safe='')}__{quote(period, safe='')}.pkl"


def _download_chunks(
    symbols: list[str],
    period: str | None,
    batch_size: int,
    *,
    start: str | None = None,
    end: str | None = None,
    minimum_bars: int = 30,
) -> dict[str, pd.DataFrame]:
    import time

    results = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size
    for idx in range(total_batches):
        chunk = symbols[idx * batch_size : (idx + 1) * batch_size]
        try:
            request = {
                "interval": "1d",
                "auto_adjust": True,
                "group_by": "ticker",
                "progress": False,
                "threads": True,
            }
            if start is not None and end is not None:
                request.update(start=start, end=end)
            else:
                request["period"] = period
            df = yf.download(
                chunk,
                **request,
            )
            if df.empty:
                continue

            if len(chunk) == 1:
                ticker = chunk[0]
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                clean_df = df.dropna()
                if not clean_df.empty and len(clean_df) >= minimum_bars:
                    results[ticker] = clean_df
            else:
                for ticker in chunk:
                    try:
                        if ticker in df:
                            sub_df = df[ticker].dropna()
                            if not sub_df.empty and len(sub_df) >= minimum_bars:
                                results[ticker] = sub_df
                    except Exception:
                        continue
        except Exception:
            pass
        
        # Short polite pause between chunks to prevent rate-limiting
        if idx < total_batches - 1:
            time.sleep(0.2)

    return results


def download_history_range(
    symbols: list[str],
    start: str,
    end: str,
    batch_size: int = 60,
) -> dict[str, pd.DataFrame]:
    """Download adjusted daily bars for an explicit date range without the period cache."""
    if not symbols:
        return {}
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("History start date cannot be later than end date")
    return _download_chunks(
        symbols,
        None,
        batch_size,
        start=start,
        end=end,
        minimum_bars=1,
    )


def _merge_history(cached: pd.DataFrame, update: pd.DataFrame, period: str) -> pd.DataFrame:
    merged = pd.concat([cached, update])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index().dropna()
    if period == "1y" and not merged.empty:
        cutoff = merged.index[-1] - pd.DateOffset(years=1)
        merged = merged.loc[merged.index >= cutoff]
    return merged


def download_batch(
    symbols: list[str],
    period: str = "1y",
    batch_size: int = 60,
    cache_dir: str | Path = "data/cache/ohlcv",
) -> dict[str, pd.DataFrame]:
    """Load daily histories from cache, downloading only missing or stale bars."""
    if not symbols:
        return {}

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    today = date.today()
    results = {}
    missing = []
    stale = []

    for symbol in symbols:
        path = _cache_file(cache_root, symbol, period)
        if not path.exists():
            missing.append(symbol)
            continue
        try:
            cached = pd.read_pickle(path)
            if cached.empty or len(cached) < 30:
                missing.append(symbol)
                continue
            results[symbol] = cached
            if datetime.fromtimestamp(path.stat().st_mtime).date() != today:
                stale.append(symbol)
        except (OSError, ValueError, TypeError, EOFError):
            missing.append(symbol)

    downloaded = _download_chunks(missing, period, batch_size)
    updates = _download_chunks(stale, "5d", batch_size)

    for symbol, history in downloaded.items():
        results[symbol] = history

    for symbol, update in updates.items():
        results[symbol] = _merge_history(results[symbol], update, period)

    for symbol in downloaded.keys() | updates.keys():
        try:
            results[symbol].to_pickle(_cache_file(cache_root, symbol, period))
        except OSError:
            pass

    fresh_count = len(symbols) - len(missing) - len(stale)
    unavailable_count = len(symbols) - len(results)
    print(
        f"OHLCV cache: {fresh_count} fresh, {len(updates)} refreshed, "
        f"{len(downloaded)} full downloads, {unavailable_count} unavailable."
    )
    return results


def download_latest_prices(
    symbols: list[str],
    batch_size: int = 80,
) -> dict[str, float]:
    """Return latest regular or extended-hours prices in threaded batches."""
    prices = {}
    for start in range(0, len(symbols), batch_size):
        chunk = symbols[start : start + batch_size]
        try:
            data = yf.download(
                chunk,
            period="1d",
            interval="1m",
            prepost=True,
            auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
        )
            if data.empty:
                continue

            if len(chunk) == 1:
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)
                close = data["Close"].dropna()
                if not close.empty:
                    prices[chunk[0]] = float(close.iloc[-1])
                continue

            for symbol in chunk:
                try:
                    close = data[symbol]["Close"].dropna()
                    if not close.empty:
                        prices[symbol] = float(close.iloc[-1])
                except (KeyError, TypeError):
                    continue
        except Exception:
            continue

    for symbol in set(symbols) - prices.keys():
        try:
            history = yf.Ticker(symbol).history(
                period="1d",
                interval="1m",
                prepost=True,
                auto_adjust=True,
            )
            close = history["Close"].dropna()
            if not close.empty:
                prices[symbol] = float(close.iloc[-1])
        except Exception:
            continue

    return prices
