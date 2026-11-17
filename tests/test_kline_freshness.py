from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock, patch

import app
from data import kline_fetcher as fetcher


class KlineFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(fetcher, "_cache", {}))
        self.clock = self.enterContext(patch.object(fetcher.time, "monotonic", return_value=1000))
        self.enterContext(patch.object(fetcher, "flow_session_status", return_value={
            "session_state": "awaiting_today", "market_date": "2026-11-17"}))
        self.history = [fetcher.Kline(str(date(2026, 9, 18) + timedelta(days=i)),
                                     10, 10, 10.1, 9.9, 1000) for i in range(60)]
        self.current = self.history + [fetcher.Kline("2026-11-17", 10, 10.2, 10.3, 10, 200)]
        self.primary = self.enterContext(patch.object(fetcher, "_fetch_kline_tencent", return_value=self.current))
        self.backup = self.enterContext(patch.object(fetcher, "_fetch_kline_eastmoney", return_value=self.history))
        self.sina = self.enterContext(patch.object(fetcher, "_fetch_kline_sina", return_value=self.history))

    def fetch(self):
        return fetcher.fetch_kline("999785", enrich=False)

    def test_current_eastmoney_precedes_sina_history_when_primary_fails(self):
        self.primary.return_value = []
        self.backup.return_value = self.current
        rows = self.fetch()
        self.assertEqual(rows[-1].date, "2026-11-17")
        self.assertEqual(rows[-1].source, "东方财富")
        self.sina.assert_not_called()

    def test_older_sources_cannot_replace_success_or_reset_its_timestamp(self):
        first = self.fetch()
        self.clock.return_value = 1016
        self.primary.return_value = []
        second = self.fetch()
        self.assertEqual(second[-1].date, "2026-11-17")
        self.assertTrue(second[-1].stale)
        self.assertEqual(second[-1].source, "腾讯")
        self.assertEqual(second[-1].fetched_at, first[-1].fetched_at)
        self.assertFalse(first[-1].stale)
        self.assertIn("新浪仅截至2026-11-16", second[-1].refresh_error)
        self.assertEqual(fetcher._cache['kline_999785_250_day_qfq'][1], 1000)
        with patch.object(app, "collect_stages", return_value=({
                "kline": second, "index": [], "fund_flow": [], "kline_extras": {}}, {}, {})):
            analysis = app.handle_analyze({"symbol": ["999785"]})
        self.assertEqual(analysis["kline_status"]["last_bar"], analysis["signal"]["input_snapshot"]["kline_through"])
        self.assertTrue(analysis["signal"]["input_snapshot"]["kline_stale"])
        self.assertIn("kline", analysis["degraded"])
        self.clock.return_value = 1032
        self.backup.return_value = self.current
        recovered = self.fetch()
        self.assertFalse(recovered[-1].stale)
        self.assertEqual(recovered[-1].source, "东方财富")

    def test_expired_current_snapshot_reports_failure_instead_of_date_regression(self):
        self.fetch()
        self.clock.return_value = 1000 + fetcher.KLINE_SNAPSHOT_TTL_SECONDS + 1
        self.primary.return_value = []
        with self.assertRaisesRegex(OSError, "早于已取得的2026-11-17"):
            self.fetch()

    def test_primary_date_regression_also_tries_current_backup(self):
        self.fetch()
        self.clock.return_value = 1016
        self.primary.return_value = self.history
        self.backup.return_value = self.current
        result = self.fetch()
        self.assertEqual(result[-1].date, "2026-11-17")
        self.assertEqual(result[-1].source, "东方财富")
        self.assertFalse(result[-1].stale)

    def test_zero_volume_today_is_not_a_bar_until_actual_trading(self):
        self.primary.return_value = self.history + [replace(self.current[-1], volume=0)]
        self.assertEqual(self.fetch()[-1].date, "2026-11-16")
        self.clock.return_value = 1016
        self.primary.return_value = []
        self.assertEqual(self.fetch()[-1].date, "2026-11-16")
        self.clock.return_value = 1032
        self.primary.return_value = self.current
        self.assertEqual(self.fetch()[-1].date, "2026-11-17")

    def test_invalid_today_bar_is_filtered_before_comparing_dates(self):
        self.fetch()
        self.clock.return_value = 1016
        self.primary.return_value = self.history + [replace(self.current[-1], low=100)]
        result = self.fetch()
        self.assertEqual(result[-1].date, "2026-11-17")
        self.assertEqual(result[-1].low, 10)
        self.assertTrue(result[-1].stale)


class SinaVolumeUnitsTests(unittest.TestCase):
    def test_stock_and_etf_volume_both_convert_shares_to_lots(self):
        payload = [{"day": "2026-11-16", "open": "10", "close": "10",
                    "high": "10.1", "low": "9.9", "volume": "100000"}]
        with patch.object(fetcher, "_http_get", return_value=Mock(status_code=200, json=lambda: payload)):
            for code in ("513100", "600000"):
                self.assertEqual(fetcher._fetch_kline_sina(code, 10, "day")[-1].volume, 1000)
