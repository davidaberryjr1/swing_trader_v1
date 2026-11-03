import argparse
import importlib.util
import json
import math
import sys
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter, sleep
from zoneinfo import ZoneInfo

import pandas as pd

from swingtrader.config import TradingConfig, load_trading_config
from swingtrader.data import (
    download_batch,
    download_latest_prices,
    fetch_market_candidates,
    load_cached_daily_close,
)
from swingtrader.scanner import TradeCandidate, analyze, refresh_candidate_price


FALLBACK_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL",
    "AVGO", "AMD", "NFLX", "TSLA", "PLTR", "ORCL",
    "CRM", "MU", "QCOM", "COST", "JPM", "XOM",
    "SOFI", "F", "PFE", "INTC", "SNAP", "RIVN",
    "NU", "VALE", "T", "BAC", "GM", "CCL", "NCLH", "DKNG",
    "HOOD", "MARA", "RKLB", "SIRI", "TGT", "AES", "ABUS", "AGRO", "AESI"
]

REPORT_DIR = Path("data") / "scans"
GUI_RUN_CACHE_DIR = REPORT_DIR / "gui_runs"
BOT_LOCK_PATH = Path("data") / "paper_bot.lock"


class ReportOutput:
    def __init__(self, console, report):
        self.console = console
        self.report = report

    def write(self, text: str) -> int:
        written = self.console.write(text)
        self.report.write(text)
        return written

    def flush(self):
        self.console.flush()
        self.report.flush()


def build_config(
    minimum_price=None,
    maximum_price=None,
):
    cfg = load_trading_config()
    minimum_price = cfg.minimum_price if minimum_price is None else minimum_price
    maximum_price = cfg.maximum_price if maximum_price is None else maximum_price

    if minimum_price > maximum_price:
        raise ValueError("Minimum price cannot be greater than maximum price.")

    return replace(
        cfg,
        minimum_price=minimum_price,
        maximum_price=maximum_price,
    )


def save_scan(candidates, symbols_scanned, output_path):
    payload = {
        "scan_date": date.today().isoformat(),
        "total_symbols_scanned": len(symbols_scanned),
        "total_candidates": len(candidates),
        "candidates": [asdict(c) for c in candidates],
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved scan snapshot to {path}")


def print_candidates_table(candidates, cfg=None):
    cfg = cfg or TradingConfig()
    target_pct_label = f"+{cfg.target_percent:g}% Target"
    print("\n## Top Swing-Trading Candidates (Ranked by Highest Setup Probability)\n")
    print(
        f"| Rank | Symbol | Score | P(+10%) | P(-5%) | Risk | Latest | Move vs Close | Stop | {target_pct_label} | Target 1 (+8-10%) | Target 2 (+16-20%) | Shares | Max Risk | Catalyst | Why Attractive |"
    )
    print("|---:|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")

    for rank, c in enumerate(candidates, start=1):
        reasons = "; ".join(c.reasons)
        move = f"{c.price_change_pct:+.1%}" if c.price_change_pct is not None else "N/A"
        print(
            f"| {rank} | **{c.symbol}** | {c.score}/100 | **{c.prob_gain_10d:.1%}** | {c.prob_loss_5d:.1%} | {c.risk_rating} | "
            f"${c.entry:.2f} | {move} | ${c.stop:.2f} | **${c.target_1pct:.2f}** | ${c.target_1:.2f} | ${c.target_2:.2f} | {c.shares} | "
            f"${c.risk_dollars:.2f} | {c.main_catalyst} | {reasons} |"
        )


def run_scanner(symbols=None, output_path=None, top=25, cfg=None, progress_callback=None):
    """Run the consistent swing trading scanner across NYSE & NASDAQ candidates."""
    cfg = cfg or TradingConfig()
    if progress_callback:
        progress_callback(5, "Finding stocks to scan...")

    if not symbols:
        symbols = fetch_market_candidates(
            minimum_price=cfg.minimum_price,
            maximum_price=cfg.maximum_price,
            minimum_volume=cfg.minimum_average_volume,
        ) or FALLBACK_SYMBOLS

    if progress_callback:
        progress_callback(12, f"Downloading price history for {len(symbols)} stocks...")
    print(f"Ingesting 1-year OHLCV bars for {len(symbols)} liquid ${cfg.minimum_price:.2f}-${cfg.maximum_price:.2f} candidates...")
    historical_data = download_batch(symbols, period="1y", batch_size=80)

    if progress_callback:
        progress_callback(25, "Analyzing stock histories...")
    candidates = []
    total_symbols = len(symbols)
    for index, symbol in enumerate(symbols, start=1):
        if progress_callback and (index == 1 or index % 5 == 0 or index == total_symbols):
            progress = 25 + int(50 * index / total_symbols)
            progress_callback(progress, f"Analyzing {symbol} ({index}/{total_symbols})...")
        data = historical_data.get(symbol)
        if data is None or data.empty:
            continue
        try:
            candidate = analyze(symbol, data, cfg)
            if candidate:
                candidates.append(candidate)
        except Exception as exc:
            print(f"  Error analyzing {symbol}: {exc}")

    if candidates:
        if progress_callback:
            progress_callback(78, f"Refreshing prices for {len(candidates)} candidates...")
        latest_prices = download_latest_prices([candidate.symbol for candidate in candidates])
        refreshed = []
        for candidate in candidates:
            latest_price = latest_prices.get(candidate.symbol)
            if latest_price is None:
                refreshed.append(candidate)
                continue
            repriced = refresh_candidate_price(candidate, latest_price, cfg)
            if repriced:
                refreshed.append(repriced)
        candidates = refreshed
        print(f"Updated {len(latest_prices)} candidates with latest extended-hours prices.")

    # Rank strictly from highest probability to lowest (with score tie-breaker)
    if progress_callback:
        progress_callback(88, "Ranking scan results...")
    candidates.sort(key=lambda x: (x.prob_gain_10d, x.score, x.reward_risk), reverse=True)

    if top and top > 0:
        candidates = candidates[:top]

    if output_path is None:
        output_path = Path("data") / "scans" / f"scan_{date.today().isoformat()}.json"

    if progress_callback:
        progress_callback(95, "Saving scan results...")
    save_scan(candidates, symbols, output_path)

    if not candidates:
        print("\nNo candidates met the full swing criteria today.")
        if progress_callback:
            progress_callback(100, "Scan finished. No candidates matched.")
        return []

    print_candidates_table(candidates, cfg=cfg)
    if progress_callback:
        progress_callback(100, f"Scan finished. Found {len(candidates)} candidates.")
    return candidates


def run_paper_bot_cycle(cfg, symbols=None, top=25, *, scan=run_scanner, now=None):
    from swingtrader.paper_trading import (
        bot_attempted_today, bot_capacity, check_bot_daily_halt, paper_bot_snapshot, record_paper_fills,
        review_paper_orders,
        submit_paper_candidate,
    )

    now = now or datetime.now(timezone.utc)
    trading, orders = paper_bot_snapshot()
    new_fills = record_paper_fills(orders)
    if new_fills:
        print(f"Recorded {new_fills} paper fills in the local ledger")
    messages = review_paper_orders(trading, orders, now, cancel_stale=True)
    for message in messages:
        print(message)
    if any("manual review" in message or "cancellation requested" in message for message in messages):
        print("Paper bot paused: verify orders before any new entries")
        return False
    account = trading.get_account()
    positions = trading.get_all_positions()
    try:
        check_bot_daily_halt(account, cfg, now)
        capacity = bot_capacity(account, positions, orders, cfg, now)
    except ValueError as exc:
        print(f"Paper bot paused: {exc}")
        return False
    if scan is None:
        return False
    local_time = now.astimezone(ZoneInfo("America/New_York"))
    if not getattr(trading.get_clock(), "is_open", False) or not (10, 0) <= (local_time.hour, local_time.minute) < (15, 30):
        return False
    if capacity.slots <= 0 or capacity.remaining_risk <= 0:
        print("Paper bot at position or planned-risk limit")
        return False

    candidates = scan(symbols=symbols, top=top, cfg=cfg)
    for candidate in candidates:
        if candidate.symbol in capacity.symbols or bot_attempted_today(orders, candidate.symbol, now):
            continue
        try:
            plan = submit_paper_candidate(candidate, cfg, execute=True, bot_mode=True)
            print(f"Paper bot: {plan.symbol} buy {plan.shares} @ <= ${plan.limit_price:.2f}; "
                  f"stop ${plan.stop_price:.2f}; target ${plan.target_price:.2f}")
        except ValueError as exc:
            print(f"Paper bot skipped {candidate.symbol}: {exc}")
    return True


def run_paper_bot(cfg, symbols=None, top=25, *, once=False):
    if importlib.util.find_spec("alpaca") is None:
        raise RuntimeError(
            f"alpaca-py is not installed in {sys.executable}; run the bot with .\\.venv\\Scripts\\python.exe"
        )
    if cfg.bot_poll_seconds < 30:
        raise ValueError("Paper bot polling interval must be at least 30 seconds")
    if cfg.bot_scan_interval_seconds < cfg.bot_poll_seconds:
        raise ValueError("Paper bot scan interval must be at least the polling interval")
    last_scan_at = None
    while True:
        now = perf_counter()
        try:
            scan = run_scanner if last_scan_at is None or now - last_scan_at >= cfg.bot_scan_interval_seconds else None
            if run_paper_bot_cycle(cfg, symbols, top, scan=scan):
                last_scan_at = perf_counter()
        except Exception as exc:
            print(f"Paper bot paused this cycle: {exc}")
            if once:
                raise
        print(f"Paper bot cycle complete at {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
        if once:
            break
        sleep(cfg.bot_poll_seconds)


def run_backtest_cli(
    symbols,
    start,
    end,
    cfg,
    *,
    slippage_bps=5.0,
    spread_bps=5.0,
    spread_scenarios_bps=None,
    commission_per_share=0.0,
    target_fill_mode="trade_through",
    target_trade_through_bps=5.0,
):
    from swingtrader.backtest import (
        AlpacaMarketDataProvider,
        SECTOR_BY_SYMBOL,
        SECTOR_ETF_BY_SECTOR,
        format_spread_sensitivity_report,
        run_backtest,
    )
    from swingtrader.backtest_store import DEFAULT_BACKTEST_DB, persist_sensitivity_results
    from swingtrader.paper_trading import load_paper_credentials

    if not symbols:
        raise ValueError("Backtests require explicit --symbols to make the tested universe reproducible")
    key, secret = load_paper_credentials()
    provider = AlpacaMarketDataProvider(key, secret)
    sectors = {SECTOR_BY_SYMBOL[symbol] for symbol in symbols if symbol in SECTOR_BY_SYMBOL}
    sector_etfs = sorted({
        SECTOR_ETF_BY_SECTOR[sector]
        for sector in sectors
        if sector in SECTOR_ETF_BY_SECTOR
    })
    requested_symbols = list(dict.fromkeys([*symbols, "SPY", *sector_etfs]))
    histories = provider.get_bars(requested_symbols, start, end)
    benchmark = histories.pop("SPY", None)
    sector_benchmarks = {etf: histories.pop(etf, None) for etf in sector_etfs}
    sector_benchmarks = {etf: bars for etf, bars in sector_benchmarks.items() if bars is not None}
    missing = sorted(set(symbols) - histories.keys())
    if missing:
        print(f"No Alpaca daily bars returned for: {', '.join(missing)}")
    if not histories:
        raise ValueError("Alpaca returned no history for the requested symbols")
    spread_scenarios = [spread_bps] if spread_scenarios_bps is None else spread_scenarios_bps
    if not spread_scenarios or any(not math.isfinite(value) or value < 0 for value in spread_scenarios):
        raise ValueError("Spread scenarios must contain non-negative basis-point values")
    strategy_cache = {}
    results = [
        (scenario_spread, run_backtest(
            histories,
            cfg,
            slippage_bps=slippage_bps,
            spread_bps=scenario_spread,
            commission_per_share=commission_per_share,
            target_fill_mode=target_fill_mode,
            target_trade_through_bps=target_trade_through_bps,
            strategy_cache=strategy_cache,
            benchmark=benchmark,
            sector_benchmarks=sector_benchmarks,
        ))
        for scenario_spread in spread_scenarios
    ]
    run_ids = persist_sensitivity_results(
        results,
        symbols=list(histories),
        start=start,
        end=end,
        slippage_bps=slippage_bps,
        commission_per_share=commission_per_share,
        target_fill_mode=target_fill_mode,
        target_trade_through_bps=target_trade_through_bps,
    )
    report = format_spread_sensitivity_report(
        results,
        run_ids=run_ids,
        database_path=str(DEFAULT_BACKTEST_DB),
        symbols=list(histories),
        start=start,
        end=end,
        slippage_bps=slippage_bps,
        commission_per_share=commission_per_share,
        target_fill_mode=target_fill_mode,
        target_trade_through_bps=target_trade_through_bps,
    )
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    report_path = REPORT_DIR / f"backtest_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")
    print(report, end="")
    print(f"Persisted {len(run_ids)} run(s) to {DEFAULT_BACKTEST_DB}")
    print(f"Saved backtest report to {report_path}")
    return results


@contextmanager
def bot_lock(path=BOT_LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+b") as lock_file:
        lock_file.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another paper bot process is running") from exc
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Another paper bot process is running") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


GUI_COLUMN_LABELS = [
    "Rank", "Symbol", "Score", "P(+10%)", "P(-5%)",
    "Risk", "Entry", "Stop", "+1% Target",
    "Target 1 (+8-10%)", "Target 2 (+16-20%)",
    "Shares", "Max Risk", "Catalyst", "Why Attractive",
]


def candidate_gui_values(candidate, rank):
    return {
        "Rank": str(rank),
        "Symbol": candidate.symbol,
        "Score": str(candidate.score),
        "P(+10%)": f"{candidate.prob_gain_10d:.1%}",
        "P(-5%)": f"{candidate.prob_loss_5d:.1%}",
        "Risk": candidate.risk_rating,
        "Entry": f"${candidate.entry:.2f}",
        "Stop": f"${candidate.stop:.2f}",
        "+1% Target": f"${candidate.target_1pct:.2f}",
        "Target 1 (+8-10%)": f"${candidate.target_1:.2f}",
        "Target 2 (+16-20%)": f"${candidate.target_2:.2f}",
        "Shares": str(candidate.shares),
        "Max Risk": f"${candidate.risk_dollars:.2f}",
        "Catalyst": candidate.main_catalyst,
        "Why Attractive": "; ".join(candidate.reasons),
    }


def candidate_info_rows(candidate, rank):
    values = candidate_gui_values(candidate, rank)
    return [
        (column, values[column])
        for column in GUI_COLUMN_LABELS
        if column not in {"Rank", "Symbol"}
    ]


def format_candidate_info_line(label, value):
    return f"{label}: {value}"


def save_gui_run_cache(
    candidates: list[TradeCandidate],
    cache_dir: str | Path = GUI_RUN_CACHE_DIR,
    run_at: datetime | None = None,
) -> Path:
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    run_at = run_at or datetime.now().astimezone()
    timestamp = run_at.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    cache_path = cache_root / f"gui_scan_{timestamp}.json"
    temporary_path = cache_path.with_suffix(".tmp")
    payload = {
        "run_at": run_at.isoformat(),
        "candidates": [asdict(candidate) for candidate in candidates],
    }

    try:
        temporary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(cache_path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
    return cache_path


def load_latest_gui_run_cache(
    cache_dir: str | Path = GUI_RUN_CACHE_DIR,
) -> tuple[list[TradeCandidate], datetime] | None:
    cache_files = sorted(Path(cache_dir).glob("gui_scan_*.json"))
    if not cache_files:
        return None

    cache_path = cache_files[-1]
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        run_at = datetime.fromisoformat(payload["run_at"])
        candidates = [TradeCandidate(**item) for item in payload["candidates"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Unable to load cached GUI scan {cache_path}: {exc}") from exc
    return candidates, run_at


def update_candidate_gui_fields(row_fields, candidates):
    for rank, fields in enumerate(row_fields, start=1):
        values = {label: "" for label in GUI_COLUMN_LABELS}
        if rank <= len(candidates):
            values.update(candidate_gui_values(candidates[rank - 1], rank))
        fields["Rank"].text = values["Rank"]
        fields["Rank"].disabled = not bool(values["Rank"])
        for label, field in fields.items():
            if label == "Rank":
                continue
            field.text = values[label]


def quarter_dollar_price_ticks(prices):
    low = math.floor(min(prices) * 4) / 4
    high = math.ceil(max(prices) * 4) / 4
    if high == low:
        low -= 0.25
        high += 0.25
    count = int(round((high - low) * 4))
    return [low + index * 0.25 for index in range(count + 1)]


def simple_moving_average_points(prices, window=200):
    if window <= 0:
        raise ValueError("Moving-average window must be positive")
    if len(prices) < window:
        return []

    rolling_total = sum(prices[:window])
    points = [(window - 1, rolling_total / window)]
    for index in range(window, len(prices)):
        rolling_total += prices[index] - prices[index - window]
        points.append((index, rolling_total / window))
    return points


def monthly_price_markers(dates):
    markers = []
    previous_month = None
    for index, value in enumerate(dates):
        month = (value.year, value.month)
        if month != previous_month:
            markers.append((index, value.strftime("%b")))
            previous_month = month
    return markers


def monthly_price_label_slots(dates, left, width, label_width=24):
    markers = monthly_price_markers(dates)
    if not markers or width <= 0:
        return []

    repeated_boundary_month = (
        len(markers) > 1 and markers[0][1] == markers[-1][1]
    )
    if repeated_boundary_month:
        markers = markers[1:]

    label_width = min(label_width, width)
    half_label_width = label_width / 2
    last_index = max(1, len(dates) - 1)
    right = left + width
    marker_positions = [
        left + width * date_index / last_index
        for date_index, _month_name in markers
    ]
    labels = []
    for marker_number, (date_index, month_name) in enumerate(markers):
        is_right_boundary = (
            repeated_boundary_month and marker_number == len(markers) - 1
        )
        if is_right_boundary:
            center = right - half_label_width
        else:
            segment_start = marker_positions[marker_number]
            segment_end = (
                marker_positions[marker_number + 1]
                if marker_number + 1 < len(markers)
                else right
            )
            center = (segment_start + segment_end) / 2
            center = min(
                max(center, left + half_label_width),
                right - half_label_width,
            )
        while labels and center - (labels[-1][1] + half_label_width) < label_width:
            if not is_right_boundary:
                break
            labels.pop()
        if labels and center - (labels[-1][1] + half_label_width) < label_width:
            continue
        labels.append((month_name, center - half_label_width, label_width))
    return labels


def adjacent_symbol_index(current_index, direction, symbol_count):
    next_index = current_index + direction
    if 0 <= next_index < symbol_count:
        return next_index
    return None


def gui_result_row_height(window_height, row_count, spacing=2, padding=2):
    available_height = window_height * (23 / 25)
    grid_overhead = (2 * padding) + (spacing * max(0, row_count - 1))
    return max(1, (available_height - grid_overhead) / row_count)


def launch_gui(cfg, symbols=None, output_path=None, top=25):
    import os
    from threading import Thread

    from kivy.config import Config

    if os.name == "nt":
        Config.set("graphics", "multisamples", "0")

    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.graphics import Color, Line
    from kivy.uix.button import Button
    from kivy.uix.floatlayout import FloatLayout
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.popup import Popup
    from kivy.uix.progressbar import ProgressBar
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.uix.widget import Widget

    if os.name == "nt":
        Window.fullscreen = True

    font_size = int(Window.size[0] / 120)

    class DailyPriceGraph(Widget):
        def __init__(self, dates, prices, visible_start_index=0, **kwargs):
            super().__init__(**kwargs)
            self.dates = dates
            self.prices = prices
            self.visible_start_index = visible_start_index
            self.axis_labels = []
            self.bind(pos=self.redraw, size=self.redraw)
            self.redraw()

        def redraw(self, *_):
            self.canvas.clear()
            for label in self.axis_labels:
                self.remove_widget(label)
            self.axis_labels.clear()
            if len(self.prices) < 2:
                return

            visible_start_index = min(
                max(0, self.visible_start_index),
                len(self.prices) - 1,
            )
            visible_prices = self.prices[visible_start_index:]
            visible_dates = self.dates[visible_start_index:]
            if len(visible_prices) < 2:
                return
            sma_data = [
                (index - visible_start_index, average)
                for index, average in simple_moving_average_points(self.prices)
                if index >= visible_start_index
            ]
            left = self.x + self.width * 0.04
            bottom = self.y + self.height * 0.12
            right_margin = self.width * 0.12
            graph_width = self.width - (left - self.x) - right_margin
            graph_height = self.height * 0.78
            graph_prices = [
                *visible_prices,
                *(average for _, average in sma_data),
            ]
            ticks = quarter_dollar_price_ticks(graph_prices)
            low = ticks[0]
            high = ticks[-1]

            points = []
            last_index = len(visible_prices) - 1
            for index, price in enumerate(visible_prices):
                x = left + graph_width * index / last_index
                y = bottom + graph_height * (price - low) / (high - low)
                points.extend((x, y))

            sma_points = []
            for index, average in sma_data:
                x = left + graph_width * index / last_index
                y = bottom + graph_height * (average - low) / (high - low)
                sma_points.extend((x, y))

            with self.canvas:
                Color(0.65, 0.65, 0.65, 1)
                Line(
                    rectangle=(left, bottom, graph_width, graph_height),
                    width=1,
                )
                for tick in ticks:
                    y = bottom + graph_height * (tick - low) / (high - low)
                    Line(points=[left, y, left + graph_width, y], width=0.5)
                Color(0.15, 0.55, 0.95, 1)
                Line(points=points, width=1.5)
                if len(sma_points) >= 4:
                    Color(1, 1, 1, 1)
                    Line(points=sma_points, width=1.5)
                Color(0.65, 0.65, 0.65, 1)
                for index, _month in monthly_price_markers(visible_dates):
                    x = left + graph_width * index / last_index
                    Line(points=[x, bottom - 3, x, bottom + 3], width=1)

            marker_font = max(8, font_size / 2)
            marker_height = marker_font * 1.5
            tick_spacing = graph_height * 0.25 / (high - low)
            label_stride = max(1, math.ceil(marker_height / tick_spacing))
            labeled_ticks = list(ticks[::label_stride])
            if ticks[-1] not in labeled_ticks:
                labeled_ticks.append(ticks[-1])
            for tick in labeled_ticks:
                y = bottom + graph_height * (tick - low) / (high - low)
                label = Label(
                    text=f"${tick:.2f}",
                    font_size=marker_font,
                    size_hint=(None, None),
                    size=(right_margin, marker_height),
                    pos=(left + graph_width, y - marker_height / 2),
                )
                self.add_widget(label)
                self.axis_labels.append(label)

            for month_name, slot_left, slot_width in monthly_price_label_slots(
                visible_dates,
                left,
                graph_width,
                label_width=marker_font * 2.5,
            ):
                label = Label(
                    text=month_name,
                    font_size=marker_font,
                    halign="center",
                    valign="middle",
                    size_hint=(None, None),
                    size=(slot_width, marker_height),
                    pos=(slot_left, self.y),
                )
                label.bind(size=label.setter("text_size"))
                self.add_widget(label)
                self.axis_labels.append(label)

    class SwingTraderGUI(App):
        def build(self):
            self.scan_thread = None
            self.row_fields = []
            self.displayed_candidates = []
            root = FloatLayout()

            header = GridLayout(
                cols=len(GUI_COLUMN_LABELS),
                spacing=2,
                size_hint=(1, 1 / 25),
                pos_hint={"top": 1},
            )
            for column in GUI_COLUMN_LABELS:
                header.add_widget(Label(text=column, font_size=font_size))
            root.add_widget(header)

            scroll_view = ScrollView(
                pos_hint={"top": 24 / 25},
                size_hint=(1, 23 / 25),
            )
            rows = GridLayout(cols=1, spacing=2, padding=2, size_hint_y=None)
            rows.bind(minimum_height=rows.setter("height"))
            row_count = max(1, top) if top > 0 else len(FALLBACK_SYMBOLS)
            row_height = gui_result_row_height(Window.size[1], row_count)
            for _ in range(row_count):
                row = GridLayout(
                    cols=len(GUI_COLUMN_LABELS),
                    spacing=2,
                    size_hint_y=None,
                    height=row_height,
                )
                fields = {}
                for column in GUI_COLUMN_LABELS:
                    if column == "Rank":
                        field = Button(
                            text="",
                            font_size=font_size,
                            size_hint_y=None,
                            height=row_height,
                            disabled=True,
                        )
                        field.bind(
                            on_release=lambda _button, row_fields=fields:
                                self.show_symbol_info(row_fields)
                        )
                    else:
                        field = TextInput(
                            text="",
                            multiline=False,
                            font_size=font_size,
                            size_hint_y=None,
                            height=row_height,
                            readonly=True,
                            write_tab=False,
                        )
                    fields[column] = field
                    row.add_widget(field)
                self.row_fields.append(fields)
                rows.add_widget(row)
            scroll_view.add_widget(rows)
            root.add_widget(scroll_view)

            self.scan_button = Button(
                text="Run Scan",
                font_size=font_size,
                size_hint=(1 / 15, 1 / 25),
                pos_hint={"x": 1 / 15, "y": 0},
            )
            self.scan_button.bind(on_release=self.run_scan)
            root.add_widget(self.scan_button)
            self.scan_status = Label(
                text="Ready",
                font_size=font_size,
                halign="left",
                valign="middle",
                size_hint=(7 / 15, 1 / 25),
                pos_hint={"x": 2 / 15, "y": 0},
            )
            root.add_widget(self.scan_status)
            self.scan_progress = ProgressBar(
                max=100,
                value=0,
                size_hint=(5 / 15, 1 / 25),
                pos_hint={"x": 9 / 15, "y": 0},
            )
            root.add_widget(self.scan_progress)
            exit_button = Button(
                text="Exit",
                font_size=font_size,
                size_hint=(1 / 15, 1 / 25),
                pos_hint={"x": 14 / 15, "y": 0},
            )
            exit_button.bind(on_release=self.exit_app)
            root.add_widget(exit_button)
            try:
                cached_run = load_latest_gui_run_cache()
            except ValueError as exc:
                self.scan_status.text = f"Could not load saved scan: {exc}"
            else:
                if cached_run is not None:
                    candidates, run_at = cached_run
                    self._display_candidates(
                        candidates,
                        status_text=f"Loaded saved scan from {run_at:%Y-%m-%d %H:%M:%S %Z}.",
                    )
            return root

        def exit_app(self, *_):
            Clock.schedule_once(lambda _dt: self.stop(), 0)

        def show_symbol_info(self, row_fields):
            try:
                current_index = int(row_fields["Rank"].text) - 1
            except ValueError:
                return
            if not 0 <= current_index < len(self.displayed_candidates):
                return

            content = FloatLayout()
            symbol_label = Label(
                text="",
                font_size=font_size,
                halign="left",
                valign="top",
                size_hint=(0.8, 0.2),
                pos_hint={"x": 0.04, "top": 0.98},
            )
            symbol_label.bind(size=symbol_label.setter("text_size"))
            content.add_widget(symbol_label)
            info_rows = candidate_info_rows(
                self.displayed_candidates[current_index],
                current_index + 1,
            )
            details = GridLayout(
                cols=1,
                rows=len(info_rows),
                spacing=1,
                padding=0,
                size_hint=(0.42, 0.58),
                pos_hint={"x": 0.04, "y": 0.37},
                row_force_default=True,
                row_default_height=Window.height * 0.58 / len(info_rows),
            )
            detail_values = {}
            for label_text, value_text in info_rows:
                value_label = Label(
                    text=format_candidate_info_line(label_text, value_text),
                    font_size=font_size,
                    halign="left",
                    valign="middle",
                )
                value_label.bind(size=value_label.setter("text_size"))
                details.add_widget(value_label)
                detail_values[label_text] = value_label
            content.add_widget(details)
            graph_title = Label(
                text="Daily Price Graph",
                font_size=font_size,
                halign="center",
                valign="middle",
                size_hint=(0.5, 0.06),
                pos_hint={"x": 0.5, "top": 0.98},
            )
            content.add_widget(graph_title)
            graph_area = FloatLayout(
                size_hint=(0.5, 0.48),
                pos_hint={"x": 0.5, "y": 0.48},
            )
            content.add_widget(graph_area)

            def display_candidate(index):
                candidate = self.displayed_candidates[index]
                symbol_label.text = candidate.symbol
                for label_text, value_text in candidate_info_rows(candidate, index + 1):
                    detail_values[label_text].text = format_candidate_info_line(
                        label_text,
                        value_text,
                    )
                graph_area.clear_widgets()
                try:
                    cached_prices = load_cached_daily_close(
                        candidate.symbol,
                        include_sma_warmup=True,
                    )
                except ValueError as exc:
                    graph_message = f"Unable to load cached price data: {exc}"
                else:
                    if cached_prices is None or cached_prices.empty:
                        graph_message = f"No cached daily price data for {candidate.symbol}."
                    elif len(cached_prices) < 2:
                        graph_message = f"Not enough cached daily prices to graph {candidate.symbol}."
                    else:
                        visible_cutoff = (
                            cached_prices.index[-1] - pd.Timedelta(days=365)
                        )
                        visible_start_index = cached_prices.index.searchsorted(
                            visible_cutoff
                        )
                        prices = [float(price) for price in cached_prices]
                        graph_area.add_widget(
                            DailyPriceGraph(
                                list(cached_prices.index),
                                prices,
                                visible_start_index=visible_start_index,
                                size_hint=(1, 1),
                                pos_hint={"x": 0, "y": 0},
                            )
                        )
                        graph_message = ""
                if graph_message:
                    graph_status = Label(
                        text=graph_message,
                        font_size=font_size / 2,
                        halign="center",
                        valign="middle",
                        size_hint=(1, 1),
                        pos_hint={"x": 0, "y": 0},
                    )
                    graph_status.bind(size=graph_status.setter("text_size"))
                    graph_area.add_widget(graph_status)

            display_candidate(current_index)
            popup = Popup(
                title="Symbol Info Window",
                title_size=font_size,
                content=content,
                size_hint=(1, 1),
                auto_dismiss=False,
            )
            popup_exit_button = Button(
                text="Exit",
                font_size=font_size,
                size_hint=(0.05, 0.0375),
                pos_hint={"right": 0.98, "y": 0.02},
            )
            popup_exit_button.bind(on_release=lambda *_: popup.dismiss())
            content.add_widget(popup_exit_button)

            selected_index = [current_index]

            def on_popup_key_down(_window, key, *_args):
                direction = {273: -1, 274: 1}.get(key)
                if direction is None:
                    return False
                next_index = adjacent_symbol_index(
                    selected_index[0],
                    direction,
                    len(self.displayed_candidates),
                )
                if next_index is not None:
                    selected_index[0] = next_index
                    display_candidate(next_index)
                return True

            def unbind_popup_keys(_popup, *_args):
                Window.unbind(on_key_down=on_popup_key_down)

            Window.bind(on_key_down=on_popup_key_down)
            popup.bind(on_dismiss=unbind_popup_keys)
            popup.open()

        def run_scan(self, *_):
            if self.scan_thread is not None and self.scan_thread.is_alive():
                return

            self.scan_button.disabled = True
            self.scan_progress.value = 0
            self.scan_status.text = "Starting scan..."
            self.scan_thread = Thread(target=self._scan_in_background, daemon=True)
            self.scan_thread.start()

        def _scan_in_background(self):
            try:
                candidates = run_scanner(
                    symbols=symbols,
                    output_path=output_path,
                    top=top,
                    cfg=cfg,
                    progress_callback=self._schedule_scan_progress,
                )
            except Exception as exc:
                error = str(exc)
                Clock.schedule_once(lambda _dt: self._show_scan_error(error))
                return

            try:
                cache_path = save_gui_run_cache(candidates)
            except OSError as exc:
                error = str(exc)
                Clock.schedule_once(
                    lambda _dt: self._display_candidates(
                        candidates,
                        status_text=f"Scan complete: {len(candidates)} candidates; cache save failed: {error}",
                    )
                )
                return

            Clock.schedule_once(
                lambda _dt: self._display_candidates(
                    candidates,
                    status_text=f"Scan complete: {len(candidates)} candidates. Saved {cache_path.name}.",
                )
            )

        def _schedule_scan_progress(self, value, message):
            Clock.schedule_once(
                lambda _dt: self._update_scan_progress(value, message)
            )

        def _update_scan_progress(self, value, message):
            self.scan_progress.value = value
            self.scan_status.text = message

        def _show_scan_error(self, error):
            self.scan_status.text = f"Scan failed: {error}"
            self.scan_button.disabled = False

        def _display_candidates(self, candidates, status_text=None):
            self.displayed_candidates = list(candidates)
            update_candidate_gui_fields(self.row_fields, candidates)
            self.scan_progress.value = 100
            self.scan_status.text = status_text or f"Scan complete: {len(candidates)} candidates."
            self.scan_button.disabled = False

    SwingTraderGUI().run()


def create_argument_parser():
    parser = argparse.ArgumentParser(description="Consistent Swing Trading Scanner for NYSE & NASDAQ")
    interface_mode = parser.add_mutually_exclusive_group()
    interface_mode.add_argument("--gui", action="store_true", help="Open the Kivy scanner interface")
    interface_mode.add_argument("--cli", action="store_true", help="Run the command-line scanner")
    parser.add_argument("--symbols", nargs="+", help="Optional specific ticker symbols to scan")
    parser.add_argument("--output", help="Path for JSON scan snapshot output")
    parser.add_argument("--top", type=int, default=25, help="Number of top candidates to return (default 25)")
    parser.add_argument("--min-price", type=float, help="Minimum stock price (default $5.00)")
    parser.add_argument("--max-price", type=float, help="Maximum stock price (default $30.00)")
    parser.add_argument("--backtest", action="store_true", help="Run a historical daily-bar backtest")
    parser.add_argument("--start", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage per fill in basis points")
    parser.add_argument("--spread-bps", type=float, default=5.0, help="Full bid-ask spread in basis points")
    parser.add_argument(
        "--spread-scenarios-bps",
        nargs="+",
        type=float,
        help="Run multiple full-spread scenarios against the same downloaded bars",
    )
    parser.add_argument(
        "--commission-per-share",
        type=float,
        default=0.0,
        help="Commission charged per share on each side of a trade",
    )
    parser.add_argument(
        "--target-fill-mode",
        choices=("touch", "trade_through"),
        default="trade_through",
        help="Target limit fill policy (default requires a trade-through)",
    )
    parser.add_argument(
        "--target-trade-through-bps",
        type=float,
        default=5.0,
        help="Required price trade-through for target fills (default 5 bps)",
    )
    paper_modes = parser.add_mutually_exclusive_group()
    paper_modes.add_argument("--paper-preview", action="store_true", help="Check top candidate against Alpaca paper quotes without placing an order")
    paper_modes.add_argument("--paper-trade", action="store_true", help="Submit one Alpaca paper bracket order for the top candidate")
    paper_modes.add_argument("--paper-status", action="store_true", help="Inspect Alpaca paper positions and orders without changing them")
    paper_modes.add_argument("--paper-reconcile", action="store_true", help="Inspect paper orders and request cancellation of stale unfilled bot entries")
    paper_modes.add_argument("--paper-bot", action="store_true", help="Reconcile continuously and rescan for open paper slots")
    paper_modes.add_argument("--paper-bot-once", action="store_true", help="Run one guarded paper bot cycle and exit")
    return parser


def main(argv=None):
    started_at = perf_counter()
    arguments = sys.argv[1:] if argv is None else argv
    parser = create_argument_parser()
    args = parser.parse_args(arguments)
    if args.gui or not arguments:
        if (
            args.backtest
            or args.paper_preview
            or args.paper_trade
            or args.paper_status
            or args.paper_reconcile
            or args.paper_bot
            or args.paper_bot_once
        ):
            parser.error("--gui cannot be combined with backtest or paper trading options")
        if args.top <= 0:
            parser.error("--gui requires --top to be greater than zero")
        launch_gui(
            cfg=build_config(minimum_price=args.min_price, maximum_price=args.max_price),
            symbols=args.symbols,
            output_path=args.output,
            top=args.top,
        )
        return

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    suffix = 0
    while True:
        report_path = REPORT_DIR / f"scan_{timestamp}{f'_{suffix}' if suffix else ''}.md"
        try:
            report = report_path.open("x", encoding="utf-8")
            break
        except FileExistsError:
            suffix += 1

    with report, redirect_stdout(ReportOutput(sys.stdout, report)):
        print(f"# Swing Trader Scan - {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        try:
            if args.backtest:
                if not args.start or not args.end:
                    parser.error("--backtest requires both --start and --end")
                if date.fromisoformat(args.start) > date.fromisoformat(args.end):
                    parser.error("--start must not be later than --end")
                config = build_config(minimum_price=args.min_price, maximum_price=args.max_price)
                run_backtest_cli(
                    args.symbols,
                    args.start,
                    args.end,
                    config,
                    slippage_bps=args.slippage_bps,
                    spread_bps=args.spread_bps,
                    spread_scenarios_bps=args.spread_scenarios_bps,
                    commission_per_share=args.commission_per_share,
                    target_fill_mode=args.target_fill_mode,
                    target_trade_through_bps=args.target_trade_through_bps,
                )
            elif args.paper_status or args.paper_reconcile:
                from swingtrader.paper_trading import report_paper_status

                report_paper_status(cancel_stale=args.paper_reconcile)
            elif args.paper_bot or args.paper_bot_once:
                config = build_config(minimum_price=args.min_price, maximum_price=args.max_price)
                with bot_lock():
                    run_paper_bot(config, symbols=args.symbols, top=args.top, once=args.paper_bot_once)
            else:
                config = build_config(
                    minimum_price=args.min_price,
                    maximum_price=args.max_price,
                )
                candidates = run_scanner(
                    symbols=args.symbols,
                    output_path=args.output,
                    top=args.top,
                    cfg=config,
                )
                if args.paper_preview or args.paper_trade:
                    if not candidates:
                        print("No candidate available for a paper order.")
                    else:
                        from swingtrader.paper_trading import submit_paper_candidate

                        try:
                            plan = submit_paper_candidate(candidates[0], config, execute=args.paper_trade)
                            print(f"\n## Alpaca Paper {'Order' if args.paper_trade else 'Preview'}\n")
                            print(f"{plan.symbol}: buy {plan.shares} @ <= ${plan.limit_price:.2f}; "
                                  f"stop ${plan.stop_price:.2f}; take profit ${plan.target_price:.2f}; "
                                  f"planned risk ${plan.risk_dollars:.2f}")
                        except ValueError as exc:
                            print(f"\nPaper order skipped: {exc}")
        finally:
            elapsed = perf_counter() - started_at
            print(f"\nTotal application runtime: {elapsed:.2f} seconds")
    print(f"Saved Markdown report to {report_path}")


if __name__ == "__main__":
    main()
