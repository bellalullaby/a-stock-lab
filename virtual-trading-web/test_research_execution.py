import unittest
from datetime import date, timedelta

from research_execution import exit_order, make_row, strict_signal, portfolio_d1


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.days = []
        day = date(2026, 7, 1)
        while len(self.days) < 45:
            if day.weekday() < 5:
                self.days.append(day.isoformat())
            day += timedelta(days=1)
        self.klines = [[d, 10, 10, 10.5, 9.5, 10000] for d in self.days]
        self.signal = {"code": "sh600000", "name": "fixture", "out": "强候选",
                       "l1_gate": "降低权重", "l1_state": "震荡市",
                       "signal_date": self.days[25], "price": 10}

    def test_gate_and_exact_label(self):
        self.assertTrue(strict_signal(self.signal))
        for override in ({"l1_gate": "暂停交易"}, {"l1_gate": ""}, {"out": "观察"},
                         {"out": "强候选(门控禁止)"}):
            self.assertFalse(strict_signal({**self.signal, **override}))

    def test_d1_respects_purchase_t_plus_one(self):
        row, reason = make_row(self.signal, self.klines, self.days)
        self.assertIsNone(reason)
        self.assertEqual(row["buy_date"], self.days[26])
        self.assertEqual(row["D1"]["date"], self.days[27])
        self.assertLess(row["D1"]["net_pct"], 0)  # unchanged price still incurs fees

    def test_time_stop_close_signal_executes_next_open(self):
        row, _ = make_row(self.signal, self.klines, self.days)
        # Buy day 26, D+3 decision day 29, execute day 30 open.
        self.assertEqual(row["D3D5"]["date"], self.days[30])
        self.assertEqual(row["D3D5"]["session"], "open")

    def test_opening_limit_up_not_rescued_by_later_break(self):
        self.klines[26][1] = 11
        self.klines[26][2] = 10  # later break does not backfill the opening order
        row, reason = make_row(self.signal, self.klines, self.days)
        self.assertIsNone(row)
        self.assertEqual(reason, "opening_limit_up_no_fill_assumed")

    def test_missing_market_session_does_not_shift_horizon(self):
        del self.klines[26]
        row, reason = make_row(self.signal, self.klines, self.days)
        self.assertIsNone(row)
        self.assertEqual(reason, "missing_market_session")

    def test_limit_down_delays_exit(self):
        self.klines[27][2] = 9
        row, _ = make_row(self.signal, self.klines, self.days)
        self.assertEqual(row["D1"]["date"], self.days[28])
        self.assertTrue(row["D1"]["delayed"])

    def test_price_basis_mismatch_rejected(self):
        row, reason = make_row({**self.signal, "price": 20}, self.klines, self.days)
        self.assertIsNone(row)
        self.assertEqual(reason, "signal_price_basis_mismatch")

    def test_portfolio_fees_and_slippage(self):
        row, _ = make_row(self.signal, self.klines, self.days)
        row["rotation"] = "主线延续，正常轮动"
        cache = {self.signal["code"]: self.klines}
        base = portfolio_d1([row], cache, self.days)
        slipped = portfolio_d1([row], cache, self.days, 10)
        self.assertEqual(len(base["trades"]), 2)
        self.assertEqual(base["open_positions"], 0)
        self.assertLess(base["return_pct"], 0)
        self.assertLess(slipped["return_pct"], base["return_pct"])


if __name__ == "__main__":
    unittest.main()
