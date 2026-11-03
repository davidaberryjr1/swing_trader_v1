import ast
import io
import sqlite3
import unittest
from contextlib import closing, nullcontext
from contextlib import redirect_stdout
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import main
from swingtrader.config import TradingConfig, load_trading_config
from swingtrader.scanner import TradeCandidate
from swingtrader.paper_trading import (
    bot_attempted_today,
    bot_capacity,
    check_bot_daily_halt,
    enforce_correlation_limit,
    enforce_sector_exposure_limit,
    load_paper_credentials,
    plan_paper_order,
    record_paper_fills,
    review_paper_orders,
    submit_paper_candidate,
)


class PaperTradingTests(unittest.TestCase):
    def test_correlation_guard_blocks_high_absolute_correlation(self):
        dates = pd.bdate_range("2025-01-02", periods=61)
        returns = [0.001 if index % 2 else -0.001 for index in range(61)]
        prices = [10.0]
        for daily_return in returns[1:]:
            prices.append(prices[-1] * (1 + daily_return))
        inverse_prices = [10.0]
        for daily_return in returns[1:]:
            inverse_prices.append(inverse_prices[-1] * (1 - daily_return))
        candidate_history = pd.DataFrame({"Close": prices}, index=dates)
        inverse_history = pd.DataFrame(
            {"Close": inverse_prices},
            index=dates,
        )

        with self.assertRaisesRegex(ValueError, "absolute correlation over 60 shared sessions"):
            enforce_correlation_limit(
                "F",
                {"BAC"},
                {"F": candidate_history, "BAC": inverse_history},
                TradingConfig(),
            )

    def test_correlation_guard_rejects_missing_open_position_history(self):
        history = pd.DataFrame(
            {"Close": [10.0, 10.1]},
            index=pd.bdate_range("2025-01-02", periods=2),
        )
        with self.assertRaisesRegex(ValueError, "missing daily bars for BAC"):
            enforce_correlation_limit(
                "F",
                {"BAC"},
                {"F": history},
                TradingConfig(),
            )

    def test_paper_candidate_blocks_correlated_open_position_before_quote(self):
        candidate = TradeCandidate(
            symbol="F", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Breakout", reasons=[],
        )
        dates = pd.bdate_range("2025-01-02", periods=61)
        close = [10.0 * (1.001 if index % 2 else 0.999) ** index for index in range(61)]
        histories = {
            symbol: pd.DataFrame({"Close": close}, index=dates)
            for symbol in ("F", "BAC")
        }
        trading = Mock()
        trading.get_clock.return_value.is_open = True
        trading.get_account.return_value = SimpleNamespace(
            cash="1000", buying_power="1000", trading_blocked=False,
        )
        trading.get_all_positions.return_value = [SimpleNamespace(symbol="BAC", market_value="100")]
        trading.get_orders.return_value = []
        with (
            patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")),
            patch("alpaca.trading.client.TradingClient", return_value=trading),
            patch(
                "swingtrader.paper_trading.AlpacaMarketDataProvider",
                return_value=SimpleNamespace(get_bars=Mock(return_value=histories)),
            ) as provider,
            patch("alpaca.data.historical.StockHistoricalDataClient") as data_client,
        ):
            with self.assertRaisesRegex(ValueError, "too correlated with open BAC exposure"):
                submit_paper_candidate(candidate, TradingConfig(), execute=True)

        provider.return_value.get_bars.assert_called_once()
        data_client.assert_not_called()
        trading.submit_order.assert_not_called()

    def test_sector_cap_counts_positions_and_unfilled_pending_order_exposure(self):
        cfg = TradingConfig(account_size=1000, max_sector_exposure_fraction=0.25)
        positions = [SimpleNamespace(symbol="F", market_value="100")]
        pending_orders = [SimpleNamespace(
            symbol="RIVN", side="buy", status="new", qty="20", filled_qty="5", limit_price="10",
        )]

        with self.assertRaisesRegex(ValueError, "Consumer Discretionary sector exposure"):
            enforce_sector_exposure_limit("CCL", 100, positions, pending_orders, cfg)
        enforce_sector_exposure_limit("BAC", 100, positions, pending_orders, cfg)

    def test_sector_cap_fails_closed_when_existing_exposure_cannot_be_verified(self):
        cfg = TradingConfig()
        with self.assertRaisesRegex(ValueError, "market value unavailable"):
            enforce_sector_exposure_limit(
                "F", 100, [SimpleNamespace(symbol="CCL")], [], cfg,
            )

    def test_paper_order_path_enforces_sector_cap_before_submission(self):
        candidate = TradeCandidate(
            symbol="F", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Breakout", reasons=[],
        )
        trading = Mock()
        trading.get_clock.return_value.is_open = True
        trading.get_account.return_value = SimpleNamespace(
            cash="1000", buying_power="1000", trading_blocked=False,
        )
        trading.get_all_positions.return_value = []
        trading.get_orders.return_value = []
        quote = SimpleNamespace(timestamp=datetime.now(timezone.utc), bid_price=9.99, ask_price=10.0)
        cfg = TradingConfig(max_sector_exposure_fraction=0.05)
        with (
            patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")),
            patch("alpaca.trading.client.TradingClient", return_value=trading),
            patch("alpaca.data.historical.StockHistoricalDataClient") as data_client,
        ):
            data_client.return_value.get_stock_latest_quote.return_value = {"F": quote}
            with self.assertRaisesRegex(ValueError, "Consumer Discretionary sector exposure"):
                submit_paper_candidate(candidate, cfg, execute=True)
        trading.submit_order.assert_not_called()

    def test_bot_reports_missing_alpaca_before_loop(self):
        with patch("main.importlib.util.find_spec", return_value=None), \
                patch("main.run_paper_bot_cycle") as cycle:
            with self.assertRaisesRegex(RuntimeError, "not installed in .*python"):
                main.run_paper_bot(TradingConfig())
        cycle.assert_not_called()

    def test_daily_halt_persists_after_equity_recovers(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fills.sqlite3"
            with self.assertRaisesRegex(ValueError, "until next trading day"):
                check_bot_daily_halt(SimpleNamespace(equity="980", last_equity="1000"), TradingConfig(), now, path)
            with self.assertRaisesRegex(ValueError, "until next trading day"):
                check_bot_daily_halt(SimpleNamespace(equity="1010", last_equity="1000"), TradingConfig(), now, path)
            check_bot_daily_halt(SimpleNamespace(equity="1000", last_equity="1000"),
                                 TradingConfig(), now + timedelta(days=1), path)

    def test_bot_lock_rejects_second_instance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            with main.bot_lock(path):
                with self.assertRaisesRegex(ValueError, "Another paper bot"):
                    with main.bot_lock(path):
                        pass

    def test_bot_status_poll_does_not_scan(self):
        account = SimpleNamespace(equity="1000", last_equity="1000")
        trading = Mock()
        trading.get_orders.return_value = []
        trading.get_account.return_value = account
        trading.get_all_positions.return_value = []
        with patch("swingtrader.paper_journal.synchronize_journal", return_value=0), \
            patch("swingtrader.paper_recovery.reconcile_paper_risk", return_value=True), \
            patch("swingtrader.paper_trading.paper_bot_snapshot", return_value=(trading, [])), \
            patch("swingtrader.paper_trading.check_bot_daily_halt"), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(main.run_paper_bot_cycle(TradingConfig(), scan=None))
            trading.get_clock.assert_not_called()

    def test_bot_cli_runs_one_cycle_without_scanner_cli_path(self):
        with TemporaryDirectory() as directory, patch("main.REPORT_DIR", Path(directory)):
            with patch("main.sys.argv", ["main.py", "--paper-bot-once"]), \
                    patch("main.bot_lock", return_value=nullcontext()), \
                    patch("main.run_paper_bot_cycle", return_value=True) as cycle, \
                    patch("main.run_scanner") as scanner, redirect_stdout(io.StringIO()):
                main.main()
        cycle.assert_called_once()
        scanner.assert_not_called()

    def test_bot_rescans_after_fifteen_minutes(self):
        with patch("main.perf_counter", side_effect=[0, 1, 301, 902, 903]), \
                patch("main.run_paper_bot_cycle", side_effect=[True, False, True]) as cycle, \
                patch("main.sleep", side_effect=[None, None, KeyboardInterrupt]), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                main.run_paper_bot(TradingConfig())
        self.assertEqual([call.kwargs["scan"] for call in cycle.call_args_list],
                         [main.run_scanner, None, main.run_scanner])

    def test_closed_or_canceled_symbol_is_not_retried_same_day(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        order = SimpleNamespace(symbol="TEST", client_order_id="swing-paper-TEST-1",
                                created_at=now - timedelta(hours=1), status="canceled")
        self.assertTrue(bot_attempted_today([order], "TEST", now))
        self.assertFalse(bot_attempted_today([order], "NEXT", now))
        self.assertFalse(bot_attempted_today([order], "TEST", now + timedelta(days=1)))

        order.id = "old"
        order.side = "buy"
        order.type = "limit"
        order.order_class = "bracket"
        order.filled_qty = "0"
        trading = Mock()
        trading.get_account.return_value = SimpleNamespace(equity="1000", last_equity="1000")
        trading.get_all_positions.return_value = []
        trading.get_clock.return_value.is_open = True
        candidates = [SimpleNamespace(symbol="TEST"), SimpleNamespace(symbol="NEXT")]
        with patch("swingtrader.paper_trading.paper_bot_snapshot", return_value=(trading, [order])), \
                patch("swingtrader.paper_recovery.reconcile_paper_risk", return_value=True), \
                patch("swingtrader.paper_journal.synchronize_journal", return_value=0), \
                patch("swingtrader.paper_trading.check_bot_daily_halt"), \
                patch("swingtrader.paper_trading.submit_paper_candidate", return_value=SimpleNamespace(
                    symbol="NEXT", shares=1, limit_price=10.0, stop_price=9.0, target_price=10.12)) as submit, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(main.run_paper_bot_cycle(TradingConfig(), scan=Mock(return_value=candidates), now=now))
        submit.assert_called_once_with(candidates[1], TradingConfig(), execute=True, bot_mode=True)

    def test_fill_ledger_records_buy_and_sell_once(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        exit_order = SimpleNamespace(id="exit", status="filled", filled_at=now,
                                     filled_avg_price="10.11", filled_qty="10", side="sell")
        entry = SimpleNamespace(id="entry", client_order_id="swing-paper-TEST-1", symbol="TEST",
                                status="filled", filled_at=now - timedelta(days=1),
                                filled_avg_price="10.00", filled_qty="10", side="buy", legs=[exit_order])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fills.sqlite3"
            self.assertEqual(record_paper_fills([entry], path), 2)
            self.assertEqual(record_paper_fills([entry], path), 0)
            with closing(sqlite3.connect(path)) as database:
                self.assertEqual(database.execute("SELECT side, price FROM fills ORDER BY filled_at").fetchall(),
                                 [("buy", "10.00"), ("sell", "10.11")])

    def test_bot_capacity_counts_protected_positions_and_daily_loss(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        account = SimpleNamespace(equity="1000", last_equity="1000")
        position = SimpleNamespace(symbol="TEST", qty="10", avg_entry_price="10")
        stop = SimpleNamespace(id="stop", side="sell", order_class="simple", legs=None,
                               type="stop", status="new", qty="10", filled_qty="0",
                               stop_price="9", created_at=now - timedelta(days=1))
        target = SimpleNamespace(id="target", side="sell", order_class="simple", legs=None,
                                 type="limit", status="new", qty="10", filled_qty="0",
                                 limit_price="11", created_at=now - timedelta(days=1))
        entry = SimpleNamespace(id="entry", symbol="TEST", client_order_id="swing-paper-TEST-1",
                                status="filled", order_class="bracket", type="limit",
                                filled_at=now - timedelta(days=1), legs=[stop, target], side="buy")
        with patch("swingtrader.paper_trading.position_context", return_value=(entry, [entry, stop, target])):
            capacity = bot_capacity(account, [position], [entry], TradingConfig(), now)
            self.assertEqual(capacity.slots, 4)
            self.assertEqual(capacity.remaining_risk, 90.0)
            stop.status = "held"
            self.assertEqual(bot_capacity(account, [position], [entry], TradingConfig(), now).remaining_risk, 90.0)
            with self.assertRaisesRegex(ValueError, "Daily paper loss"):
                bot_capacity(SimpleNamespace(equity="980", last_equity="1000"), [position], [entry], TradingConfig(), now)
            stop.status = "canceled"
            with self.assertRaisesRegex(ValueError, "exits unverified"):
                bot_capacity(account, [position], [entry], TradingConfig(), now)
            stop.status = "new"
            stop.created_at = now - timedelta(days=85)
            with self.assertRaisesRegex(ValueError, "exits unverified"):
                bot_capacity(account, [position], [entry], TradingConfig(), now)

    def test_bot_capacity_rejects_pending_cancel_and_full_book(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        account = SimpleNamespace(equity="1000", last_equity="1000")
        pending = SimpleNamespace(symbol="TEST", client_order_id="swing-paper-TEST-1",
                                  status="pending_cancel", side="buy")
        with self.assertRaisesRegex(ValueError, "change pending"):
            bot_capacity(account, [], [pending], TradingConfig(), now)
        pending.status = "new"
        pending.limit_price = "10"
        pending.qty = "10"
        pending.filled_qty = "0"
        pending.legs = [SimpleNamespace(type="stop", stop_price="9")]
        book = [pending]
        for index in range(4):
            book.append(SimpleNamespace(symbol=f"TEST{index}", client_order_id=f"swing-paper-TEST{index}-1",
                                        status="new", side="buy", limit_price="10", qty="10",
                                        filled_qty="0", legs=[SimpleNamespace(type="stop", stop_price="9")]))
        capacity = bot_capacity(account, [], book, TradingConfig(), now)
        self.assertEqual(capacity.slots, 0)
        self.assertEqual(capacity.remaining_risk, 50.0)

    def test_bot_cycle_scans_and_submits_with_guard(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        candidate = SimpleNamespace(symbol="TEST")
        trading = Mock()
        trading.get_account.return_value = SimpleNamespace(equity="1000", last_equity="1000")
        trading.get_all_positions.return_value = []
        trading.get_clock.return_value.is_open = True
        scan = Mock(return_value=[candidate])
        with patch("swingtrader.paper_trading.paper_bot_snapshot", return_value=(trading, [])), \
                patch("swingtrader.paper_recovery.reconcile_paper_risk", return_value=True), \
                patch("swingtrader.paper_journal.synchronize_journal", return_value=0), \
            patch("swingtrader.paper_trading.check_bot_daily_halt"), \
                patch("swingtrader.paper_trading.submit_paper_candidate", return_value=SimpleNamespace(
                    symbol="TEST", shares=1, limit_price=10.0, stop_price=9.0, target_price=10.11)) as submit, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(main.run_paper_bot_cycle(TradingConfig(), scan=scan, now=now))
        scan.assert_called_once()
        submit.assert_called_once_with(candidate, TradingConfig(), execute=True, bot_mode=True)

    def test_bot_rechecks_broker_before_submitting(self):
        candidate = TradeCandidate(
            symbol="TEST", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Momentum", reasons=[],
        )
        account = SimpleNamespace(equity="980", last_equity="1000", cash="980", buying_power="980",
                                  trading_blocked=False)
        trading = Mock()
        trading.get_clock.return_value.is_open = True
        trading.get_account.return_value = account
        trading.get_all_positions.return_value = []
        trading.get_orders.return_value = []
        quote = SimpleNamespace(timestamp=datetime.now(timezone.utc), bid_price=9.99, ask_price=10.0)
        with patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")), \
                patch("alpaca.trading.client.TradingClient", return_value=trading), \
            patch("swingtrader.paper_trading.check_bot_daily_halt"), \
                patch("alpaca.data.historical.StockHistoricalDataClient") as data_client:
            data_client.return_value.get_stock_latest_quote.return_value = {"TEST": quote}
            with self.assertRaisesRegex(ValueError, "Daily paper loss"):
                submit_paper_candidate(candidate, TradingConfig(), execute=True, bot_mode=True)
        trading.submit_order.assert_not_called()

    def test_bot_does_not_reorder_symbol_after_scan(self):
        candidate = TradeCandidate(
            symbol="TEST", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Momentum", reasons=[],
        )
        trading = Mock()
        trading.get_clock.return_value.is_open = True
        trading.get_account.return_value = SimpleNamespace(
            equity="1000", last_equity="1000", cash="1000", buying_power="1000", trading_blocked=False,
        )
        trading.get_all_positions.return_value = []
        order = SimpleNamespace(id="old", symbol="TEST", client_order_id="swing-paper-TEST-1",
                                created_at=datetime.now(timezone.utc), status="canceled")
        trading.get_orders.side_effect = [[], [order]]
        quote = SimpleNamespace(timestamp=datetime.now(timezone.utc), bid_price=9.99, ask_price=10.0)
        with patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")), \
                patch("swingtrader.paper_trading.check_bot_daily_halt"), \
                patch("alpaca.trading.client.TradingClient", return_value=trading), \
                patch("alpaca.data.historical.StockHistoricalDataClient") as data_client:
            data_client.return_value.get_stock_latest_quote.return_value = {"TEST": quote}
            with self.assertRaisesRegex(ValueError, "already attempted"):
                submit_paper_candidate(candidate, TradingConfig(), execute=True, bot_mode=True)
        trading.submit_order.assert_not_called()

    def test_paper_status_does_not_scan_or_submit(self):
        with TemporaryDirectory() as directory, patch("main.REPORT_DIR", Path(directory)):
            with patch("main.sys.argv", ["main.py", "--paper-status"]), \
                    patch("main.run_scanner") as scan, \
                    patch("swingtrader.paper_trading.report_paper_status") as status, \
                    redirect_stdout(io.StringIO()):
                main.main()
        scan.assert_not_called()
        status.assert_called_once_with(cancel_stale=False)

    def test_only_expired_unfilled_paper_entry_is_canceled(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        def entry(order_id, filled_qty, age_minutes, status="new"):
            return SimpleNamespace(
                id=order_id, client_order_id=f"swing-paper-TEST-{order_id}", symbol="TEST",
                side="buy", type="limit", order_class="bracket", status=status,
                filled_qty=filled_qty, created_at=now - timedelta(minutes=age_minutes), legs=None,
            )

        trading = Mock()
        messages = review_paper_orders(
            trading, [entry("old", "0", 11), entry("partial", "1", 11, "partially_filled"),
                      entry("new", "0", 3), entry("filled", "2", 11, "filled")], now, cancel_stale=True,
        )

        trading.cancel_order_by_id.assert_called_once_with("old")
        self.assertTrue(any("partial" in message and "manual" in message for message in messages))
        self.assertTrue(any("filled" in message and "manual" in message for message in messages))

    def test_status_review_does_not_cancel_stale_entries(self):
        now = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        entry = SimpleNamespace(
            id="old", client_order_id="swing-paper-TEST-old", symbol="TEST",
            side="buy", type="limit", order_class="bracket", status="new",
            filled_qty="0", created_at=now - timedelta(minutes=11), legs=None,
        )
        trading = Mock()

        messages = review_paper_orders(trading, [entry], now)

        trading.cancel_order_by_id.assert_not_called()
        self.assertTrue(any("old" in message and "stale" in message for message in messages))

    def test_example_covers_all_trading_settings(self):
        example = Path(__file__).resolve().parent.parent / "config.example.py"
        tree = ast.parse(example.read_text(encoding="utf-8"))
        config_call = next(
            assignment.value for assignment in tree.body
            if isinstance(assignment, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "TRADING_CONFIG" for target in assignment.targets)
        )
        self.assertIsInstance(config_call, ast.Call)
        self.assertEqual(
            {field.name for field in fields(TradingConfig)},
            {keyword.arg for keyword in config_call.keywords},
        )
        settings = run_path(str(example))
        self.assertIsInstance(settings["TRADING_CONFIG"], TradingConfig)
        self.assertEqual(settings["APCA_API_BASE_URL"], "https://paper-api.alpaca.markets")
        self.assertEqual(settings["APCA_API_KEY_ID"], "")
        self.assertEqual(settings["APCA_API_SECRET_KEY"], "")

    def test_loads_local_trading_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.py"
            path.write_text(
                "from swingtrader.config import TradingConfig\n"
                "TRADING_CONFIG = TradingConfig(account_size=2000.0, risk_fraction=0.005)\n",
                encoding="utf-8",
            )
            config = load_trading_config(path)
            self.assertEqual(config.account_size, 2000.0)
            self.assertEqual(config.risk_fraction, 0.005)

    def test_cli_uses_local_trading_settings(self):
        with patch("main.load_trading_config", return_value=TradingConfig(minimum_price=8.0, risk_fraction=0.005)):
            config = main.build_config(maximum_price=20.0)
        self.assertEqual(config.minimum_price, 8.0)
        self.assertEqual(config.maximum_price, 20.0)
        self.assertEqual(config.risk_fraction, 0.005)

    def test_loads_paper_credentials_from_local_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.py"
            path.write_text("APCA_API_KEY_ID = 'paper-key'\nAPCA_API_SECRET_KEY = 'paper-secret'\n", encoding="utf-8")

            self.assertEqual(load_paper_credentials(path), ("paper-key", "paper-secret"))

    def test_rejects_non_paper_base_url(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.py"
            path.write_text(
                "APCA_API_BASE_URL = 'https://api.alpaca.markets'\n"
                "APCA_API_KEY_ID = 'paper-key'\nAPCA_API_SECRET_KEY = 'paper-secret'\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "paper endpoint"):
                load_paper_credentials(path)

    def test_wide_spread_is_rejected(self):
        candidate = TradeCandidate(
            symbol="TEST", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Momentum",
            reasons=[],
        )

        with self.assertRaisesRegex(ValueError, "spread"):
            plan_paper_order(candidate, bid=9.50, ask=10.50, cfg=TradingConfig())

    def test_default_spread_limit_preserves_small_target(self):
        candidate = TradeCandidate(
            symbol="TEST", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Momentum",
            reasons=[],
        )

        with self.assertRaisesRegex(ValueError, "spread"):
            plan_paper_order(candidate, bid=9.97, ask=10.00, cfg=TradingConfig())
        self.assertEqual(plan_paper_order(candidate, bid=9.99, ask=10.00, cfg=TradingConfig()).symbol, "TEST")

    def test_paper_take_profit_uses_configured_target_and_cost_allowance(self):
        candidate = TradeCandidate(
            symbol="TEST", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10.0, stop=9.0, target_1pct=10.1,
            target_1=11.5, target_2=13.0, shares=10, risk_dollars=10.0,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Momentum",
            reasons=[],
        )

        plan = plan_paper_order(candidate, bid=9.99, ask=10.0, cfg=TradingConfig())
        self.assertEqual(plan.target_price, 10.12)
        self.assertGreaterEqual(plan.target_price * (1 - TradingConfig().estimated_exit_cost_fraction) / plan.limit_price - 1, 0.01)
        custom = plan_paper_order(candidate, bid=9.99, ask=10.0,
                                  cfg=TradingConfig(target_percent=2.0, estimated_exit_cost_fraction=0.002))
        self.assertEqual(custom.target_price, 10.23)


if __name__ == "__main__":
    unittest.main()