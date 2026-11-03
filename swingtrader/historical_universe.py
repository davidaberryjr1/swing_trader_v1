import csv
from dataclasses import dataclass
from datetime import date
import hashlib
from io import StringIO
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class HistoricalUniverse:
    membership: dict[str, tuple[tuple[date, date | None], ...]]
    source_path: str
    sha256: str
    record_count: int

    @property
    def symbols(self) -> list[str]:
        return sorted(self.membership)

    def symbols_overlapping(self, start: date, end: date) -> list[str]:
        return [
            symbol
            for symbol, intervals in self.membership.items()
            if any(
                first <= end and (last is None or last >= start)
                for first, last in intervals
            )
        ]

    def is_member(self, symbol: str, day: date) -> bool:
        return any(
            first <= day and (last is None or day <= last)
            for first, last in self.membership.get(symbol, ())
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                symbol: [
                    {
                        "start_date": first.isoformat(),
                        "end_date": last.isoformat() if last else None,
                    }
                    for first, last in intervals
                ]
                for symbol, intervals in sorted(self.membership.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def load_historical_universe(path: str | Path) -> HistoricalUniverse:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read historical universe file {source}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Historical universe file must be UTF-8: {source}") from exc

    reader = csv.DictReader(StringIO(text))
    required = {"symbol", "start_date", "end_date"}
    if (
        reader.fieldnames is None
        or len(reader.fieldnames) != len(set(reader.fieldnames))
        or not required.issubset(reader.fieldnames)
    ):
        raise ValueError("Historical universe CSV requires symbol,start_date,end_date columns")

    membership: dict[str, list[tuple[date, date | None]]] = {}
    record_count = 0
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"Historical universe row {line_number}: too many CSV fields")
        symbol = (row.get("symbol") or "").strip().upper()
        start_text = (row.get("start_date") or "").strip()
        end_text = (row.get("end_date") or "").strip()
        if not symbol or any(character.isspace() for character in symbol):
            raise ValueError(f"Historical universe row {line_number}: invalid symbol")
        if not start_text:
            raise ValueError(f"Historical universe row {line_number}: start_date is required")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_text) or (
            end_text and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_text)
        ):
            raise ValueError(f"Historical universe row {line_number}: dates must use YYYY-MM-DD")
        try:
            first = date.fromisoformat(start_text)
            last = date.fromisoformat(end_text) if end_text else None
        except ValueError as exc:
            raise ValueError(
                f"Historical universe row {line_number}: dates must use YYYY-MM-DD"
            ) from exc
        if last is not None and last < first:
            raise ValueError(f"Historical universe row {line_number}: end_date precedes start_date")
        membership.setdefault(symbol, []).append((first, last))
        record_count += 1

    if not membership:
        raise ValueError("Historical universe file contains no membership records")

    normalized = {}
    for symbol, intervals in membership.items():
        intervals.sort(key=lambda interval: interval[0])
        previous_end = None
        first_interval = True
        for first, last in intervals:
            if previous_end is None and not first_interval:
                raise ValueError(f"{symbol}: overlapping or open-ended membership intervals")
            if previous_end is not None and first <= previous_end:
                raise ValueError(f"{symbol}: overlapping membership intervals")
            previous_end = last
            first_interval = False
        normalized[symbol] = tuple(intervals)

    return HistoricalUniverse(
        membership=normalized,
        source_path=str(source),
        sha256=hashlib.sha256(raw).hexdigest(),
        record_count=record_count,
    )
