from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import probe_rqdata_iopv as probe


DAY = date(2026, 1, 5)
TODAY = date(2026, 1, 6)


def stamp(value="2026-01-06T10:31:00"):
    return datetime.fromisoformat(value).replace(tzinfo=probe.CHINA_TZ)


def tick(**changes):
    return SimpleNamespace(**{
        "order_book_id": probe.SYMBOLS[0], "datetime": stamp(), "last": 2.2,
        "iopv": 2.01, "prev_iopv": 2, "trading_phase_code": "T", **changes,
    })


def history(symbol=probe.SYMBOLS[0], *, flat=False, day=DAY):
    rows = []
    for start, end in probe.trading_intervals(day, []):
        current = start
        while current <= end:
            rows.append({"order_book_id": symbol, "datetime": current,
                         "last": 2.2, "iopv": 2 if flat or current.hour >= 10 else 1.99})
            current += timedelta(minutes=5)
    return rows


def calendar():
    return {day: {"source": "deterministic fixture", "symbols": {symbol: [] for symbol in probe.SYMBOLS}}
            for day in (DAY, TODAY)}


def client_fixture(*, flat=False):
    client = Mock()
    client.get_trading_dates.return_value = [DAY, TODAY]
    client.current_snapshot.return_value = [tick(order_book_id=symbol) for symbol in probe.SYMBOLS]

    def prices(symbol, **kwargs):
        table = Mock()
        table.columns = ["order_book_id", "datetime", "last", "iopv"]
        table.to_dict.return_value = history(symbol, flat=flat, day=kwargs["start_date"])
        frame = Mock()
        frame.reset_index.return_value = table
        return frame

    client.get_price.side_effect = prices
    return client


class SnapshotTests(unittest.TestCase):
    def report(self, value, now=None, halts=(), **kwargs):
        now = now or stamp()
        return probe.snapshot_report(value, now, now, True, halts, 900, halt_verified=True, **kwargs)

    def test_threshold_future_and_session_boundaries(self):
        self.assertTrue(self.report(tick(datetime=stamp() - timedelta(seconds=900)))["usable_sample"])
        for value in (stamp() - timedelta(seconds=901), stamp() + timedelta(seconds=1),
                      stamp("2026-01-05T10:31:00"), stamp("2026-01-06T09:29:59")):
            with self.subTest(value=value):
                self.assertFalse(self.report(tick(datetime=value))["usable_sample"])
        self.assertFalse(self.report(tick(datetime=stamp("2026-01-06T11:30:00")),
                                     stamp("2026-01-06T11:31:00"))["usable_sample"])

    def test_invalid_values_do_not_count_as_available(self):
        for value in (None, 0, -1, float("nan"), float("inf"), True, "-"):
            with self.subTest(value=value):
                result = self.report(tick(iopv=value))
                self.assertFalse(result["value_present"])
                self.assertFalse(result["usable_sample"])
                json.dumps(result, allow_nan=False)

    def test_halted_and_pre_halt_ticks_are_not_current_values(self):
        halts = [(stamp("2026-01-06T10:20:00"), stamp("2026-01-06T10:30:00"))]
        self.assertFalse(self.report(tick(datetime=stamp("2026-01-06T10:19:00")), halts=halts)["usable_sample"])
        self.assertFalse(self.report(tick(datetime=stamp("2026-01-06T10:25:00")),
                                     stamp("2026-01-06T10:25:00"), halts)["usable_sample"])
        self.assertTrue(self.report(tick(), halts=halts)["usable_sample"])
        for phase in ("H", "P"):
            self.assertFalse(self.report(tick(trading_phase_code=phase))["usable_sample"])

    def test_unverified_suspension_and_invalid_time_cannot_be_usable(self):
        result = probe.snapshot_report(tick(), stamp(), stamp(), True, [], 900)
        self.assertFalse(result["usable_sample"])
        for value in (None, "NaT", "bad-time"):
            self.assertFalse(self.report(tick(datetime=value))["time_within_threshold"])

    def test_utc_and_naive_sdk_times_use_beijing_time(self):
        for value in ("2026-01-06T02:31:00+00:00", "2026-01-06 10:31:00"):
            self.assertTrue(self.report(tick(datetime=value))["usable_sample"])


class HistoryTests(unittest.TestCase):
    def test_complete_changing_and_flat_days_are_distinguished(self):
        result = probe.history_report(history(), probe.SYMBOLS[0], DAY, [], 900)
        self.assertTrue(result["full_day_covered"])
        self.assertTrue(result["intraday_changed"])
        self.assertEqual(result["change_count"], 1)
        self.assertEqual(result["distinct_iopv_count"], 2)
        flat = probe.history_report(history(flat=True), probe.SYMBOLS[0], DAY, [], 900)
        self.assertFalse(flat["intraday_changed"])

    def test_sparse_rows_and_time_regression_do_not_pass_coverage(self):
        rows = history()
        sparse = probe.history_report([rows[0], rows[-1]], probe.SYMBOLS[0], DAY, [], 900)
        self.assertFalse(sparse["full_day_covered"])
        rows[1], rows[2] = rows[2], rows[1]
        result = probe.history_report(rows, probe.SYMBOLS[0], DAY, [], 900)
        self.assertEqual(result["time_regressions"], 1)
        self.assertFalse(result["full_day_covered"])

    def test_halt_rows_are_excluded_including_halt_start(self):
        halts = [(stamp("2026-01-05T09:30:00"), stamp("2026-01-05T10:30:00"))]
        result = probe.history_report(history(), probe.SYMBOLS[0], DAY, halts, 900)
        self.assertEqual(result["excluded_count"], 12)
        self.assertEqual(result["first_tick_time"], "2026-01-05T10:30:00+08:00")
        self.assertTrue(result["full_day_covered"])
        self.assertFalse(result["intraday_changed"])

    def test_wrong_symbol_and_date_are_errors(self):
        for change in ({"order_book_id": "600000.XSHG"}, {"datetime": stamp()}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                probe.history_report([{**history()[0], **change}], probe.SYMBOLS[0], DAY, [], 900)


class ProbeTests(unittest.TestCase):
    def test_pass_requires_all_symbols_and_does_not_complete_item_19(self):
        client = client_fixture()
        report = probe.run_probe(client, halt_calendar=calendar(), clock=stamp)
        self.assertEqual(report["status"], "probe_pass")
        self.assertEqual(len(report["results"]), 5)
        self.assertFalse(report["item_19_complete"])
        self.assertTrue(all(row["prev_iopv_matches_previous_day_last"] for row in report["results"]))
        for call in client.get_price.call_args_list:
            self.assertEqual(call.kwargs["fields"], ["last", "iopv"])
            self.assertTrue(call.kwargs["skip_suspended"])
            self.assertEqual(call.kwargs["start_date"], DAY)
        self.assertNotIn('"iopv_time"', json.dumps(report, allow_nan=False))

    def test_missing_symbol_and_flat_day_fail(self):
        client = client_fixture()
        client.current_snapshot.return_value.pop()
        self.assertEqual(probe.run_probe(client, halt_calendar=calendar(), clock=stamp)["status"], "probe_fail")
        self.assertEqual(probe.run_probe(client_fixture(flat=True), halt_calendar=calendar(), clock=stamp)["status"], "probe_fail")

    def test_unknown_halts_and_previous_value_mismatch_require_review(self):
        report = probe.run_probe(client_fixture(), clock=stamp)
        self.assertEqual(report["status"], "needs_review")
        self.assertFalse(report["results"][0]["snapshot"]["usable_sample"])
        client = client_fixture()
        client.current_snapshot.return_value[0].prev_iopv = 1.5
        report = probe.run_probe(client, halt_calendar=calendar(), clock=stamp)
        self.assertEqual(report["status"], "needs_review")

    def test_lunch_and_holiday_are_not_live_acceptance(self):
        for now in (stamp("2026-01-06T12:00:00"), stamp("2026-01-07T10:31:00")):
            report = probe.run_probe(client_fixture(), halt_calendar=calendar(), clock=lambda: now)
            self.assertEqual(report["status"], "not_ready")

    def test_incomplete_history_day_and_wrong_identity_are_rejected(self):
        with self.assertRaises(ValueError):
            probe.run_probe(client_fixture(), history_day=TODAY, clock=stamp)
        client = client_fixture()
        client.current_snapshot.return_value[0].order_book_id = "600000.XSHG"
        with self.assertRaises(ValueError):
            probe.run_probe(client, clock=stamp)

    def test_halt_calendar_requires_explicit_symbols_and_source(self):
        payload = {DAY.isoformat(): {"source": "fixture", "symbols": {symbol: [] for symbol in probe.SYMBOLS}}}
        payload[DAY.isoformat()]["symbols"][probe.SYMBOLS[0]] = [["09:30", "10:30"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "halts.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(probe.load_halt_calendar(path)[DAY]["symbols"][probe.SYMBOLS[0]][0][1].hour, 10)
            del payload[DAY.isoformat()]["symbols"][probe.SYMBOLS[1]]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                probe.load_halt_calendar(path)

    def test_missing_sdk_reports_not_ready(self):
        output = io.StringIO()
        with patch.object(probe, "environment_status", return_value={"sdk_installed": False}), redirect_stdout(output):
            code = probe.main([])
        report = json.loads(output.getvalue())
        self.assertEqual((code, report["status"]), (2, "not_ready"))
        self.assertFalse(report["item_19_complete"])

    def test_sdk_error_is_explicit_without_leaking_credentials(self):
        client = Mock()
        client.init.side_effect = RuntimeError("license=fixture-secret-not-for-output")
        output = io.StringIO()
        with patch.object(probe, "environment_status", return_value={"sdk_installed": True}), \
                patch.dict("sys.modules", {"rqdatac": client}), redirect_stdout(output):
            code = probe.main([])
        report = json.loads(output.getvalue())
        self.assertEqual((code, report["status"], report["stage"]), (1, "error", "rqdatac_init"))
        self.assertNotIn("fixture-secret-not-for-output", output.getvalue())


if __name__ == "__main__":
    unittest.main()
