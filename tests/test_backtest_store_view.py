import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
from swingtrader.backtest_store import format_backtest_history, format_backtest_run


def create_run_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript("""
            CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT, strategy_id TEXT, strategy_version TEXT,
                start_date TEXT, end_date TEXT, symbols_json TEXT, experiment_name TEXT,
                spread_bps REAL, slippage_bps REAL, total_return REAL, max_drawdown REAL,
                trade_count INTEGER, sharpe_ratio REAL, configuration_hash TEXT,
                configuration_json TEXT, commission_per_share REAL, target_fill_mode TEXT,
                target_trade_through_bps REAL, universe_source TEXT, universe_sha256 TEXT,
                universe_record_count INTEGER, starting_equity REAL, ending_equity REAL,
                cagr REAL, benchmark_return REAL, benchmark_alpha REAL, win_rate REAL,
                expectancy_dollars REAL, profit_factor REAL
            );
            CREATE TABLE backtest_trades (
                run_id TEXT, trade_number INTEGER, symbol TEXT, entry_date TEXT, exit_date TEXT,
                entry_price REAL, exit_price REAL, shares INTEGER, exit_reason TEXT,
                realized_pnl REAL, realized_r REAL
            );
            CREATE TABLE backtest_equity (run_id TEXT, session_date TEXT, equity REAL);
        """)
        database.executemany(
            """INSERT INTO backtest_runs VALUES (
                ?, ?, 'breakout', '1.0', ?, ?, ?, ?, ?, 5, ?, -0.1, 2, 1.2, 'config-hash',
                ?, 0.005, 'trade_through', 5, ?, 'universe-hash', 1, 10000, ?, 0.2,
                0.1, 0.1, 0.5, 25, 2.0
            )""",
            [
                ("run-old", "2025-01-01T00:00:00+00:00", "2024-01-01", "2024-12-31",
                 json.dumps(["AAA"]), None, 5, 0.1, '{"risk": 0.01}', "members.csv", 10100),
                ("run-new", "2025-02-01T00:00:00+00:00", "2024-06-01", "2025-01-31",
                 json.dumps(["BBB"]), "variant", 10, -0.05, '{"risk": 0.02}', None, 9500),
            ],
        )
        database.execute(
            """INSERT INTO backtest_trades VALUES (
                'run-new', 1, 'BBB', '2024-06-10', '2024-06-12', 10, 10.5, 10,
                'target', 5, 0.5
            )"""
        )
        database.executemany(
            "INSERT INTO backtest_equity VALUES ('run-new', ?, ?)",
            [("2024-06-10", 10000), ("2024-06-12", 10005)],
        )


class BacktestStoreViewTests(unittest.TestCase):
    def test_history_is_read_only_recent_first_and_labeled_not_ranked(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backtests.sqlite3"
            create_run_database(path)
            before = path.read_bytes()

            report = format_backtest_history(path, limit=1)

            self.assertEqual(path.read_bytes(), before)
        self.assertIn("Showing 1 most recent run(s); this is a record view, not a ranking.", report)
        self.assertIn("run-new", report)
        self.assertNotIn("run-old", report)
        self.assertIn("2024-06-01 to 2025-01-31", report)
        self.assertIn("variant", report)
        self.assertIn("Corr blocks", report)
        self.assertIn("| 2 | 0 | N/A | N/A | 1.200 |", report)

    def test_history_handles_missing_and_empty_database_without_creating_files(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"
            self.assertIn("does not exist yet", format_backtest_history(missing))
            self.assertFalse(missing.exists())

            empty = Path(directory) / "empty.sqlite3"
            with closing(sqlite3.connect(empty)):
                pass
            self.assertIn("no run table", format_backtest_history(empty))

    def test_run_detail_includes_configuration_trades_and_equity_summary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backtests.sqlite3"
            create_run_database(path)

            report = format_backtest_run("run-new", path)

        self.assertIn("# Backtest Run run-new", report)
        self.assertIn('"risk": 0.02', report)
        self.assertIn("| 2 | 0 | N/A | N/A | 50.00% |", report)
        self.assertIn("| 1 | BBB | 2024-06-10 | 2024-06-12 |", report)
        self.assertIn("2 daily point(s)", report)
        self.assertIn("2024-06-10 ($10,000.00) through 2024-06-12 ($10,005.00)", report)

    def test_run_detail_rejects_missing_run(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backtests.sqlite3"
            create_run_database(path)
            with self.assertRaisesRegex(ValueError, "Backtest run not found"):
                format_backtest_run("unknown", path)

    def test_cli_history_route_does_not_backtest(self):
        with TemporaryDirectory() as directory:
            output = StringIO()
            with (
                patch("main.REPORT_DIR", Path(directory)),
                patch("main.sys.argv", ["main.py", "--backtest-history", "3"]),
                patch("swingtrader.backtest_store.format_backtest_history", return_value="# History\n") as history,
                patch("main.run_backtest_cli") as run_backtest,
                redirect_stdout(output),
            ):
                main.main()
        history.assert_called_once_with(limit=3)
        run_backtest.assert_not_called()
        self.assertIn("# History", output.getvalue())


if __name__ == "__main__":
    unittest.main()
