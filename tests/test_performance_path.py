import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import main
from swingtrader.config import TradingConfig
from swingtrader.data import download_batch, download_history_range, download_latest_prices
from swingtrader.market_data import (
    AlpacaMarketDataProvider,
    YFinanceMarketDataProvider,
    audit_daily_coverage,
    format_data_coverage_report,
    validate_daily_history,
)
from swingtrader.scanner import TradeCandidate, analyze, evaluate_candidate, refresh_candidate_price
from swingtrader.sentiment import SentimentResult


class MarketDataProviderTests(unittest.TestCase):
    def test_historical_coverage_counts_only_sessions_in_membership(self):
        dates = pd.to_datetime(["2025-01-02", "2025-01-06"])
        history = pd.DataFrame({"Close": [10.0, 11.0]}, index=dates)

        coverage = audit_daily_coverage(
            {"TEST": history},
            ["TEST", "MISSING"],
            "2025-01-01",
            "2025-01-07",
            membership={
                "TEST": ((date(2025, 1, 2), date(2025, 1, 6)),),
                "MISSING": ((date(2025, 1, 3), date(2025, 1, 7)),),
            },
        )

        self.assertEqual(coverage[0].expected_sessions, 3)
        self.assertEqual(coverage[0].returned_sessions, 2)
        self.assertEqual(coverage[0].missing_sessions, (date(2025, 1, 3),))
        self.assertEqual(coverage[1].expected_sessions, 3)
        self.assertEqual(coverage[1].returned_sessions, 0)
        self.assertEqual(
            coverage[1].missing_sessions,
            (date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)),
        )

    def test_historical_coverage_report_lists_missing_symbols_and_sessions(self):
        coverage = audit_daily_coverage(
            {},
            ["MISSING"],
            "2025-01-02",
            "2025-01-03",
        )

        report = format_data_coverage_report(coverage)

        self.assertIn("0/2 (0.00%)", report)
        self.assertIn("| MISSING | 2 | 0 | 0.00% | 2 | 2025-01-02, 2025-01-03 |", report)

    def test_backtest_cli_persists_development_and_walk_forward_periods(self):
        dates = pd.to_datetime([
            "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
            "2025-01-08", "2025-01-10",
        ])
        bars = pd.DataFrame(
            {"Open": [10.0] * len(dates), "High": [10.2] * len(dates),
             "Low": [9.8] * len(dates), "Close": [10.0] * len(dates),
             "Volume": [1_000_000] * len(dates)},
            index=dates,
        )

        with TemporaryDirectory() as directory:
            report_dir = Path(directory) / "reports"
            report_dir.mkdir()
            database_path = Path(directory) / "backtests.sqlite3"
            universe_path = Path(directory) / "members.csv"
            universe_path.write_text(
                "symbol,start_date,end_date\nTEST,2025-01-06,\n",
                encoding="utf-8",
            )
            provider = SimpleNamespace(
                get_bars=lambda _symbols, **_kwargs: {"TEST": bars, "SPY": bars},
            )
            from swingtrader.backtest_store import persist_sensitivity_results as persist_results

            def persist_to_temporary_database(results_to_persist, **kwargs):
                return persist_results(results_to_persist, path=database_path, **kwargs)

            with (
                patch("main.REPORT_DIR", report_dir),
                patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")),
                patch("swingtrader.market_data.AlpacaMarketDataProvider", return_value=provider),
                patch("swingtrader.backtest_store.DEFAULT_BACKTEST_DB", database_path),
                patch(
                    "swingtrader.backtest_store.persist_sensitivity_results",
                    side_effect=persist_to_temporary_database,
                ),
                redirect_stdout(io.StringIO()),
            ):
                results = main.run_backtest_cli(
                    None,
                    "2025-01-02",
                    "2025-01-10",
                    TradingConfig(),
                    holdout_start="2025-01-06",
                    walk_forward_months=1,
                    monte_carlo_simulations=2,
                    universe_file=universe_path,
                )

            with closing(sqlite3.connect(database_path)) as database:
                runs = database.execute(
                    "SELECT universe_source, universe_sha256, universe_membership_json, universe_record_count "
                    "FROM backtest_runs"
                ).fetchall()
            report = next(report_dir.glob("backtest_*.md")).read_text(encoding="utf-8")

        self.assertEqual(len(results), 1)
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(row[0] == str(universe_path) for row in runs))
        self.assertTrue(all(len(row[1]) == 64 for row in runs))
        self.assertTrue(all('"start_date":"2025-01-06"' in row[2] for row in runs))
        self.assertTrue(all(row[3] == 1 for row in runs))
        self.assertIn("# Walk-Forward Out-of-Sample Validation", report)
        self.assertIn("## Out-of-sample window 2025-01-06 to 2025-01-10", report)
        self.assertIn("## Monte Carlo Trade-Sequence Bootstrap", report)
        self.assertIn("Universe: date-effective file", report)
        self.assertIn("Universe SHA-256:", report)
        self.assertIn("## Historical Data Coverage", report)
        self.assertIn("| TEST | 4 | 4 | 100.00% | 0 | None |", report)

    def test_backtest_cli_runs_and_persists_named_config_experiments(self):
        dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
        bars = pd.DataFrame(
            {"Open": [10.0] * 3, "High": [10.2] * 3, "Low": [9.8] * 3,
             "Close": [10.0] * 3, "Volume": [1_000_000] * 3},
            index=dates,
        )

        with TemporaryDirectory() as directory:
            report_dir = Path(directory) / "reports"
            report_dir.mkdir()
            database_path = Path(directory) / "backtests.sqlite3"
            experiment_path = Path(directory) / "experiments.json"
            experiment_path.write_text(
                '{"experiments":['
                '{"name":"baseline","overrides":{}},'
                '{"name":"larger-target","overrides":{"target_percent":2.0}}'
                ']}',
                encoding="utf-8",
            )
            provider = SimpleNamespace(
                get_bars=lambda _symbols, **_kwargs: {"TEST": bars, "SPY": bars},
            )
            from swingtrader.backtest_store import persist_sensitivity_results as persist_results

            def persist_to_temporary_database(results_to_persist, **kwargs):
                return persist_results(results_to_persist, path=database_path, **kwargs)

            with (
                patch("main.REPORT_DIR", report_dir),
                patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")),
                patch("swingtrader.market_data.AlpacaMarketDataProvider", return_value=provider),
                patch("swingtrader.backtest_store.DEFAULT_BACKTEST_DB", database_path),
                patch(
                    "swingtrader.backtest_store.persist_sensitivity_results",
                    side_effect=persist_to_temporary_database,
                ),
                redirect_stdout(io.StringIO()),
            ):
                main.run_backtest_cli(
                    ["TEST"],
                    "2025-01-02",
                    "2025-01-06",
                    TradingConfig(),
                    experiment_file=experiment_path,
                    monte_carlo_simulations=2,
                )

            with closing(sqlite3.connect(database_path)) as database:
                runs = database.execute(
                    "SELECT experiment_name, configuration_hash FROM backtest_runs ORDER BY experiment_name"
                ).fetchall()
            report = next(report_dir.glob("backtest_*.md")).read_text(encoding="utf-8")

        self.assertEqual([name for name, _ in runs], ["baseline", "larger-target"])
        self.assertNotEqual(runs[0][1], runs[1][1])
        self.assertIn("# Strategy Configuration Experiments", report)
        self.assertIn("## Experiment: baseline", report)
        self.assertIn("## Experiment: larger-target", report)

    def test_daily_history_quality_limits_gaps_to_membership_window(self):
        history = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0],
             "Close": [10.5], "Volume": [1000]},
            index=pd.to_datetime(["2025-01-06"]),
        )

        warnings = validate_daily_history(
            {"TEST": history},
            ["TEST"],
            "2025-01-02",
            "2025-01-07",
            membership={"TEST": ((date(2025, 1, 6), date(2025, 1, 7)),)},
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("2025-01-07", warnings[0])
        self.assertNotIn("2025-01-02", warnings[0])

    def test_daily_history_quality_uses_exchange_sessions_not_holidays(self):
        history = pd.DataFrame(
            {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
             "Close": [10.5, 11.5], "Volume": [1000, 1200]},
            index=pd.to_datetime(["2025-01-02", "2025-01-06"]),
        )

        warnings = validate_daily_history(
            {"TEST": history}, ["TEST"], "2025-01-01", "2025-01-06",
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("2025-01-03", warnings[0])
        self.assertIn("expected NYSE sessions", warnings[0])
        self.assertIn("(data gap)", warnings[0])
        self.assertNotIn("2025-01-01", warnings[0])

    def test_daily_history_quality_ignores_closed_only_date_range(self):
        warnings = validate_daily_history(
            {}, ["TEST"], "2025-01-20", "2025-01-20",
        )

        self.assertEqual(warnings, [])

    def test_daily_history_quality_includes_both_range_boundaries(self):
        history = pd.DataFrame(
            {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
             "Close": [10.5, 11.5], "Volume": [1000, 1200]},
            index=pd.to_datetime(["2025-01-17", "2025-01-21"]),
        )

        warnings = validate_daily_history(
            {"TEST": history}, ["TEST"], "2025-01-17", "2025-01-21",
        )

        self.assertEqual(warnings, [])

    def test_daily_history_quality_rejects_bars_on_exchange_closures(self):
        history = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0],
             "Close": [10.5], "Volume": [1000]},
            index=pd.to_datetime(["2025-01-09"]),
        )

        with self.assertRaisesRegex(ValueError, "non-NYSE sessions"):
            validate_daily_history(
                {"TEST": history}, ["TEST"], "2025-01-09", "2025-01-09",
            )

    def test_daily_history_quality_rejects_duplicate_sessions(self):
        history = pd.DataFrame(
            {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
             "Close": [10.5, 11.5], "Volume": [1000, 1200]},
            index=pd.to_datetime(["2025-01-02 10:00", "2025-01-02 15:00"]),
        )

        with self.assertRaisesRegex(ValueError, "duplicate session dates"):
            validate_daily_history({"TEST": history}, ["TEST"], "2025-01-02", "2025-01-02")

    def test_daily_history_quality_rejects_bars_outside_requested_range(self):
        history = pd.DataFrame(
            {"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [1000]},
            index=pd.to_datetime(["2025-01-03"]),
        )

        with self.assertRaisesRegex(ValueError, "outside the requested date range"):
            validate_daily_history({"TEST": history}, ["TEST"], "2025-01-02", "2025-01-02")

    @patch("swingtrader.data.yf.download")
    def test_yfinance_explicit_history_range_uses_requested_dates(self, mock_download):
        mock_download.return_value = pd.DataFrame(
            {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
             "Close": [10.5, 11.5], "Volume": [1000, 1200]},
            index=pd.date_range("2025-01-02", periods=2),
        )

        history = download_history_range(["TEST"], "2025-01-01", "2025-01-31")

        self.assertIn("TEST", history)
        self.assertEqual(mock_download.call_args.kwargs["start"], "2025-01-01")
        self.assertEqual(mock_download.call_args.kwargs["end"], "2025-01-31")
        self.assertNotIn("period", mock_download.call_args.kwargs)

    def test_yfinance_adapter_routes_period_range_and_latest_calls(self):
        history = pd.DataFrame({"Close": [10.0]})
        calls = []
        provider = YFinanceMarketDataProvider(
            batch_loader=lambda symbols, **kwargs: calls.append(("period", symbols, kwargs)) or {"TEST": history},
            range_loader=lambda symbols, start, end, **kwargs: calls.append(("range", symbols, start, end, kwargs)) or {"TEST": history},
            latest_price_loader=lambda symbols: calls.append(("latest", symbols)) or {"TEST": 11.0},
        )

        self.assertEqual(provider.get_bars(["TEST"], period="1y"), {"TEST": history})
        self.assertEqual(provider.get_bars(["TEST"], start="2025-01-01", end="2025-12-31"), {"TEST": history})
        self.assertEqual(provider.get_latest_prices(["TEST"]), {"TEST": 11.0})
        self.assertEqual(calls[0], ("period", ["TEST"], {"period": "1y", "batch_size": 80}))
        self.assertEqual(calls[1], ("range", ["TEST"], "2025-01-01", "2025-12-31", {"batch_size": 80}))
        self.assertEqual(calls[2], ("latest", ["TEST"]))

    def test_alpaca_adapter_returns_daily_bars_and_latest_prices(self):
        dates = pd.date_range("2025-01-02", periods=2, tz="UTC")
        bars = pd.DataFrame(
            {"open": [10.0, 11.0], "high": [10.5, 11.5], "low": [9.5, 10.5],
             "close": [10.2, 11.2], "volume": [1000, 1200]},
            index=pd.MultiIndex.from_arrays([["TEST", "TEST"], dates], names=["symbol", "timestamp"]),
        )
        client = Mock()
        client.get_stock_bars.return_value = SimpleNamespace(df=bars)
        client.get_stock_latest_trade.return_value = {
            "TEST": SimpleNamespace(price=11.25),
            "EMPTY": SimpleNamespace(price=None),
        }
        provider = AlpacaMarketDataProvider(client=client)

        history = provider.get_bars(["TEST", "EMPTY"], start="2025-01-01", end="2025-01-03")
        latest = provider.get_latest_prices(["TEST", "EMPTY"])

        self.assertEqual(list(history), ["TEST"])
        self.assertEqual(list(history["TEST"].columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(latest, {"TEST": 11.25})
        bars_request = client.get_stock_bars.call_args.args[0]
        self.assertEqual(bars_request.feed.value, "iex")
        self.assertEqual(bars_request.asof, "-")


class ScannerPerformancePathTests(unittest.TestCase):
    def test_technical_candidate_evaluation_is_pure_and_matches_neutral_analyze(self):
        close = np.linspace(10.0, 20.0, 240)
        history = pd.DataFrame(
            {
                "Open": close,
                "High": close,
                "Low": close - 0.2,
                "Close": close,
                "Volume": [1_000_000] * len(close),
            },
            index=pd.date_range("2025-01-02", periods=len(close)),
        )
        config = TradingConfig()

        with patch("swingtrader.scanner.get_sentiment", side_effect=AssertionError("unexpected network lookup")):
            first = evaluate_candidate("TEST", history, config)
            second = evaluate_candidate("TEST", history, config)

        if first is None or second is None:
            self.fail("Expected technical candidate from the synthetic uptrend")
        self.assertEqual(first, second)
        neutral = analyze(
            "TEST",
            history,
            config,
            sentiment_fn=lambda _symbol: SentimentResult(0.0, 0, None, "Technical Trend"),
        )
        self.assertEqual(first, neutral)
        enriched = analyze(
            "TEST",
            history,
            config,
            sentiment_fn=lambda _symbol: SentimentResult(0.8, 1, "Positive headline", "Company News"),
        )
        if enriched is None:
            self.fail("Expected sentiment-enriched candidate")
        self.assertEqual(enriched.score, first.score + config.sentiment_bonus_score)
        self.assertEqual(enriched.sentiment_headline, "Positive headline")

    def test_candidate_table_header_matches_separator(self):
        with redirect_stdout(io.StringIO()) as output:
            main.print_candidates_table([])

        rows = [line for line in output.getvalue().splitlines() if line.startswith("|")]
        self.assertEqual(rows[0].count("|"), rows[1].count("|"))

    @patch("main.run_scanner")
    def test_cli_saves_each_run_as_markdown(self, mock_run_scanner):
        mock_run_scanner.side_effect = lambda **kwargs: print("\n## Candidates\n\n| Symbol |\n|---|\n| TEST |")

        with TemporaryDirectory() as report_dir, patch("main.REPORT_DIR", Path(report_dir)):
            with patch("main.sys.argv", ["main.py"]), redirect_stdout(io.StringIO()) as output:
                main.main()
                main.main()

            reports = list(Path(report_dir).glob("*.md"))
            self.assertEqual(len(reports), 2)
            for report in reports:
                contents = report.read_text(encoding="utf-8")
                self.assertIn("## Candidates", contents)
                self.assertIn("| TEST |", contents)
                self.assertIn("Total application runtime:", contents)
            self.assertIn("| TEST |", output.getvalue())

    @patch("main.print_candidates_table")
    @patch("main.save_scan")
    @patch("main.analyze", return_value=None)
    @patch("main.download_batch")
    def test_scanner_reuses_daily_history_price(
        self,
        mock_download_batch,
        mock_analyze,
        _mock_save_scan,
        _mock_print_candidates_table,
    ):
        history = pd.DataFrame({"Close": [10.0]})
        mock_download_batch.return_value = {"TEST": history}
        config = TradingConfig()

        main.run_scanner(symbols=["TEST"], output_path="unused.json", cfg=config)

        mock_download_batch.assert_called_once_with(["TEST"], period="1y", batch_size=80)
        mock_analyze.assert_called_once_with("TEST", history, config)

    @patch("swingtrader.data.yf.download", return_value=pd.DataFrame())
    def test_history_download_uses_yfinance_threads(self, mock_download):
        with TemporaryDirectory() as cache_dir:
            download_batch(["TEST"], cache_dir=cache_dir)

        self.assertTrue(mock_download.call_args.kwargs["threads"])

    @patch("swingtrader.data.yf.download")
    def test_warm_history_cache_skips_download(self, mock_download):
        history = pd.DataFrame(
            {
                "Open": range(30),
                "High": range(1, 31),
                "Low": range(30),
                "Close": range(1, 31),
                "Volume": [1_000_000] * 30,
            },
            index=pd.date_range("2026-08-01", periods=30),
        )
        mock_download.return_value = history

        with TemporaryDirectory() as cache_dir:
            first = download_batch(["TEST"], cache_dir=cache_dir)
            second = download_batch(["TEST"], cache_dir=cache_dir)

        self.assertEqual(mock_download.call_count, 1)
        pd.testing.assert_frame_equal(first["TEST"], second["TEST"])

    @patch("swingtrader.data.yf.download")
    def test_latest_prices_include_extended_hours(self, mock_download):
        mock_download.return_value = pd.DataFrame({"Close": [10.0, 10.75]})

        prices = download_latest_prices(["TEST"])

        self.assertEqual(prices, {"TEST": 10.75})
        self.assertEqual(mock_download.call_args.kwargs["interval"], "1m")
        self.assertTrue(mock_download.call_args.kwargs["prepost"])
        self.assertTrue(mock_download.call_args.kwargs["threads"])

    @patch("swingtrader.data.yf.Ticker")
    @patch("swingtrader.data.yf.download", return_value=pd.DataFrame())
    def test_latest_prices_retry_symbols_missing_from_batch(self, _mock_download, mock_ticker):
        mock_ticker.return_value.history.return_value = pd.DataFrame(
            {"Close": [10.0, 10.5]}
        )

        prices = download_latest_prices(["TEST"])

        self.assertEqual(prices, {"TEST": 10.5})
        mock_ticker.return_value.history.assert_called_once_with(
            period="1d",
            interval="1m",
            prepost=True,
            auto_adjust=True,
        )

    def test_latest_price_recalculates_risk_plan(self):
        candidate = TradeCandidate(
            symbol="TEST",
            score=70,
            prob_gain_10d=0.4,
            prob_loss_5d=0.2,
            risk_rating="Medium",
            entry=10.0,
            stop=8.5,
            target_1pct=10.1,
            target_1=12.25,
            target_2=14.5,
            shares=6,
            risk_dollars=9.0,
            reward_risk=1.5,
            main_catalyst="Technical Trend",
            setup="Momentum",
            reasons=[],
            reference_close=10.0,
        )

        refreshed = refresh_candidate_price(candidate, 12.0, TradingConfig())

        self.assertIsNotNone(refreshed)
        self.assertAlmostEqual(refreshed.entry, 12.0)
        self.assertAlmostEqual(refreshed.stop, 10.5)
        self.assertAlmostEqual(refreshed.target_1, 14.25)
        self.assertEqual(refreshed.shares, 6)
        self.assertAlmostEqual(refreshed.risk_dollars, 9.0)
        self.assertAlmostEqual(refreshed.price_change_pct, 0.2)


if __name__ == "__main__":
    unittest.main()