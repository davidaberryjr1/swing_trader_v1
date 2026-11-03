from datetime import date, datetime, time, timedelta, timezone
import re
from dataclasses import dataclass
from typing import Protocol

import pandas as pd
import pandas_market_calendars as mcal

from .data import download_batch, download_history_range, download_latest_prices


@dataclass(frozen=True)
class SymbolDataCoverage:
    symbol: str
    expected_sessions: int
    returned_sessions: int
    missing_sessions: tuple[date, ...]

    @property
    def coverage_fraction(self) -> float | None:
        if not self.expected_sessions:
            return None
        return self.returned_sessions / self.expected_sessions


def audit_daily_coverage(
    histories: dict[str, pd.DataFrame],
    symbols: list[str],
    start: str,
    end: str,
    *,
    membership: dict[str, tuple[tuple[date, date | None], ...]] | None = None,
) -> list[SymbolDataCoverage]:
    """Compare returned daily bars with NYSE sessions while each symbol is eligible."""
    first_day = date.fromisoformat(start)
    last_day = date.fromisoformat(end)
    if first_day > last_day:
        raise ValueError("History start date cannot be later than end date")

    calendar_sessions = mcal.get_calendar("NYSE").valid_days(
        start_date=first_day,
        end_date=last_day,
    )
    if calendar_sessions.tz is not None:
        calendar_sessions = calendar_sessions.tz_convert("UTC").tz_localize(None)
    expected_market_days = {timestamp.date() for timestamp in calendar_sessions.normalize()}

    coverage = []
    for symbol in dict.fromkeys(symbols):
        intervals = membership.get(symbol, ()) if membership is not None else ((first_day, last_day),)
        expected = {
            day for day in expected_market_days
            if any(start_date <= day and (end_date is None or day <= end_date)
                   for start_date, end_date in intervals)
        }
        history = histories.get(symbol)
        returned = set()
        if history is not None and not history.empty:
            if not isinstance(history.index, pd.DatetimeIndex):
                raise ValueError(f"{symbol}: daily-bar index must be a DatetimeIndex")
            timestamps = pd.DatetimeIndex(pd.to_datetime(history.index, errors="coerce"))
            if timestamps.hasnans:
                raise ValueError(f"{symbol}: daily bars contain invalid timestamps")
            if timestamps.tz is not None:
                timestamps = timestamps.tz_convert("UTC").tz_localize(None)
            returned = {timestamp.date() for timestamp in timestamps.normalize()} & expected
        missing = tuple(sorted(expected - returned))
        coverage.append(SymbolDataCoverage(
            symbol=symbol,
            expected_sessions=len(expected),
            returned_sessions=len(returned),
            missing_sessions=missing,
        ))
    return coverage


def format_data_coverage_report(coverage: list[SymbolDataCoverage]) -> str:
    if not coverage:
        return "## Historical Data Coverage\n\nNo symbols were available for coverage analysis.\n"
    total_expected = sum(item.expected_sessions for item in coverage)
    total_returned = sum(item.returned_sessions for item in coverage)
    overall = "N/A" if not total_expected else f"{total_returned / total_expected:.2%}"
    rows = [
        "## Historical Data Coverage",
        "",
        f"Overall returned/expected symbol-sessions: {total_returned:,}/{total_expected:,} ({overall}).",
        "Expected sessions are NYSE sessions inside each symbol's effective membership intervals. "
        "Missing sessions may reflect provider coverage, symbol halts, or data gaps; they are not imputed.",
        "",
        "| Symbol | Expected sessions | Returned sessions | Coverage | Missing sessions | Missing date examples |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in coverage:
        fraction = "N/A" if item.coverage_fraction is None else f"{item.coverage_fraction:.2%}"
        examples = ", ".join(day.isoformat() for day in item.missing_sessions[:5]) or "None"
        rows.append(
            f"| {item.symbol} | {item.expected_sessions} | {item.returned_sessions} | "
            f"{fraction} | {len(item.missing_sessions)} | {examples} |"
        )
    return "\n".join(rows) + "\n"


def validate_daily_history(
    histories: dict[str, pd.DataFrame],
    symbols: list[str],
    start: str,
    end: str,
    *,
    membership: dict[str, tuple[tuple[date, date | None], ...]] | None = None,
) -> list[str]:
    """Validate daily-bar contract and report missing NYSE trading sessions."""
    first_day = date.fromisoformat(start)
    last_day = date.fromisoformat(end)
    if first_day > last_day:
        raise ValueError("History start date cannot be later than end date")

    required_columns = {"Open", "High", "Low", "Close", "Volume"}
    requested_sessions = mcal.get_calendar("NYSE").valid_days(
        start_date=first_day,
        end_date=last_day,
    )
    if requested_sessions.tz is not None:
        requested_sessions = requested_sessions.tz_convert("UTC").tz_localize(None)
    requested_sessions = requested_sessions.normalize()
    warnings = []

    for symbol in dict.fromkeys(symbols):
        symbol_sessions = requested_sessions
        if membership is not None:
            intervals = membership.get(symbol, ())
            symbol_sessions = pd.DatetimeIndex([
                session for session in requested_sessions
                if any(
                    first <= session.date() and (last is None or session.date() <= last)
                    for first, last in intervals
                )
            ])
        if symbol not in histories:
            if symbol_sessions.empty:
                continue
            warnings.append(_missing_session_warning(symbol, symbol_sessions, no_bars=True))
            continue
        history = histories[symbol]
        if not isinstance(history, pd.DataFrame):
            raise TypeError(f"{symbol}: daily history must be a pandas DataFrame")
        if history.empty:
            if not symbol_sessions.empty:
                warnings.append(_missing_session_warning(symbol, symbol_sessions, no_bars=True))
            continue
        if not isinstance(history.index, pd.DatetimeIndex):
            raise ValueError(f"{symbol}: daily-bar index must be a DatetimeIndex")
        if history.columns.has_duplicates:
            raise ValueError(f"{symbol}: daily bars contain duplicate column names")

        missing_columns = required_columns.difference(history.columns)
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"{symbol}: daily bars are missing required columns: {missing}")

        index = pd.DatetimeIndex(pd.to_datetime(history.index, errors="coerce"))
        if index.hasnans:
            raise ValueError(f"{symbol}: daily bars contain invalid timestamps")
        if index.tz is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        sessions = index.normalize()
        if sessions.has_duplicates:
            raise ValueError(f"{symbol}: daily bars contain duplicate session dates")
        if not sessions.is_monotonic_increasing:
            raise ValueError(f"{symbol}: daily bars are not in chronological order")

        first_timestamp = pd.Timestamp(first_day)
        last_timestamp = pd.Timestamp(last_day)
        if (sessions < first_timestamp).any() or (sessions > last_timestamp).any():
            raise ValueError(f"{symbol}: daily bars fall outside the requested date range")
        returned_dates = sessions[(sessions >= first_timestamp) & (sessions <= last_timestamp)]
        non_sessions = returned_dates.difference(requested_sessions)
        if not non_sessions.empty:
            examples = ", ".join(day.strftime("%Y-%m-%d") for day in non_sessions[:5])
            raise ValueError(f"{symbol}: daily bars fall on non-NYSE sessions: {examples}")

        prices = history[["Open", "High", "Low", "Close", "Volume"]].apply(
            pd.to_numeric, errors="coerce",
        )
        if prices.isna().any().any():
            raise ValueError(f"{symbol}: daily bars contain missing or non-numeric OHLCV values")
        if (prices[["Open", "High", "Low", "Close"]] <= 0).any().any() or (prices["Volume"] < 0).any():
            raise ValueError(f"{symbol}: daily bars contain invalid prices or volume")
        if (
            (prices["High"] < prices[["Open", "Close", "Low"]].max(axis=1)).any()
            or (prices["Low"] > prices[["Open", "Close", "High"]].min(axis=1)).any()
        ):
            raise ValueError(f"{symbol}: daily bars contain inconsistent OHLC values")

        missing_sessions = symbol_sessions.difference(sessions)
        if not missing_sessions.empty:
            warnings.append(_missing_session_warning(symbol, missing_sessions))

    return warnings


def _missing_session_warning(
    symbol: str,
    missing_sessions: pd.DatetimeIndex,
    *,
    no_bars: bool = False,
) -> str:
    examples = ", ".join(day.strftime("%Y-%m-%d") for day in missing_sessions[:5])
    prefix = f"{symbol}: no daily bars returned; " if no_bars else f"{symbol}: "
    return (
        f"{prefix}{len(missing_sessions)} expected NYSE sessions have no bar "
        f"(data gap), e.g. {examples}"
    )


class MarketDataProvider(Protocol):
    def get_bars(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        period: str | None = None,
    ) -> dict[str, pd.DataFrame]: ...

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]: ...


class YFinanceMarketDataProvider:
    def __init__(self, *, batch_loader=None, range_loader=None, latest_price_loader=None):
        self._batch_loader = batch_loader or download_batch
        self._range_loader = range_loader or download_history_range
        self._latest_price_loader = latest_price_loader or download_latest_prices

    def get_bars(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        period: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        if period is not None and start is None and end is None:
            return self._batch_loader(symbols, period=period, batch_size=80)
        if start is None or end is None or period is not None:
            raise ValueError("Provide either period or both explicit start and end dates")
        return self._range_loader(symbols, start, end, batch_size=80)

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return self._latest_price_loader(symbols)


class AlpacaMarketDataProvider:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        client=None,
        asof: str | None = "-",
    ):
        if client is None:
            if not api_key or not secret_key:
                raise ValueError("Alpaca API credentials are required")
            from alpaca.data.historical import StockHistoricalDataClient

            client = StockHistoricalDataClient(api_key, secret_key)
        self._client = client
        self._asof = asof

    def get_bars(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        period: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start, end = self._date_range(start, end, period)
        start_dt = datetime.combine(date.fromisoformat(start), time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(date.fromisoformat(end), time.max, tzinfo=timezone.utc)
        response = self._client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            start=start_dt,
            end=end_dt,
            timeframe=TimeFrame.Day,
            adjustment=Adjustment.SPLIT,
            feed=DataFeed.IEX,
            asof=self._asof,
        ))
        frame = response.df
        if frame.empty:
            return {}
        histories = {}
        for symbol in symbols:
            try:
                history = frame.xs(symbol, level="symbol").copy()
            except KeyError:
                continue
            history.columns = [str(column).title() for column in history.columns]
            histories[symbol] = history[["Open", "High", "Low", "Close", "Volume"]].dropna()
        return histories

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        response = self._client.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=symbols,
            feed=DataFeed.IEX,
        ))
        return {
            symbol: float(trade.price)
            for symbol, trade in response.items()
            if getattr(trade, "price", None) is not None
        }

    @staticmethod
    def _date_range(start: str | None, end: str | None, period: str | None) -> tuple[str, str]:
        if period is not None:
            if start is not None or end is not None:
                raise ValueError("Provide period or explicit dates, not both")
            match = re.fullmatch(r"(\d+)(d|wk|mo|y)", period)
            if match is None:
                raise ValueError("Alpaca period must use days, weeks, months, or years (for example 1y)")
            amount, unit = int(match.group(1)), match.group(2)
            today = datetime.now(timezone.utc).date()
            if unit == "d":
                first_day = today - timedelta(days=amount)
            else:
                offset = {"wk": pd.DateOffset(weeks=amount), "mo": pd.DateOffset(months=amount), "y": pd.DateOffset(years=amount)}[unit]
                first_day = (pd.Timestamp(today) - offset).date()
            return first_day.isoformat(), today.isoformat()
        if start is None or end is None:
            raise ValueError("Provide both start and end dates")
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError("History start date cannot be later than end date")
        return start, end
