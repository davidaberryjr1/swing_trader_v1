import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.common.exceptions import APIError

import main
from swingtrader.config import TradingConfig
from swingtrader.paper_journal import (
    format_paper_performance, journal, record_paper_fills, save_entry_plan, synchronize_journal,
)
from swingtrader.paper_recovery import fetch_paper_orders, reconcile_paper_risk
from swingtrader.paper_trading import PaperOrderPlan, bot_capacity, submit_paper_candidate
from swingtrader.scanner import TradeCandidate


NOW = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)


def order(order_id, **values):
    data = dict(
        id=order_id, client_order_id=f"broker-{order_id}", symbol="F",
        type="limit", side="sell", order_class="simple", status="new", qty="10",
        filled_qty="0", filled_avg_price=None, filled_at=None,
        created_at=NOW - timedelta(days=1), updated_at=NOW, expires_at=None,
        legs=None, stop_price=None, limit_price="10.11",
    )
    data.update(values)
    return SimpleNamespace(**data)


def entry(**values):
    data = dict(
        client_order_id="swing-paper-F-20261022", side="buy", order_class="bracket",
        status="filled", filled_qty="10", filled_avg_price="10", filled_at=NOW - timedelta(days=1),
        legs=[order("stop", type="stop", stop_price="9", limit_price=None), order("target")],
    )
    data.update(values)
    return order("entry", **data)


def position(**values):
    data = dict(symbol="F", qty="10", avg_entry_price="10", market_value="101", unrealized_pl="1")
    data.update(values)
    return SimpleNamespace(**data)


def api_error(status):
    return APIError(
        json.dumps({"code": status * 10000, "message": "test rejection"}),
        SimpleNamespace(response=SimpleNamespace(status_code=status)),
    )


class PaperAutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper.sqlite3"
        self.cfg = TradingConfig()
        self.trading = Mock()
        self.trading.get_clock.return_value.is_open = True

    def reconcile(self, entries, positions=None):
        return reconcile_paper_risk(
            self.trading, [position()] if positions is None else positions,
            entries, NOW, self.cfg, self.path,
        )

    def test_healthy_bracket_needs_no_mutations(self):
        self.assertTrue(self.reconcile([entry()]))
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_missing_exits_submit_oco_and_wait_for_confirmation(self):
        original = entry()
        for leg in original.legs:
            leg.status = "canceled"
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.order_class.value, "oco")
        self.assertEqual(request.qty, 10)
        self.assertEqual(request.stop_loss.stop_price, 9)
        self.assertEqual(request.take_profit.limit_price, 10.11)
        with journal(self.path) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0], 1)
        self.trading.get_order_by_client_id.side_effect = api_error(404)
        self.assertFalse(self.reconcile([original]))
        retried = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.client_order_id, retried.client_order_id)
        self.assertEqual(self.trading.submit_order.call_count, 2)

    def test_acknowledgement_timeout_keeps_same_client_id_across_restart(self):
        original = entry(legs=[])
        self.trading.submit_order.side_effect = TimeoutError("ambiguous submission")
        with self.assertRaises(TimeoutError):
            self.reconcile([original])
        first = self.trading.submit_order.call_args.kwargs["order_data"]
        self.trading.submit_order.side_effect = None
        self.trading.get_order_by_client_id.side_effect = api_error(404)
        self.assertFalse(self.reconcile([original]))
        self.assertEqual(first.client_order_id, self.trading.submit_order.call_args.kwargs["order_data"].client_order_id)

    def test_existing_broker_acknowledgement_prevents_duplicate_close(self):
        original = entry(legs=[])
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        acknowledged = order("close", client_order_id=request.client_order_id, type="market")
        self.trading.get_order_by_client_id.return_value = acknowledged
        self.assertFalse(self.reconcile([original]))
        self.assertEqual(self.trading.submit_order.call_count, 1)

    def test_working_emergency_close_is_not_canceled(self):
        original = entry(legs=[])
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        closing = order("close", client_order_id=request.client_order_id, type="market")
        self.assertFalse(self.reconcile([original, closing]))
        self.trading.cancel_order_by_id.assert_not_called()
        self.assertEqual(self.trading.submit_order.call_count, 1)

    def test_rejected_protection_falls_back_to_close_after_confirmation(self):
        original = entry()
        for leg in original.legs:
            leg.status = "canceled"
        self.trading.submit_order.side_effect = api_error(422)
        self.trading.get_order_by_client_id.side_effect = api_error(404)
        with self.assertRaises(APIError):
            self.reconcile([original])
        self.trading.submit_order.side_effect = None
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.type.value, "market")
        self.assertEqual(request.side.value, "sell")

    def test_unknown_position_never_liquidated(self):
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.reconcile([], [position(symbol="MANUAL")])
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_same_symbol_manual_quantity_blocks_recovery(self):
        with self.assertRaisesRegex(ValueError, "quantity differs"):
            self.reconcile([entry()], [position(qty="11")])
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_unrelated_order_blocks_cancel_and_sell(self):
        original = entry(legs=[])
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.reconcile([original, order("manual")])
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_partial_entry_cancels_remainder_before_reprotecting(self):
        original = entry(status="partially_filled", filled_qty="4", filled_at=None)
        self.assertFalse(self.reconcile([original], [position(qty="4")]))
        self.trading.cancel_order_by_id.assert_called_once_with("entry")
        self.trading.submit_order.assert_not_called()
        original.status = "canceled"
        for leg in original.legs:
            leg.status = "canceled"
        self.assertFalse(self.reconcile([original], [position(qty="4")]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.qty, 4)

    def test_cancellation_must_be_confirmed_before_replacement(self):
        original = entry()
        original.legs[0].status = "pending_cancel"
        self.assertFalse(self.reconcile([original]))
        self.trading.submit_order.assert_not_called()
        self.trading.cancel_order_by_id.assert_not_called()

    def test_nearing_expiry_cancels_before_renewal(self):
        original = entry()
        original.legs[0].expires_at = NOW + timedelta(days=4)
        self.assertFalse(self.reconcile([original]))
        self.trading.cancel_order_by_id.assert_called_once()
        self.trading.submit_order.assert_not_called()

    def test_maximum_hold_closes_after_cancellations(self):
        original = entry(filled_at=NOW - timedelta(days=180))
        for leg in original.legs:
            leg.status = "canceled"
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.type.value, "market")

    def test_outside_hours_leaves_existing_protection_untouched(self):
        original = entry()
        original.legs[0].expires_at = NOW + timedelta(days=2)
        self.trading.get_clock.return_value.is_open = False
        self.assertFalse(self.reconcile([original]))
        self.trading.cancel_order_by_id.assert_not_called()
        self.trading.submit_order.assert_not_called()

    def test_fill_position_disagreement_blocks_entries(self):
        with self.assertRaisesRegex(ValueError, "positions and bot fills disagree"):
            self.reconcile([entry()], [])
        self.trading.submit_order.assert_not_called()

    def test_orphaned_exit_is_canceled_before_another_entry(self):
        original = entry()
        original.legs[1].status = "filled"
        original.legs[1].filled_qty = "10"
        original.legs[1].filled_avg_price = "10.11"
        original.legs[1].filled_at = NOW
        self.assertFalse(self.reconcile([original], []))
        self.trading.cancel_order_by_id.assert_called_once_with("stop")
        self.trading.submit_order.assert_not_called()

    def test_unfilled_bracket_children_are_not_orphaned(self):
        original = entry(status="new", filled_qty="0", filled_avg_price=None,
                         filled_at=None, created_at=NOW)
        self.assertTrue(self.reconcile([original], []))
        self.trading.cancel_order_by_id.assert_not_called()

    def test_wrong_sized_target_requires_recovery(self):
        original = entry()
        original.legs[1].qty = "11"
        self.assertFalse(self.reconcile([original]))
        self.trading.cancel_order_by_id.assert_called_once()
        self.trading.submit_order.assert_not_called()

    def test_expiry_fallback_threshold_is_eighty_days(self):
        original = entry()
        original.legs[0].created_at = NOW - timedelta(days=80)
        self.assertFalse(self.reconcile([original]))
        self.trading.cancel_order_by_id.assert_called_once()

    def test_missing_prices_trigger_verified_position_close(self):
        original = entry()
        original.legs[0].stop_price = None
        for leg in original.legs:
            leg.status = "canceled"
        self.assertFalse(self.reconcile([original]))
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.type.value, "market")

    def test_partially_exited_position_reports_realized_pnl_without_closing_trade(self):
        original = entry()
        original.legs[1].status = "partially_filled"
        original.legs[1].filled_qty = "4"
        original.legs[1].filled_avg_price = "11"
        record_paper_fills([original], self.path)
        report = format_paper_performance(self.path)
        self.assertIn("Closed trades: 0", report)
        self.assertIn("$4.00", report)
        self.assertIn("10.00%", report)
        self.assertIn("open / partially exited", report)

    def test_repaired_oco_counts_for_risk_capacity_and_fill_ledger(self):
        original = entry()
        for leg in original.legs:
            leg.status = "canceled"
        self.reconcile([original])
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        repaired = order(
            "repair", client_order_id=request.client_order_id, order_class="oco",
            legs=[order("new-stop", type="stop", stop_price="9", limit_price=None)],
        )
        self.assertTrue(self.reconcile([original, repaired]))
        with patch("swingtrader.paper_trading.position_context", return_value=(original, [repaired, *repaired.legs])):
            capacity = bot_capacity(
                SimpleNamespace(equity="1000", last_equity="1000"),
                [position()], [original, repaired], self.cfg, NOW,
            )
        self.assertEqual(capacity.remaining_risk, 90)
        repaired.status, repaired.filled_qty = "filled", "10"
        repaired.filled_avg_price, repaired.filled_at = "10.11", NOW
        repaired.legs[0].status = "canceled"
        self.assertEqual(record_paper_fills([original, repaired], self.path), 2)
        self.assertIn("Closed trades: 1", format_paper_performance(self.path))
        self.assertIn("$1.10", format_paper_performance(self.path))

    def test_cumulative_partial_fills_update_without_double_counting(self):
        original = entry(status="partially_filled", filled_qty="4", filled_at=None, legs=[])
        self.assertEqual(record_paper_fills([original], self.path), 1)
        self.assertEqual(record_paper_fills([original], self.path), 0)
        original.status, original.filled_qty = "filled", "10"
        original.filled_avg_price, original.filled_at = "10.02", NOW
        self.assertEqual(record_paper_fills([original], self.path), 1)
        with journal(self.path) as database:
            self.assertEqual(database.execute("SELECT qty, price FROM fills").fetchone()[:], ("10", "10.02"))
            self.assertEqual(database.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        original.filled_qty = "9"
        with self.assertRaisesRegex(ValueError, "backwards"):
            record_paper_fills([original], self.path)

    def test_existing_fill_database_is_migrated_without_losing_records(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("CREATE TABLE fills (order_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, "
                             "symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, "
                             "price TEXT NOT NULL, filled_at TEXT NOT NULL)")
            database.execute("INSERT INTO fills VALUES ('legacy', 'legacy', 'F', 'buy', '1', '10', ?)",
                             (NOW.isoformat(),))
        self.assertEqual(record_paper_fills([entry()], self.path), 1)
        with journal(self.path) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)

    def test_sync_binds_account_and_records_observed_equity(self):
        account = SimpleNamespace(id="paper-A", equity="1000", cash="900")
        synchronize_journal(account, [position()], [entry()], NOW, self.path)
        account.equity = "980"
        synchronize_journal(account, [position()], [entry()], NOW + timedelta(minutes=1), self.path)
        report = format_paper_performance(self.path)
        self.assertIn("2.00%", report)
        self.assertIn("Last successful broker synchronization: 2026-10-23", report)
        self.assertIn("not bot-only returns", report)
        account.id = "paper-B"
        with self.assertRaisesRegex(ValueError, "another account"):
            synchronize_journal(account, [], [], NOW, self.path)

    def test_performance_read_only_does_not_create_database(self):
        self.assertIn("No paper journal", format_paper_performance(self.path))
        self.assertFalse(self.path.exists())

    def test_performance_rejects_unmatched_sales(self):
        original = entry(legs=[order("sold", status="filled", filled_qty="11",
                                    filled_avg_price="11", filled_at=NOW)])
        record_paper_fills([original], self.path)
        with self.assertRaisesRegex(ValueError, "unmatched"):
            format_paper_performance(self.path)

    def test_journal_saves_strategy_and_prevents_duplicate_daily_attempt(self):
        candidate = TradeCandidate(
            symbol="F", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10, stop=9, target_1pct=10.1,
            target_1=11.5, target_2=13, shares=10, risk_dollars=10,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Breakout", reasons=["test"],
        )
        plan = PaperOrderPlan("F", 10, 10, 9, 10.11, 10)
        save_entry_plan("daily", candidate, plan, self.cfg, self.path)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            save_entry_plan("daily", candidate, plan, self.cfg, self.path)
        with journal(self.path) as database:
            row = database.execute("SELECT * FROM entry_plans").fetchone()
            self.assertEqual(json.loads(row["config_json"])["target_percent"], 1)
            self.assertEqual(json.loads(row["candidate_json"])["reasons"], ["test"])

    def test_uncertain_buy_is_journaled_before_submission_and_not_duplicated(self):
        candidate = TradeCandidate(
            symbol="F", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10, stop=9, target_1pct=10.1,
            target_1=11.5, target_2=13, shares=10, risk_dollars=10,
            reward_risk=1.5, main_catalyst="Technical Trend", setup="Breakout", reasons=[],
        )
        self.trading.get_account.return_value = SimpleNamespace(
            cash="1000", buying_power="1000", trading_blocked=False,
        )
        self.trading.get_all_positions.return_value = []
        self.trading.get_orders.return_value = []
        self.trading.submit_order.side_effect = TimeoutError("unknown acknowledgement")
        quote = SimpleNamespace(
            timestamp=datetime.now(timezone.utc), bid_price=9.99, ask_price=10,
        )
        with patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")), \
                patch("alpaca.trading.client.TradingClient", return_value=self.trading), \
                patch("alpaca.data.historical.StockHistoricalDataClient") as data, \
                patch("swingtrader.paper_trading.save_entry_plan",
                      side_effect=lambda *args: save_entry_plan(*args, path=self.path)):
            data.return_value.get_stock_latest_quote.return_value = {"F": quote}
            with self.assertRaises(TimeoutError):
                submit_paper_candidate(candidate, self.cfg, execute=True)
            with self.assertRaisesRegex(ValueError, "already attempted"):
                submit_paper_candidate(candidate, self.cfg, execute=True)
        self.trading.submit_order.assert_called_once()
        request = self.trading.submit_order.call_args.kwargs["order_data"]
        with journal(self.path) as database:
            self.assertEqual(database.execute("SELECT client_order_id FROM entry_plans").fetchone()[0],
                             request.client_order_id)

    def test_history_pagination_overlaps_boundary_without_losing_orders(self):
        first = [order(str(index), created_at=NOW - timedelta(seconds=index)) for index in range(500)]
        self.trading.get_orders.side_effect = [first, [first[-1], order("older")]]
        result = fetch_paper_orders(self.trading)
        self.assertEqual(len(result), 501)
        second = self.trading.get_orders.call_args.kwargs["filter"]
        self.assertEqual(second.until, first[-1].created_at + timedelta(microseconds=1))

    def test_history_pagination_stalls_fail_explicitly(self):
        page = [order(str(index)) for index in range(500)]
        self.trading.get_orders.return_value = page
        with self.assertRaisesRegex(ValueError, "no progress"):
            fetch_paper_orders(self.trading)

    def test_research_mode_preserves_grid_without_broker_execution(self):
        with patch("main.REPORT_DIR", Path(self.directory.name)), \
                patch("main.sys.argv", ["main.py", "--research"]), \
                patch("main.build_config", return_value=self.cfg), \
                patch("main.run_scanner", return_value=[]) as scan, \
                patch("swingtrader.paper_trading.submit_paper_candidate") as submit, \
                patch("swingtrader.paper_trading.paper_bot_snapshot") as snapshot, \
                redirect_stdout(io.StringIO()):
            main.main()
        scan.assert_called_once()
        submit.assert_not_called()
        snapshot.assert_not_called()

    def test_research_grid_keeps_candidate_columns(self):
        candidate = SimpleNamespace(
            symbol="F", score=70, prob_gain_10d=0.5, prob_loss_5d=0.2,
            risk_rating="Medium", entry=10, stop=9, target_1pct=10.1,
            target_1=11.5, target_2=13, shares=10, risk_dollars=10,
            main_catalyst="Technical Trend", reasons=["test"], price_change_pct=None,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            main.print_candidates_table([candidate], cfg=self.cfg)
        self.assertIn("| Rank | Symbol | Score | P(+10%) | P(-5%) |", output.getvalue())
        self.assertIn("| Target 1 (+8-10%) | Target 2 (+16-20%) |", output.getvalue())
        self.assertIn("| 1 | **F** | 70/100 |", output.getvalue())

    def test_performance_cli_never_loads_credentials_or_scans(self):
        with patch("main.REPORT_DIR", Path(self.directory.name)), \
                patch("main.sys.argv", ["main.py", "--paper-performance"]), \
                patch("swingtrader.paper_journal.format_paper_performance", return_value="report") as report, \
                patch("main.run_scanner") as scan, \
                patch("main.build_config") as config, \
                patch("swingtrader.paper_trading.load_paper_credentials") as credentials, \
                redirect_stdout(io.StringIO()):
            main.main()
        report.assert_called_once()
        scan.assert_not_called()
        config.assert_not_called()
        credentials.assert_not_called()

    def test_research_and_paper_flags_are_incompatible(self):
        with patch("main.REPORT_DIR", Path(self.directory.name)), \
                patch("main.sys.argv", ["main.py", "--research", "--paper-bot-once"]), \
                redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main.main()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
