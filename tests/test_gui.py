import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import main
import pandas as pd
from main import (
    GUI_COLUMN_LABELS,
    adjacent_symbol_index,
    candidate_info_rows,
    format_candidate_info_line,
    gui_result_row_height,
    load_cached_daily_close,
    load_latest_gui_run_cache,
    monthly_price_markers,
    monthly_price_label_slots,
    quarter_dollar_price_ticks,
    simple_moving_average_points,
    save_gui_run_cache,
    update_candidate_gui_fields,
)
from swingtrader.scanner import TradeCandidate


def make_candidate(symbol="TEST"):
    return TradeCandidate(
        symbol=symbol,
        score=82,
        prob_gain_10d=0.64,
        prob_loss_5d=0.12,
        risk_rating="Low",
        entry=12.34,
        stop=11.50,
        target_1pct=12.46,
        target_1=14.00,
        target_2=15.00,
        shares=10,
        risk_dollars=8.40,
        reward_risk=2.0,
        main_catalyst="Company News",
        setup="Breakout",
        reasons=["Strong trend", "Positive catalyst"],
    )


class ScannerGuiTests(unittest.TestCase):
    @patch("main.launch_gui")
    @patch("main.build_config")
    def test_no_arguments_launch_gui(self, mock_build_config, mock_launch_gui):
        config = object()
        mock_build_config.return_value = config

        main.main([])

        mock_build_config.assert_called_once_with(minimum_price=None, maximum_price=None)
        mock_launch_gui.assert_called_once_with(
            cfg=config,
            symbols=None,
            output_path=None,
            top=25,
        )

    @patch("main.launch_gui")
    @patch("main.build_config")
    def test_gui_mode_starts_application_with_scanner_options(self, mock_build_config, mock_launch_gui):
        config = object()
        mock_build_config.return_value = config

        main.main(["--gui", "--symbols", "TEST", "--top", "1"])

        mock_build_config.assert_called_once_with(minimum_price=None, maximum_price=None)
        mock_launch_gui.assert_called_once_with(
            cfg=config,
            symbols=["TEST"],
            output_path=None,
            top=1,
        )

    def test_scan_results_populate_matching_fields_and_clear_unused_rows(self):
        candidate = make_candidate()
        rows = [
            {
                label: SimpleNamespace(text="old", disabled=True)
                for label in GUI_COLUMN_LABELS
            }
            for _ in range(2)
        ]

        update_candidate_gui_fields(rows, [candidate])

        self.assertEqual(rows[0]["Rank"].text, "1")
        self.assertFalse(rows[0]["Rank"].disabled)
        self.assertEqual(
            {label: field.text for label, field in rows[0].items()},
            {
                "Rank": "1",
                "Symbol": "TEST",
                "Score": "82",
                "P(+10%)": "64.0%",
                "P(-5%)": "12.0%",
                "Risk": "Low",
                "Entry": "$12.34",
                "Stop": "$11.50",
                "+1% Target": "$12.46",
                "Target 1 (+8-10%)": "$14.00",
                "Target 2 (+16-20%)": "$15.00",
                "Shares": "10",
                "Max Risk": "$8.40",
                "Catalyst": "Company News",
                "Why Attractive": "Strong trend; Positive catalyst",
            },
        )
        self.assertTrue(all(field.text == "" for field in rows[1].values()))
        self.assertTrue(rows[1]["Rank"].disabled)

    def test_symbol_info_rows_include_all_non_rank_columns(self):
        candidate = make_candidate()

        rows = candidate_info_rows(candidate, 3)

        self.assertEqual(
            rows,
            [
                ("Score", "82"),
                ("P(+10%)", "64.0%"),
                ("P(-5%)", "12.0%"),
                ("Risk", "Low"),
                ("Entry", "$12.34"),
                ("Stop", "$11.50"),
                ("+1% Target", "$12.46"),
                ("Target 1 (+8-10%)", "$14.00"),
                ("Target 2 (+16-20%)", "$15.00"),
                ("Shares", "10"),
                ("Max Risk", "$8.40"),
                ("Catalyst", "Company News"),
                ("Why Attractive", "Strong trend; Positive catalyst"),
            ],
        )

    def test_symbol_info_line_includes_column_header_and_colon(self):
        self.assertEqual(
            format_candidate_info_line("Entry", "$12.34"),
            "Entry: $12.34",
        )

    def test_run_cache_round_trip_loads_most_recent_results(self):
        first_run = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
        latest_run = first_run + timedelta(hours=1)
        first_candidate = make_candidate()
        latest_candidate = replace(first_candidate, symbol="LATEST")

        with TemporaryDirectory() as cache_dir:
            save_gui_run_cache([first_candidate], cache_dir, first_run)
            latest_path = save_gui_run_cache([latest_candidate], cache_dir, latest_run)

            cached_run = load_latest_gui_run_cache(cache_dir)

        self.assertEqual(latest_path.name, "gui_scan_20261030_130000_000000.json")
        self.assertIsNotNone(cached_run)
        candidates, run_at = cached_run
        self.assertEqual(run_at, latest_run)
        self.assertEqual(candidates, [latest_candidate])

    def test_daily_graph_data_uses_cached_one_year_close_series(self):
        dates = pd.to_datetime(["2024-01-01", "2025-01-01", "2025-06-01"])
        history = pd.DataFrame(
            {"Close": [10.0, 20.0, 25.0]},
            index=dates,
        )

        with TemporaryDirectory() as cache_dir:
            history.to_pickle(Path(cache_dir) / "TEST__1y.pkl")
            cached_close = load_cached_daily_close("TEST", cache_dir)

        self.assertIsNotNone(cached_close)
        self.assertEqual(cached_close.tolist(), [20.0, 25.0])

    def test_daily_graph_data_includes_199_sma_warmup_sessions(self):
        dates = pd.bdate_range(end="2025-12-31", periods=600)
        history = pd.DataFrame({"Close": range(len(dates))}, index=dates)
        cutoff = dates[-1] - pd.Timedelta(days=365)
        visible_start = dates.searchsorted(cutoff)

        with TemporaryDirectory() as cache_dir:
            history.to_pickle(Path(cache_dir) / "TEST__1y.pkl")
            cached_close = load_cached_daily_close(
                "TEST",
                cache_dir,
                include_sma_warmup=True,
            )

        self.assertIsNotNone(cached_close)
        self.assertEqual(cached_close.index[0], dates[visible_start - 199])
        self.assertEqual(
            int((cached_close.index < cutoff).sum()),
            199,
        )

    def test_daily_graph_window_uses_exactly_365_days(self):
        dates = pd.to_datetime(
            ["2024-12-30", "2024-12-31", "2025-01-01", "2025-12-31"]
        )
        history = pd.DataFrame(
            {"Close": [9.0, 10.0, 11.0, 12.0]},
            index=dates,
        )

        with TemporaryDirectory() as cache_dir:
            history.to_pickle(Path(cache_dir) / "TEST__1y.pkl")
            cached_close = load_cached_daily_close("TEST", cache_dir)

        self.assertIsNotNone(cached_close)
        self.assertEqual(cached_close.index.tolist(), dates[1:].tolist())

    def test_graph_axis_uses_quarter_dollar_ticks_and_month_labels(self):
        ticks = quarter_dollar_price_ticks([10.11, 10.61])
        dates = pd.to_datetime(
            ["2025-01-02", "2025-01-03", "2025-02-03", "2025-04-01"]
        )

        self.assertEqual(ticks, [10.0, 10.25, 10.5, 10.75])
        self.assertEqual(
            monthly_price_markers(list(dates)),
            [(0, "Jan"), (2, "Feb"), (3, "Apr")],
        )

    def test_graph_axis_handles_constant_prices(self):
        self.assertEqual(
            quarter_dollar_price_ticks([10.0, 10.0]),
            [9.75, 10.0, 10.25],
        )

    def test_200_day_sma_points_use_trailing_200_closes(self):
        sma_points = simple_moving_average_points(
            [float(price) for price in range(1, 202)]
        )

        self.assertEqual(sma_points, [(199, 100.5), (200, 101.5)])

    def test_200_day_sma_is_empty_when_history_is_too_short(self):
        self.assertEqual(simple_moving_average_points([10.0] * 199), [])

    def test_month_labels_use_non_overlapping_slots_with_left_edge_padding(self):
        dates = pd.to_datetime(
            ["2025-11-03", "2025-11-04", "2025-12-01", "2026-01-02"]
        )

        slots = monthly_price_label_slots(list(dates), left=20, width=120)

        self.assertEqual([slot[0] for slot in slots], ["Nov", "Dec"])
        self.assertGreaterEqual(slots[0][1], 20)
        self.assertLessEqual(slots[-1][1] + slots[-1][2], 140)
        first_month_marker_x = 20
        second_month_marker_x = 20 + 120 * 2 / 3
        self.assertAlmostEqual(
            slots[0][1] + slots[0][2] / 2,
            (first_month_marker_x + second_month_marker_x) / 2,
        )
        for previous, current in zip(slots, slots[1:]):
            self.assertLessEqual(previous[1] + previous[2], current[1])

    def test_month_labels_at_left_edge_do_not_overlap(self):
        dates = pd.to_datetime(
            ["2025-01-31", "2025-02-03", "2025-03-03", "2025-04-01"]
        )

        slots = monthly_price_label_slots(
            list(dates),
            left=20,
            width=100,
            label_width=24,
        )

        self.assertEqual([slot[0] for slot in slots], ["Jan", "Feb", "Mar"])
        self.assertAlmostEqual(slots[0][1] + slots[0][2] / 2, 20 + 100 / 6)
        for previous, current in zip(slots, slots[1:]):
            self.assertLessEqual(previous[1] + previous[2], current[1])

    def test_repeated_boundary_month_is_shown_once_at_right_edge(self):
        dates = pd.to_datetime(
            [
                "2024-10-31",
                "2024-11-01",
                "2024-12-02",
                "2025-01-02",
                "2025-02-03",
                "2025-03-03",
                "2025-04-01",
                "2025-05-01",
                "2025-06-02",
                "2025-07-01",
                "2025-08-01",
                "2025-09-02",
                "2025-10-31",
            ]
        )

        slots = monthly_price_label_slots(
            list(dates),
            left=20,
            width=120,
            label_width=12,
        )
        oct_labels = [slot for slot in slots if slot[0] == "Oct"]

        self.assertEqual(len(oct_labels), 1)
        self.assertAlmostEqual(
            oct_labels[0][1] + oct_labels[0][2],
            20 + 120,
        )
        for previous, current in zip(slots, slots[1:]):
            self.assertLessEqual(previous[1] + previous[2], current[1])

    def test_symbol_navigation_stops_at_first_and_last_results(self):
        self.assertEqual(adjacent_symbol_index(1, 1, 3), 2)
        self.assertEqual(adjacent_symbol_index(1, -1, 3), 0)
        self.assertIsNone(adjacent_symbol_index(2, 1, 3))
        self.assertIsNone(adjacent_symbol_index(0, -1, 3))

    def test_25_result_rows_fit_from_header_to_toolbar(self):
        window_height = 1080
        row_count = 25
        row_height = gui_result_row_height(window_height, row_count)
        total_height = row_count * row_height + (row_count - 1) * 2 + 2 * 2

        self.assertAlmostEqual(total_height, window_height * 23 / 25)


if __name__ == "__main__":
    unittest.main()
