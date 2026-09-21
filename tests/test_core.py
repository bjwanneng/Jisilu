import io
import json
import sqlite3
import unittest
from unittest import mock

import pandas as pd

import daily_sync
from metrics import lookback_change_pct


class MetricTests(unittest.TestCase):
    def test_lookback_is_measured_from_latest_observation(self):
        values = list(range(100, 111))
        self.assertAlmostEqual(lookback_change_pct(values, 5), (110 / 105 - 1) * 100)

    def test_zero_base_is_rejected(self):
        self.assertTrue(pd.isna(lookback_change_pct([0, 1], 1)))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(daily_sync.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_sync_daily_preserves_each_rows_source_date(self):
        cross = pd.DataFrame([
            {"fund_id": "A", "last_dt": "2026-09-18", "in_pool": 1, "price": 1,
             "increase_rt": 1, "fund_nav": 1, "nav_discount_rt": 0, "volume": 10,
             "amount": 20, "amount_incr": 1, "unit_total": 2, "index_increase_rt": 1},
            {"fund_id": "B", "last_dt": "2026-09-17", "in_pool": 0, "price": 2,
             "increase_rt": 0, "fund_nav": 2, "nav_discount_rt": 0, "volume": 5,
             "amount": 10, "amount_incr": 0, "unit_total": 1, "index_increase_rt": 0},
        ])
        daily_sync.sync_daily(self.conn, cross, None)
        dates = dict(self.conn.execute("SELECT fund_id, trade_dt FROM market_daily"))
        self.assertEqual(dates, {"A": "2026-09-18", "B": "2026-09-17"})

    def test_backfill_matches_current_market_schema(self):
        self.conn.execute(
            "INSERT INTO etf_info(fund_id, in_pool) VALUES ('A', 1)"
        )
        record = {"fund_id": "A", "history": [{
            "hist_dt": "2026-09-18", "trade_price": "1.23", "increase_rt": "2.5",
            "fund_nav": "1.20", "discount_rt": "2.5%", "volume": "100",
            "amount": "200", "amount_incr": "3", "idx_incr_rt": "1.1"
        }]}
        payload = io.StringIO(json.dumps(record) + "\n")
        with mock.patch("daily_sync.os.path.exists", return_value=True), \
             mock.patch("builtins.open", return_value=payload):
            self.assertEqual(daily_sync.backfill_history(self.conn), 1)
        row = self.conn.execute(
            "SELECT price, increase_rt FROM market_daily WHERE fund_id='A'"
        ).fetchone()
        self.assertEqual(row, (1.23, 2.5))

    def test_average_volume_uses_exact_trading_dates(self):
        rows = [("A", f"2026-09-{day:02d}", None, float(day), None, None, None, None)
                for day in (1, 2, 8, 9)]
        self.conn.executemany("INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?)", rows)
        result = daily_sync.avg_volume_from_db(self.conn, days=2)
        self.assertEqual(result.iloc[0]["avg_vol"], 8.5)


if __name__ == "__main__":
    unittest.main()
