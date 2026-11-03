import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from swingtrader.asset_snapshot import (
    fetch_and_persist_alpaca_asset_snapshot,
    persist_asset_snapshot,
)


def asset(symbol, status="active"):
    return SimpleNamespace(
        symbol=symbol,
        name=f"{symbol} Inc.",
        exchange="NASDAQ",
        status=status,
        asset_class="us_equity",
        tradable=status == "active",
        marginable=True,
        shortable=False,
        easy_to_borrow=False,
        fractionable=True,
    )


class AlpacaAssetSnapshotTests(unittest.TestCase):
    def test_persists_multiple_dated_snapshots_without_conflating_status(self):
        retrieved_at = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "assets.sqlite3"
            first_id, first_count = persist_asset_snapshot(
                [asset("TEST")], retrieved_at=retrieved_at, path=path,
            )
            second_id, second_count = persist_asset_snapshot(
                [asset("TEST", "inactive")],
                retrieved_at=datetime(2026, 10, 19, 12, tzinfo=timezone.utc),
                path=path,
            )
            with closing(sqlite3.connect(path)) as database:
                snapshots = database.execute(
                    "SELECT snapshot_id, retrieved_at, asset_count FROM asset_snapshots "
                    "ORDER BY retrieved_at"
                ).fetchall()
                observations = database.execute(
                    "SELECT o.snapshot_id, o.symbol, o.status, o.tradable "
                    "FROM asset_observations o JOIN asset_snapshots s USING (snapshot_id) "
                    "ORDER BY s.retrieved_at"
                ).fetchall()

        self.assertEqual((first_count, second_count), (1, 1))
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0][1], retrieved_at.isoformat())
        self.assertEqual([row[2:] for row in observations], [("active", 1), ("inactive", 0)])

    def test_refuses_empty_and_duplicate_symbol_snapshots(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "assets.sqlite3"
            with self.assertRaisesRegex(ValueError, "no US-equity assets"):
                persist_asset_snapshot([], path=path)
            with self.assertRaisesRegex(ValueError, "duplicate symbol: TEST"):
                persist_asset_snapshot([asset("TEST"), asset("test")], path=path)
            self.assertFalse(path.exists())

    def test_fetch_uses_paper_client_and_requests_us_equities(self):
        assets = [asset("TEST")]
        trading = SimpleNamespace(get_all_assets=Mock(return_value=assets))
        with (
            patch("swingtrader.paper_trading.load_paper_credentials", return_value=("key", "secret")),
            patch("alpaca.trading.client.TradingClient", return_value=trading) as client_class,
            TemporaryDirectory() as directory,
        ):
            snapshot_id, count = fetch_and_persist_alpaca_asset_snapshot(
                path=Path(directory) / "assets.sqlite3",
            )

        self.assertTrue(snapshot_id)
        self.assertEqual(count, 1)
        client_class.assert_called_once_with("key", "secret", paper=True)
        from alpaca.trading.enums import AssetClass

        request = trading.get_all_assets.call_args.args[0]
        self.assertEqual(request.asset_class, AssetClass.US_EQUITY)

    def test_cli_snapshot_mode_persists_without_scanning(self):
        with TemporaryDirectory() as directory:
            with (
                patch("main.REPORT_DIR", Path(directory)),
                patch("main.sys.argv", ["main.py", "--snapshot-alpaca-assets"]),
                patch(
                    "swingtrader.asset_snapshot.fetch_and_persist_alpaca_asset_snapshot",
                    return_value=("snapshot-id", 123),
                ) as snapshot,
                patch("main.run_scanner") as scanner,
            ):
                main.main()

        snapshot.assert_called_once_with()
        scanner.assert_not_called()

    def test_cli_rejects_snapshot_combined_with_backtest(self):
        with TemporaryDirectory() as directory:
            with (
                patch("main.REPORT_DIR", Path(directory)),
                patch(
                    "main.sys.argv",
                    ["main.py", "--snapshot-alpaca-assets", "--backtest"],
                ),
                patch("main.run_alpaca_asset_snapshot") as snapshot,
            ):
                with self.assertRaises(SystemExit) as raised:
                    main.main()

        self.assertEqual(raised.exception.code, 2)
        snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
