import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .backtest import BacktestResult


DEFAULT_BACKTEST_DB = Path("data") / "backtests.sqlite3"


def format_backtest_history(path: str | Path = DEFAULT_BACKTEST_DB, limit: int = 20) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Backtest history limit must be a positive integer")
    database_path = Path(path)
    if not database_path.is_file():
        return "No persisted backtests found; the backtest database does not exist yet.\n"
    with closing(_open_read_only(database_path)) as database:
        if not _table_exists(database, "backtest_runs"):
            return "No persisted backtests found; the backtest database has no run table.\n"
        blocked_select = (
            "correlation_blocked_entries"
            if "correlation_blocked_entries" in _table_columns(database, "backtest_runs")
            else "0"
        )
        columns = _table_columns(database, "backtest_runs")
        rows = database.execute(
            """SELECT run_id, created_at, strategy_id, strategy_version, start_date, end_date,
                      symbols_json, experiment_name, spread_bps, slippage_bps, total_return,
                      max_drawdown, trade_count, sharpe_ratio, """
            + blocked_select
            + """ AS correlation_blocked_entries, """
            + ("average_open_risk_fraction" if "average_open_risk_fraction" in columns else "NULL")
            + """ AS average_open_risk_fraction, """
            + ("peak_open_risk_fraction" if "peak_open_risk_fraction" in columns else "NULL")
            + """ AS peak_open_risk_fraction
               FROM backtest_runs
               ORDER BY created_at DESC, run_id
               LIMIT ?""",
            (limit,),
        ).fetchall()
    if not rows:
        return "No persisted backtests found.\n"

    lines = [
        "# Persisted Backtest History",
        "",
        f"Showing {len(rows)} most recent run(s); this is a record view, not a ranking.",
        "",
        "| Run ID | Created (UTC) | Strategy | Period | Symbols | Experiment | Spread bps | Slippage bps | Return | Max drawdown | Trades | Corr blocks | Avg open risk | Peak open risk | Sharpe |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        (run_id, created_at, strategy_id, strategy_version, start, end, symbols_json,
         experiment, spread, slippage, total_return, drawdown, trades, sharpe,
         correlation_blocked_entries, average_open_risk, peak_open_risk) = row
        symbols = _format_symbols(symbols_json, run_id)
        lines.append(
            f"| {_markdown_cell(run_id)} | {_markdown_cell(created_at)} | "
            f"{_markdown_cell(strategy_id)} {_markdown_cell(strategy_version)} | "
            f"{start} to {end} | {_markdown_cell(symbols)} | {_markdown_cell(experiment or '-')} | "
            f"{spread:g} | {slippage:g} | {total_return:.2%} | {drawdown:.2%} | "
            f"{trades} | {correlation_blocked_entries} | {_format_percent(average_open_risk)} | "
            f"{_format_percent(peak_open_risk)} | {_format_number(sharpe)} |"
        )
    return "\n".join(lines) + "\n"


def format_backtest_run(run_id: str, path: str | Path = DEFAULT_BACKTEST_DB) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("A non-empty backtest run ID is required")
    database_path = Path(path)
    if not database_path.is_file():
        raise ValueError(f"Backtest database does not exist: {database_path}")
    with closing(_open_read_only(database_path)) as database:
        if not _table_exists(database, "backtest_runs"):
            raise ValueError(f"Backtest database has no run table: {database_path}")
        database.row_factory = sqlite3.Row
        blocked_select = (
            "correlation_blocked_entries"
            if "correlation_blocked_entries" in _table_columns(database, "backtest_runs")
            else "0 AS correlation_blocked_entries"
        )
        columns = _table_columns(database, "backtest_runs")
        risk_select = ", ".join(
            column if column in columns else f"NULL AS {column}"
            for column in ("average_open_risk_fraction", "peak_open_risk_fraction")
        )
        run = database.execute(
            "SELECT *, " + blocked_select + ", " + risk_select
            + " FROM backtest_runs WHERE run_id = ?",
            (run_id.strip(),),
        ).fetchone()
        if run is None:
            raise ValueError(f"Backtest run not found: {run_id}")
        trades = database.execute(
            "SELECT trade_number, symbol, entry_date, exit_date, entry_price, exit_price, shares, "
            "exit_reason, realized_pnl, realized_r FROM backtest_trades "
            "WHERE run_id = ? ORDER BY trade_number",
            (run_id.strip(),),
        ).fetchall() if _table_exists(database, "backtest_trades") else []
        equity = database.execute(
            "SELECT session_date, equity FROM backtest_equity "
            "WHERE run_id = ? ORDER BY session_date",
            (run_id.strip(),),
        ).fetchall() if _table_exists(database, "backtest_equity") else []

    lines = [
        f"# Backtest Run {run['run_id']}",
        "",
        f"- Created (UTC): {run['created_at']}",
        f"- Strategy: {run['strategy_id']} {run['strategy_version']}",
        f"- Period: {run['start_date']} to {run['end_date']}",
        f"- Symbols: {_markdown_cell(_format_symbols(run['symbols_json'], run_id))}",
        f"- Experiment: {_markdown_cell(run['experiment_name'] or '-')}",
        f"- Configuration SHA-256: `{run['configuration_hash']}`",
        f"- Execution: spread {run['spread_bps']:g} bps; slippage {run['slippage_bps']:g} bps per fill; "
        f"commission ${run['commission_per_share']:g}/share/side; target fill `{run['target_fill_mode']}` "
        f"with {run['target_trade_through_bps']:g} bps trade-through",
        f"- Universe source: {_markdown_cell(run['universe_source'] or 'explicit symbols')}",
        f"- Universe SHA-256: `{run['universe_sha256'] or 'N/A'}`; records: {run['universe_record_count']}",
        "",
        "## Performance",
        "",
        "| Starting equity | Ending equity | Return | CAGR | Benchmark return | Alpha | Max drawdown | Sharpe | Trades | Corr blocks | Avg open risk | Peak open risk | Win rate | Expectancy | Profit factor |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| ${run['starting_equity']:,.2f} | ${run['ending_equity']:,.2f} | {run['total_return']:.2%} | "
        f"{_format_percent(run['cagr'])} | {_format_percent(run['benchmark_return'])} | "
        f"{_format_percent(run['benchmark_alpha'])} | {run['max_drawdown']:.2%} | "
        f"{_format_number(run['sharpe_ratio'])} | {run['trade_count']} | "
        f"{run['correlation_blocked_entries']} | "
        f"{_format_percent(run['average_open_risk_fraction'])} | "
        f"{_format_percent(run['peak_open_risk_fraction'])} | {run['win_rate']:.2%} | "
        f"${run['expectancy_dollars']:,.2f} | {_format_number(run['profit_factor'])} |",
        "",
        "## Configuration",
        "",
        "```json",
    ]
    try:
        configuration = json.dumps(json.loads(run["configuration_json"]), indent=2, sort_keys=True)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Backtest run {run_id} has invalid configuration JSON") from exc
    lines.extend([configuration, "```", "", "## Trades", ""])
    if trades:
        lines.extend([
            "| # | Symbol | Entry | Exit | Entry price | Exit price | Shares | Exit reason | P&L | R |",
            "|---:|---|---|---|---:|---:|---:|---|---:|---:|",
        ])
        lines.extend(
            f"| {trade['trade_number']} | {_markdown_cell(trade['symbol'])} | {trade['entry_date']} | "
            f"{trade['exit_date']} | ${trade['entry_price']:.2f} | ${trade['exit_price']:.2f} | "
            f"{trade['shares']} | {_markdown_cell(trade['exit_reason'])} | "
            f"${trade['realized_pnl']:,.2f} | {trade['realized_r']:.2f} |"
            for trade in trades
        )
    else:
        lines.append("No closed trades were recorded for this run.")
    lines.extend(["", "## Equity curve", ""])
    if equity:
        lines.append(
            f"{len(equity)} daily point(s), from {equity[0]['session_date']} "
            f"(${equity[0]['equity']:,.2f}) through {equity[-1]['session_date']} "
            f"(${equity[-1]['equity']:,.2f})."
        )
    else:
        lines.append("No daily equity points were recorded for this run.")
    return "\n".join(lines) + "\n"


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(database: sqlite3.Connection, table_name: str) -> bool:
    return database.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(database: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in database.execute(f"PRAGMA table_info({table_name})")}


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _format_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _format_symbols(symbols_json: str, run_id: str) -> str:
    try:
        symbols = json.loads(symbols_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Backtest run {run_id} has invalid symbols JSON") from exc
    if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
        raise ValueError(f"Backtest run {run_id} has invalid symbols JSON")
    return ", ".join(symbols)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    slippage_bps REAL NOT NULL,
    spread_bps REAL NOT NULL,
    commission_per_share REAL NOT NULL,
    target_fill_mode TEXT NOT NULL,
    target_trade_through_bps REAL NOT NULL,
    starting_equity REAL NOT NULL,
    ending_equity REAL NOT NULL,
    total_return REAL NOT NULL,
    cagr REAL,
    benchmark_return REAL,
    benchmark_alpha REAL,
    max_drawdown REAL NOT NULL,
    annualized_volatility REAL NOT NULL,
    sharpe_ratio REAL,
    sortino_ratio REAL,
    calmar_ratio REAL,
    trade_count INTEGER NOT NULL,
    win_rate REAL NOT NULL,
    average_win REAL,
    average_loss REAL,
    median_win REAL,
    median_loss REAL,
    expectancy_dollars REAL NOT NULL,
    average_r REAL NOT NULL,
    profit_factor REAL NOT NULL,
    max_consecutive_losses INTEGER NOT NULL,
    average_holding_sessions REAL NOT NULL,
    average_gross_exposure REAL NOT NULL DEFAULT 0,
    average_open_risk_fraction REAL,
    peak_open_risk_fraction REAL,
    turnover REAL NOT NULL DEFAULT 0,
    market_regime_json TEXT NOT NULL DEFAULT '{}',
    sector_mapping_json TEXT NOT NULL DEFAULT '{}',
    sector_exposure_json TEXT NOT NULL DEFAULT '{}',
    average_pairwise_correlation REAL,
    max_pairwise_correlation REAL,
    correlation_blocked_entries INTEGER NOT NULL DEFAULT 0,
    sector_etf_mapping_json TEXT NOT NULL DEFAULT '{}',
    average_relative_strength_vs_market REAL,
    average_relative_strength_vs_sector REAL,
    universe_source TEXT,
    universe_sha256 TEXT,
    universe_membership_json TEXT NOT NULL DEFAULT '{}',
    universe_record_count INTEGER NOT NULL DEFAULT 0,
    experiment_name TEXT
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id TEXT NOT NULL,
    trade_number INTEGER NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    configuration_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    sector TEXT NOT NULL DEFAULT 'Unclassified',
    sector_etf TEXT,
    market_regime TEXT NOT NULL DEFAULT 'UNKNOWN',
    stock_return_20d REAL,
    market_return_20d REAL,
    sector_return_20d REAL,
    relative_strength_vs_market REAL,
    relative_strength_vs_sector REAL,
    entry_date TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    shares INTEGER NOT NULL,
    holding_sessions INTEGER NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl REAL NOT NULL,
    realized_r REAL NOT NULL,
    strategy_score INTEGER NOT NULL,
    setup TEXT NOT NULL,
    PRIMARY KEY (run_id, trade_number),
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_symbol_entry
    ON backtest_trades(symbol, entry_date);
CREATE TABLE IF NOT EXISTS backtest_equity (
    run_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY (run_id, session_date),
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
);
"""


def persist_sensitivity_results(
    results: list[tuple[float, BacktestResult]],
    *,
    symbols: list[str],
    start: str,
    end: str,
    slippage_bps: float,
    commission_per_share: float,
    target_fill_mode: str,
    target_trade_through_bps: float,
    universe_source: str | None = None,
    universe_sha256: str | None = None,
    universe_membership_json: str = "{}",
    universe_record_count: int = 0,
    experiment_name: str | None = None,
    path: str | Path = DEFAULT_BACKTEST_DB,
) -> list[str]:
    if not results:
        raise ValueError("At least one backtest result is required")

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    run_ids = []

    with closing(sqlite3.connect(database_path)) as database:
        database.execute("PRAGMA foreign_keys = ON")
        database.executescript(_SCHEMA)
        _ensure_column(database, "backtest_runs", "average_gross_exposure", "REAL NOT NULL DEFAULT 0")
        _ensure_column(database, "backtest_runs", "average_open_risk_fraction", "REAL")
        _ensure_column(database, "backtest_runs", "peak_open_risk_fraction", "REAL")
        _ensure_column(database, "backtest_runs", "turnover", "REAL NOT NULL DEFAULT 0")
        _ensure_column(database, "backtest_runs", "market_regime_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(database, "backtest_runs", "sector_mapping_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(database, "backtest_runs", "sector_exposure_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(database, "backtest_runs", "average_pairwise_correlation", "REAL")
        _ensure_column(database, "backtest_runs", "max_pairwise_correlation", "REAL")
        _ensure_column(database, "backtest_runs", "correlation_blocked_entries", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "backtest_runs", "sector_etf_mapping_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(database, "backtest_runs", "average_relative_strength_vs_market", "REAL")
        _ensure_column(database, "backtest_runs", "average_relative_strength_vs_sector", "REAL")
        _ensure_column(database, "backtest_runs", "universe_source", "TEXT")
        _ensure_column(database, "backtest_runs", "universe_sha256", "TEXT")
        _ensure_column(database, "backtest_runs", "universe_membership_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(database, "backtest_runs", "universe_record_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(database, "backtest_runs", "experiment_name", "TEXT")
        _ensure_column(database, "backtest_trades", "sector", "TEXT NOT NULL DEFAULT 'Unclassified'")
        _ensure_column(database, "backtest_trades", "sector_etf", "TEXT")
        _ensure_column(database, "backtest_trades", "market_regime", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        _ensure_column(database, "backtest_trades", "stock_return_20d", "REAL")
        _ensure_column(database, "backtest_trades", "market_return_20d", "REAL")
        _ensure_column(database, "backtest_trades", "sector_return_20d", "REAL")
        _ensure_column(database, "backtest_trades", "relative_strength_vs_market", "REAL")
        _ensure_column(database, "backtest_trades", "relative_strength_vs_sector", "REAL")
        database.execute("PRAGMA user_version = 7")
        with database:
            for spread_bps, result in results:
                run_id = str(uuid4())
                run_ids.append(run_id)
                database.execute(
                    """INSERT INTO backtest_runs (
                        run_id, created_at, strategy_id, strategy_version, configuration_hash,
                        configuration_json, symbols_json, start_date, end_date, slippage_bps,
                        spread_bps, commission_per_share, target_fill_mode, target_trade_through_bps,
                        starting_equity, ending_equity, total_return, cagr, benchmark_return,
                        benchmark_alpha, max_drawdown, annualized_volatility, sharpe_ratio,
                        sortino_ratio, calmar_ratio, trade_count, win_rate, average_win,
                        average_loss, median_win, median_loss, expectancy_dollars, average_r,
                        profit_factor, max_consecutive_losses, average_holding_sessions,
                        average_gross_exposure, average_open_risk_fraction, peak_open_risk_fraction,
                        turnover, market_regime_json, sector_mapping_json,
                        sector_exposure_json, average_pairwise_correlation, max_pairwise_correlation,
                        correlation_blocked_entries,
                        sector_etf_mapping_json, average_relative_strength_vs_market,
                        average_relative_strength_vs_sector, universe_source,
                        universe_sha256, universe_membership_json, universe_record_count,
                        experiment_name
                    ) VALUES (
                        :run_id, :created_at, :strategy_id, :strategy_version, :configuration_hash,
                        :configuration_json, :symbols_json, :start_date, :end_date, :slippage_bps,
                        :spread_bps, :commission_per_share, :target_fill_mode, :target_trade_through_bps,
                        :starting_equity, :ending_equity, :total_return, :cagr, :benchmark_return,
                        :benchmark_alpha, :max_drawdown, :annualized_volatility, :sharpe_ratio,
                        :sortino_ratio, :calmar_ratio, :trade_count, :win_rate, :average_win,
                        :average_loss, :median_win, :median_loss, :expectancy_dollars, :average_r,
                        :profit_factor, :max_consecutive_losses, :average_holding_sessions,
                        :average_gross_exposure, :average_open_risk_fraction, :peak_open_risk_fraction,
                        :turnover, :market_regime_json, :sector_mapping_json,
                        :sector_exposure_json, :average_pairwise_correlation, :max_pairwise_correlation,
                        :correlation_blocked_entries,
                        :sector_etf_mapping_json, :average_relative_strength_vs_market,
                        :average_relative_strength_vs_sector, :universe_source,
                        :universe_sha256, :universe_membership_json, :universe_record_count,
                        :experiment_name
                    )""",
                    {
                        "run_id": run_id,
                        "created_at": created_at,
                        "strategy_id": result.strategy_id,
                        "strategy_version": result.strategy_version,
                        "configuration_hash": result.configuration_hash,
                        "configuration_json": json.dumps(result.configuration, sort_keys=True, separators=(",", ":"), allow_nan=False),
                        "symbols_json": json.dumps(symbols, separators=(",", ":")),
                        "start_date": start,
                        "end_date": end,
                        "slippage_bps": slippage_bps,
                        "spread_bps": spread_bps,
                        "commission_per_share": commission_per_share,
                        "target_fill_mode": target_fill_mode,
                        "target_trade_through_bps": target_trade_through_bps,
                        "starting_equity": result.starting_equity,
                        "ending_equity": result.ending_equity,
                        "total_return": result.ending_equity / result.starting_equity - 1,
                        "cagr": result.cagr,
                        "benchmark_return": result.benchmark_return,
                        "benchmark_alpha": result.benchmark_alpha,
                        "max_drawdown": result.max_drawdown,
                        "annualized_volatility": result.annualized_volatility,
                        "sharpe_ratio": result.sharpe_ratio,
                        "sortino_ratio": result.sortino_ratio,
                        "calmar_ratio": result.calmar_ratio,
                        "trade_count": len(result.trades),
                        "win_rate": result.win_rate,
                        "average_win": result.average_win,
                        "average_loss": result.average_loss,
                        "median_win": result.median_win,
                        "median_loss": result.median_loss,
                        "expectancy_dollars": result.expectancy_dollars,
                        "average_r": result.average_r,
                        "profit_factor": result.profit_factor,
                        "max_consecutive_losses": result.max_consecutive_losses,
                        "average_holding_sessions": result.average_holding_sessions,
                        "average_gross_exposure": result.average_gross_exposure,
                        "average_open_risk_fraction": result.average_open_risk_fraction,
                        "peak_open_risk_fraction": result.peak_open_risk_fraction,
                        "turnover": result.turnover,
                        "market_regime_json": json.dumps(
                            {
                                regime: {
                                    **metrics,
                                    "profit_factor": "inf" if math.isinf(metrics["profit_factor"]) else metrics["profit_factor"],
                                }
                                for regime, metrics in result.market_regime_metrics.items()
                            },
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        "sector_mapping_json": json.dumps(result.sector_mapping, sort_keys=True, allow_nan=False),
                        "sector_exposure_json": json.dumps(result.sector_exposure_metrics, sort_keys=True, allow_nan=False),
                        "average_pairwise_correlation": result.average_pairwise_correlation,
                        "max_pairwise_correlation": result.max_pairwise_correlation,
                        "correlation_blocked_entries": result.correlation_blocked_entries,
                        "sector_etf_mapping_json": json.dumps(result.sector_etf_mapping, sort_keys=True, allow_nan=False),
                        "average_relative_strength_vs_market": result.average_relative_strength_vs_market,
                        "average_relative_strength_vs_sector": result.average_relative_strength_vs_sector,
                        "universe_source": universe_source,
                        "universe_sha256": universe_sha256,
                        "universe_membership_json": universe_membership_json,
                        "universe_record_count": universe_record_count,
                        "experiment_name": experiment_name,
                    },
                )
                database.executemany(
                    """INSERT INTO backtest_trades (
                        run_id, trade_number, strategy_id, strategy_version, configuration_hash,
                        symbol, sector, sector_etf, market_regime, stock_return_20d, market_return_20d,
                        sector_return_20d, relative_strength_vs_market, relative_strength_vs_sector,
                        entry_date, exit_date, entry_price, exit_price,
                        shares, holding_sessions, stop_price, target_price, exit_reason,
                        realized_pnl, realized_r, strategy_score, setup
                    ) VALUES (
                        :run_id, :trade_number, :strategy_id, :strategy_version, :configuration_hash,
                        :symbol, :sector, :sector_etf, :market_regime, :stock_return_20d, :market_return_20d,
                        :sector_return_20d, :relative_strength_vs_market, :relative_strength_vs_sector,
                        :entry_date, :exit_date, :entry_price, :exit_price, :shares,
                        :holding_sessions, :stop_price, :target_price, :exit_reason, :realized_pnl,
                        :realized_r, :strategy_score, :setup
                    )""",
                    [
                        {
                            "run_id": run_id,
                            "trade_number": trade_number,
                            **trade.__dict__,
                        }
                        for trade_number, trade in enumerate(result.trades, start=1)
                    ],
                )
                database.executemany(
                    "INSERT INTO backtest_equity (run_id, session_date, equity) VALUES (?, ?, ?)",
                    [
                        (run_id, session_date.date().isoformat(), float(equity))
                        for session_date, equity in result.equity_curve.items()
                    ],
                )

    return run_ids


def _ensure_column(database: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in database.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
