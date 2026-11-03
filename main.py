import argparse
import importlib.util
import json
import logging
import math
import sys
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from time import perf_counter, sleep
from zoneinfo import ZoneInfo

from swingtrader.config import TradingConfig, load_trading_config
from swingtrader.data import (
    download_batch,
    download_latest_prices,
    fetch_market_candidates,
)
from swingtrader.market_data import AlpacaMarketDataProvider, YFinanceMarketDataProvider
from swingtrader.scanner import analyze, refresh_candidate_price


FALLBACK_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL",
    "AVGO", "AMD", "NFLX", "TSLA", "PLTR", "ORCL",
    "CRM", "MU", "QCOM", "COST", "JPM", "XOM",
    "SOFI", "F", "PFE", "INTC", "SNAP", "RIVN",
    "NU", "VALE", "T", "BAC", "GM", "CCL", "NCLH", "DKNG",
    "HOOD", "MARA", "RKLB", "SIRI", "TGT", "AES", "ABUS", "AGRO", "AESI"
]

REPORT_DIR = Path("data") / "scans"
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


def run_scanner(symbols=None, output_path=None, top=25, cfg=None):
    """Run the consistent swing trading scanner across NYSE & NASDAQ candidates."""
    cfg = cfg or TradingConfig()

    if not symbols:
        symbols = fetch_market_candidates(
            minimum_price=cfg.minimum_price,
            maximum_price=cfg.maximum_price,
            minimum_volume=cfg.minimum_average_volume,
        ) or FALLBACK_SYMBOLS

    print(f"Ingesting 1-year OHLCV bars for {len(symbols)} liquid ${cfg.minimum_price:.2f}-${cfg.maximum_price:.2f} candidates...")
    market_data = YFinanceMarketDataProvider(
        batch_loader=download_batch,
        latest_price_loader=download_latest_prices,
    )
    historical_data = market_data.get_bars(symbols, period="1y")

    candidates = []
    for symbol in symbols:
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
        latest_prices = market_data.get_latest_prices([candidate.symbol for candidate in candidates])
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
    candidates.sort(key=lambda x: (x.prob_gain_10d, x.score, x.reward_risk), reverse=True)

    if top and top > 0:
        candidates = candidates[:top]

    if output_path is None:
        output_path = Path("data") / "scans" / f"scan_{date.today().isoformat()}.json"

    save_scan(candidates, symbols, output_path)

    if not candidates:
        print("\nNo candidates met the full swing criteria today.")
        return []

    print_candidates_table(candidates, cfg=cfg)
    return candidates


def run_paper_bot_cycle(cfg, symbols=None, top=25, *, scan=run_scanner, now=None):
    from swingtrader.paper_trading import (
        bot_attempted_today, bot_capacity, check_bot_daily_halt, paper_bot_snapshot,
        review_paper_orders,
        submit_paper_candidate,
    )
    from swingtrader.paper_journal import add_event, synchronize_journal
    from swingtrader.paper_recovery import reconcile_paper_risk

    now = now or datetime.now(timezone.utc)
    trading, orders = paper_bot_snapshot()
    account = trading.get_account()
    positions = trading.get_all_positions()
    new_fills = synchronize_journal(account, positions, orders, now)
    if new_fills:
        print(f"Recorded {new_fills} new or updated paper fills in the local ledger")
    if cfg.bot_auto_recover:
        if not reconcile_paper_risk(trading, positions, orders, now, cfg):
            print("Paper bot recovering broker state; no new entries this cycle")
            return False
    else:
        messages = review_paper_orders(trading, orders, now, cancel_stale=True)
        for message in messages:
            print(message)
        if any("manual review" in message or "cancellation requested" in message for message in messages):
            print("Paper bot paused: verify orders before any new entries")
            return False
    try:
        check_bot_daily_halt(account, cfg, now)
        capacity = bot_capacity(account, positions, orders, cfg, now)
    except ValueError as exc:
        print(f"Paper bot paused: {exc}")
        add_event("WARNING", str(exc))
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
    from swingtrader.paper_journal import FILLS_PATH, add_event

    if importlib.util.find_spec("alpaca") is None:
        raise RuntimeError(
            f"alpaca-py is not installed in {sys.executable}; run the bot with .\\.venv\\Scripts\\python.exe"
        )
    if cfg.bot_poll_seconds < 30:
        raise ValueError("Paper bot polling interval must be at least 30 seconds")
    if cfg.bot_scan_interval_seconds < cfg.bot_poll_seconds:
        raise ValueError("Paper bot scan interval must be at least the polling interval")
    if not isinstance(cfg.bot_auto_recover, bool) or not isinstance(cfg.bot_max_hold_days, int) or cfg.bot_max_hold_days < 1:
        raise ValueError("Invalid paper bot automatic recovery or maximum holding period")
    logger = logging.getLogger("swingtrader.paper_bot")
    if not logger.handlers:
        FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(FILLS_PATH.with_name("paper_bot.log"), maxBytes=2_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    last_scan_at = None
    while True:
        now = perf_counter()
        try:
            scan = run_scanner if last_scan_at is None or now - last_scan_at >= cfg.bot_scan_interval_seconds else None
            if run_paper_bot_cycle(cfg, symbols, top, scan=scan):
                last_scan_at = perf_counter()
            logger.info("Paper bot cycle complete")
        except Exception as exc:
            print(f"Paper bot paused this cycle: {exc}")
            logger.exception("Paper bot cycle failed; no further entries this cycle")
            add_event("ERROR", str(exc))
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
    holdout_start=None,
    walk_forward_months=None,
    monte_carlo_simulations=1000,
    monte_carlo_drawdown_threshold=-0.20,
    universe_file=None,
    experiment_file=None,
):
    from swingtrader.backtest import (
        SECTOR_BY_SYMBOL,
        SECTOR_ETF_BY_SECTOR,
        build_walk_forward_windows,
        format_spread_sensitivity_report,
        run_backtest,
    )
    from swingtrader.backtest_store import DEFAULT_BACKTEST_DB, persist_sensitivity_results
    from swingtrader.experiments import load_config_experiments
    from swingtrader.historical_universe import load_historical_universe
    from swingtrader.market_data import (
        AlpacaMarketDataProvider,
        audit_daily_coverage,
        format_data_coverage_report,
        validate_daily_history,
    )
    from swingtrader.paper_trading import load_paper_credentials

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError("Backtest start date cannot be later than end date")
    if bool(symbols) == bool(universe_file):
        raise ValueError("Provide exactly one of explicit --symbols or --universe-file")
    historical_universe = load_historical_universe(universe_file) if universe_file else None
    if historical_universe is not None:
        symbols = historical_universe.symbols_overlapping(
            start_date, end_date,
        )
        if not symbols:
            raise ValueError("Historical universe contains no members overlapping the requested backtest period")
    if holdout_start is not None and not (
        date.fromisoformat(start) < date.fromisoformat(holdout_start) <= date.fromisoformat(end)
    ):
        raise ValueError("--holdout-start must be later than --start and no later than --end")
    if walk_forward_months is not None:
        if holdout_start is None:
            raise ValueError("--walk-forward-months requires --holdout-start")
        if walk_forward_months < 1:
            raise ValueError("--walk-forward-months must be at least 1")
    if experiment_file is not None and (holdout_start is not None or walk_forward_months is not None):
        raise ValueError("Config experiments cannot be combined with holdout or walk-forward evaluation")
    if (
        isinstance(monte_carlo_simulations, bool)
        or not isinstance(monte_carlo_simulations, int)
        or monte_carlo_simulations < 1
    ):
        raise ValueError("--monte-carlo-simulations must be at least 1")
    if not math.isfinite(monte_carlo_drawdown_threshold) or not -1 <= monte_carlo_drawdown_threshold <= 0:
        raise ValueError("--monte-carlo-drawdown-threshold must be between -1 and 0")
    key, secret = load_paper_credentials()
    provider = AlpacaMarketDataProvider(key, secret)
    sectors = {SECTOR_BY_SYMBOL[symbol] for symbol in symbols if symbol in SECTOR_BY_SYMBOL}
    sector_etfs = sorted({
        SECTOR_ETF_BY_SECTOR[sector]
        for sector in sectors
        if sector in SECTOR_ETF_BY_SECTOR
    })
    requested_symbols = list(dict.fromkeys([*symbols, "SPY", *sector_etfs]))
    histories = provider.get_bars(requested_symbols, start=start, end=end)
    membership = historical_universe.membership if historical_universe else None
    for warning in validate_daily_history(
        histories, requested_symbols, start, end, membership=membership,
    ):
        print(f"Market-data quality warning: {warning}")
    coverage = audit_daily_coverage(
        histories,
        symbols,
        start,
        end,
        membership=membership,
    )
    for item in coverage:
        if item.missing_sessions:
            print(
                f"Historical coverage warning: {item.symbol} returned "
                f"{item.returned_sessions}/{item.expected_sessions} expected member sessions"
            )
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
    config_experiments = (
        load_config_experiments(experiment_file, cfg) if experiment_file is not None else None
    )
    strategy_cache = {}
    experiment_name = None

    def run_period(period_start, period_end, *, simulation_start=None, simulation_end=None):
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
                simulation_start=simulation_start,
                simulation_end=simulation_end,
                universe_membership=(
                    historical_universe.membership if historical_universe is not None else None
                ),
            ))
            for scenario_spread in spread_scenarios
        ]
        run_ids = persist_sensitivity_results(
            results,
            symbols=list(symbols),
            start=period_start,
            end=period_end,
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            target_fill_mode=target_fill_mode,
            target_trade_through_bps=target_trade_through_bps,
            universe_source=historical_universe.source_path if historical_universe else None,
            universe_sha256=historical_universe.sha256 if historical_universe else None,
            universe_membership_json=historical_universe.to_json() if historical_universe else "{}",
            universe_record_count=historical_universe.record_count if historical_universe else 0,
            experiment_name=experiment_name,
        )
        report = format_spread_sensitivity_report(
            results,
            run_ids=run_ids,
            database_path=str(DEFAULT_BACKTEST_DB),
            symbols=list(symbols),
            start=period_start,
            end=period_end,
            slippage_bps=slippage_bps,
            commission_per_share=commission_per_share,
            target_fill_mode=target_fill_mode,
            target_trade_through_bps=target_trade_through_bps,
            monte_carlo_simulations=monte_carlo_simulations,
            monte_carlo_drawdown_threshold=monte_carlo_drawdown_threshold,
            universe_source=historical_universe.source_path if historical_universe else None,
            universe_sha256=historical_universe.sha256 if historical_universe else None,
            universe_record_count=historical_universe.record_count if historical_universe else 0,
            experiment_name=experiment_name,
        )
        return results, report, run_ids

    if config_experiments is not None:
        experiment_sections = []
        all_run_ids = []
        results = []
        for experiment_name, experiment_config in config_experiments:
            cfg = experiment_config
            experiment_results, experiment_report, experiment_run_ids = run_period(start, end)
            results = experiment_results
            all_run_ids.extend(experiment_run_ids)
            experiment_sections.extend([
                f"## Experiment: {experiment_name}",
                "",
                experiment_report.replace(
                    "# Swing Trader Execution-Cost Sensitivity",
                    "### Execution-Cost Sensitivity",
                    1,
                ),
                "",
            ])
        report = "\n".join([
            "# Strategy Configuration Experiments",
            "",
            f"Period: {start} to {end}",
            f"Experiments: {len(config_experiments)}",
            "All variants use identical downloaded bars, universe, execution assumptions, and date range. "
            "No parameters are selected automatically.",
            "Run experiments only on development data; this option cannot be combined with holdout or walk-forward evaluation.",
            "",
            *experiment_sections,
        ])
        run_ids = all_run_ids
    elif holdout_start is None:
        results, report, run_ids = run_period(start, end)
    elif walk_forward_months is None:
        training_end = (date.fromisoformat(holdout_start) - timedelta(days=1)).isoformat()
        _, training_report, training_run_ids = run_period(
            start, training_end, simulation_end=training_end,
        )
        holdout_results, holdout_report, holdout_run_ids = run_period(
            holdout_start, end, simulation_start=holdout_start,
        )
        report = "\n".join([
            "# Out-of-Sample Holdout Validation",
            "",
            f"Data downloaded: {start} to {end}",
            f"In-sample period: {start} to {training_end}",
            f"Untouched holdout period: {holdout_start} to {end}",
            "The holdout period uses the same strategy and configuration; no tuning is performed by this command.",
            "",
            "## In-sample results",
            "",
            training_report.replace("# Swing Trader Execution-Cost Sensitivity", "### Execution-Cost Sensitivity", 1),
            "",
            "## Out-of-sample holdout results",
            "",
            holdout_report.replace("# Swing Trader Execution-Cost Sensitivity", "### Execution-Cost Sensitivity", 1),
        ])
        results = holdout_results
        run_ids = training_run_ids + holdout_run_ids
    else:
        training_end = (date.fromisoformat(holdout_start) - timedelta(days=1)).isoformat()
        _, training_report, all_run_ids = run_period(
            start, training_end, simulation_end=training_end,
        )
        windows = build_walk_forward_windows(holdout_start, end, walk_forward_months)
        window_sections = []
        for window_start, window_end in windows:
            results, window_report, window_run_ids = run_period(
                window_start,
                window_end,
                simulation_start=window_start,
                simulation_end=window_end,
            )
            all_run_ids.extend(window_run_ids)
            window_sections.extend([
                f"## Out-of-sample window {window_start} to {window_end}",
                "",
                window_report.replace(
                    "# Swing Trader Execution-Cost Sensitivity",
                    "### Execution-Cost Sensitivity",
                    1,
                ),
                "",
            ])
        report = "\n".join([
            "# Walk-Forward Out-of-Sample Validation",
            "",
            f"Data downloaded: {start} to {end}",
            f"Initial development period: {start} to {training_end}",
            f"Out-of-sample windows: {len(windows)} consecutive windows of up to {walk_forward_months} month(s)",
            "The same strategy and configuration are evaluated independently in each window; no parameter tuning is performed by this command.",
            "",
            "## Initial development-period results",
            "",
            training_report.replace(
                "# Swing Trader Execution-Cost Sensitivity",
                "### Execution-Cost Sensitivity",
                1,
            ),
            "",
            *window_sections,
        ])
        run_ids = all_run_ids
    report = f"{format_data_coverage_report(coverage)}\n{report}"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    report_path = REPORT_DIR / f"backtest_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")
    print(report, end="")
    print(f"Persisted {len(run_ids)} run(s) to {DEFAULT_BACKTEST_DB}")
    print(f"Saved backtest report to {report_path}")
    return results


def run_alpaca_asset_snapshot() -> tuple[str, int]:
    from swingtrader.asset_snapshot import fetch_and_persist_alpaca_asset_snapshot

    snapshot_id, asset_count = fetch_and_persist_alpaca_asset_snapshot()
    print(f"Saved read-only Alpaca US-equity asset snapshot: {snapshot_id} ({asset_count:,} assets)")
    print("Asset status is observed as of retrieval time; this does not reconstruct historical membership.")
    return snapshot_id, asset_count


def run_backtest_history(limit: int = 20) -> str:
    from swingtrader.backtest_store import format_backtest_history

    report = format_backtest_history(limit=limit)
    print(report, end="")
    return report


def run_backtest_detail(run_id: str) -> str:
    from swingtrader.backtest_store import format_backtest_run

    report = format_backtest_run(run_id)
    print(report, end="")
    return report


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


def main():
    started_at = perf_counter()
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
            parser = argparse.ArgumentParser(description="Consistent Swing Trading Scanner for NYSE & NASDAQ")
            parser.add_argument("--symbols", nargs="+", help="Optional specific ticker symbols to scan")
            parser.add_argument(
                "--universe-file",
                help="CSV of date-effective historical membership (symbol,start_date,end_date); for backtests only",
            )
            parser.add_argument(
                "--experiment-file",
                help="JSON list of named TradingConfig overrides for a development-period comparison",
            )
            parser.add_argument("--output", help="Path for JSON scan snapshot output")
            parser.add_argument("--top", type=int, default=25, help="Number of top candidates to return (default 25)")
            parser.add_argument("--min-price", type=float, help="Minimum stock price (default $5.00)")
            parser.add_argument("--max-price", type=float, help="Maximum stock price (default $30.00)")
            parser.add_argument("--backtest", action="store_true", help="Run a historical daily-bar backtest")
            parser.add_argument("--research", action="store_true", help="Run the research scanner and ranked grid; never submit orders (default mode)")
            parser.add_argument(
                "--backtest-history",
                nargs="?",
                const=20,
                type=int,
                metavar="LIMIT",
                help="List recent persisted backtests (default: 20)",
            )
            parser.add_argument("--backtest-run", metavar="RUN_ID", help="Show a persisted backtest and its trades")
            parser.add_argument(
                "--snapshot-alpaca-assets",
                action="store_true",
                help="Save a read-only, dated snapshot of Alpaca US-equity asset status",
            )
            parser.add_argument("--start", help="Backtest start date (YYYY-MM-DD)")
            parser.add_argument("--end", help="Backtest end date (YYYY-MM-DD)")
            parser.add_argument(
                "--holdout-start",
                help="Optional untouched out-of-sample start date; runs and reports pre-holdout and holdout periods separately",
            )
            parser.add_argument(
                "--walk-forward-months",
                type=int,
                help="Split the holdout period into consecutive out-of-sample windows of this many months",
            )
            parser.add_argument(
                "--monte-carlo-simulations",
                type=int,
                default=1000,
                help="Trade-sequence bootstrap paths in each report (default 1000)",
            )
            parser.add_argument(
                "--monte-carlo-drawdown-threshold",
                type=float,
                default=-0.20,
                help="Drawdown threshold for Monte Carlo probability (default -0.20)",
            )
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
            paper_modes.add_argument("--paper-performance", action="store_true", help="Read the local paper journal and performance report; no broker access")
            paper_modes.add_argument("--paper-reconcile", action="store_true", help="Inspect paper orders and request cancellation of stale unfilled bot entries")
            paper_modes.add_argument("--paper-bot", action="store_true", help="Reconcile continuously and rescan for open paper slots")
            paper_modes.add_argument("--paper-bot-once", action="store_true", help="Run one guarded paper bot cycle and exit")

            args = parser.parse_args()
            paper_mode = any((
                args.paper_preview, args.paper_trade, args.paper_status, args.paper_performance,
                args.paper_reconcile, args.paper_bot, args.paper_bot_once,
            ))
            if args.research and any((paper_mode, args.backtest, args.backtest_history is not None,
                                      args.backtest_run, args.snapshot_alpaca_assets)):
                parser.error("--research cannot be combined with a trading or historical mode")
            if args.backtest and paper_mode:
                parser.error("--backtest cannot be combined with a paper mode")
            if args.paper_performance and any((
                args.symbols, args.output, args.universe_file, args.experiment_file,
                args.start, args.end, args.holdout_start, args.walk_forward_months is not None,
                args.min_price is not None, args.max_price is not None,
            )):
                parser.error("--paper-performance reads the local journal only; scan/backtest options are not supported")
            if args.backtest_history is not None or args.backtest_run:
                if any((
                    args.snapshot_alpaca_assets, args.backtest, args.paper_preview, args.paper_trade,
                    args.paper_status, args.paper_performance, args.paper_reconcile, args.paper_bot, args.paper_bot_once,
                    args.symbols, args.universe_file, args.experiment_file, args.output,
                    args.start, args.end, args.holdout_start, args.walk_forward_months is not None,
                    args.spread_scenarios_bps is not None, args.min_price is not None,
                    args.max_price is not None, args.top != 25, args.slippage_bps != 5.0,
                    args.spread_bps != 5.0, args.commission_per_share != 0.0,
                    args.target_fill_mode != "trade_through",
                    args.target_trade_through_bps != 5.0,
                    args.monte_carlo_simulations != 1000,
                    args.monte_carlo_drawdown_threshold != -0.20,
                )):
                    parser.error("Backtest history/detail options cannot be combined with other modes")
                if args.backtest_history is not None:
                    if args.backtest_run:
                        parser.error("--backtest-history and --backtest-run are mutually exclusive")
                    if args.backtest_history < 1:
                        parser.error("--backtest-history limit must be at least 1")
                    run_backtest_history(args.backtest_history)
                else:
                    run_backtest_detail(args.backtest_run)
            elif args.snapshot_alpaca_assets:
                if any((
                    args.backtest, args.paper_preview, args.paper_trade, args.paper_status, args.paper_performance,
                    args.paper_reconcile, args.paper_bot, args.paper_bot_once, args.symbols,
                    args.universe_file, args.experiment_file, args.output, args.start, args.end,
                    args.holdout_start, args.walk_forward_months is not None,
                    args.spread_scenarios_bps is not None, args.min_price is not None,
                    args.max_price is not None, args.top != 25, args.slippage_bps != 5.0,
                    args.spread_bps != 5.0, args.commission_per_share != 0.0,
                    args.target_fill_mode != "trade_through",
                    args.target_trade_through_bps != 5.0,
                    args.monte_carlo_simulations != 1000,
                    args.monte_carlo_drawdown_threshold != -0.20,
                )):
                    parser.error("--snapshot-alpaca-assets cannot be combined with other operating modes")
                run_alpaca_asset_snapshot()
            elif args.backtest:
                if not args.start or not args.end:
                    parser.error("--backtest requires both --start and --end")
                if bool(args.symbols) == bool(args.universe_file):
                    parser.error("--backtest requires exactly one of --symbols or --universe-file")
                if args.experiment_file and (args.holdout_start or args.walk_forward_months is not None):
                    parser.error("--experiment-file cannot be combined with holdout or walk-forward evaluation")
                if date.fromisoformat(args.start) > date.fromisoformat(args.end):
                    parser.error("--start must not be later than --end")
                if args.holdout_start:
                    try:
                        valid_holdout = (
                            date.fromisoformat(args.start)
                            < date.fromisoformat(args.holdout_start)
                            <= date.fromisoformat(args.end)
                        )
                    except ValueError:
                        parser.error("--holdout-start must use YYYY-MM-DD")
                    if not valid_holdout:
                        parser.error("--holdout-start must be later than --start and no later than --end")
                if args.walk_forward_months is not None:
                    if not args.holdout_start:
                        parser.error("--walk-forward-months requires --holdout-start")
                    if args.walk_forward_months < 1:
                        parser.error("--walk-forward-months must be at least 1")
                if args.monte_carlo_simulations < 1:
                    parser.error("--monte-carlo-simulations must be at least 1")
                if not -1 <= args.monte_carlo_drawdown_threshold <= 0:
                    parser.error("--monte-carlo-drawdown-threshold must be between -1 and 0")
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
                    holdout_start=args.holdout_start,
                    walk_forward_months=args.walk_forward_months,
                    monte_carlo_simulations=args.monte_carlo_simulations,
                    monte_carlo_drawdown_threshold=args.monte_carlo_drawdown_threshold,
                    universe_file=args.universe_file,
                    experiment_file=args.experiment_file,
                )
            elif args.universe_file or args.experiment_file:
                parser.error("--universe-file and --experiment-file can only be used with --backtest")
            elif args.paper_performance:
                from swingtrader.paper_journal import format_paper_performance

                print(format_paper_performance(), end="")
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
