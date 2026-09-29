import copy
import json
import unittest
import runpy
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

import data_collector
import stop_loss
from backfill_fees import fill_missing_fees
from data_integrity import latest_closing_quotes, require_live_date
from reconcile_ledger import reconcile
from signal_tracker import find_forward_prices
from account_context import CALIBRATION_META


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.day = "2026-09-29"
        self.h = {"code": "sh600000", "name": "fixture", "shares": 100,
                  "cost": 10, "cost_price": 10, "buy_date": "2026-09-28"}
        self.pool = {"date": self.day, "status": "ok", "stocks": []}

    def run_stops(self, prices, pool=None, holding=None):
        return stop_loss.run_stop_loss([holding or self.h], prices, None, None, None,
                                      self.day, l2_dt_pool=pool)

    def test_missing_invalid_prices_do_not_sell(self):
        for price in (None, 0, -1, float("nan"), float("inf")):
            sells, deferred = self.run_stops({"sh600000": price}, self.pool)
            self.assertEqual(sells, [])
            self.assertEqual(deferred[0]["reason"], "missing_quote")

    def test_missing_failed_stale_and_legacy_pool_do_not_sell(self):
        for pool in (None, {}, {**self.pool, "status": "error"},
                     {**self.pool, "date": "2026-09-28"}, {"date": self.day, "stocks": []}):
            sells, deferred = self.run_stops({"sh600000": 9.5}, pool)
            self.assertFalse(sells)
            self.assertEqual(deferred[0]["reason"], "market_data_unavailable")

    def test_verified_empty_pool_can_sell(self):
        sells, _ = self.run_stops({"sh600000": 9.5}, self.pool)
        self.assertEqual(sells[0]["amount"], 950)

    def test_cannot_sell_today_or_future_purchase(self):
        for day in (self.day, "2026-09-30"):
            self.assertEqual(self.run_stops({"sh600000": 10}, self.pool,
                                           {**self.h, "buy_date": day})[0], [])

    def test_daily_stop_uses_previous_close(self):
        self.assertFalse(stop_loss.check_hard_stop({**self.h, "prev_close": 9.4}, 9.4)["triggered"])
        self.assertTrue(stop_loss.check_hard_stop({**self.h, "prev_close": 12}, 11)["triggered"])

    def test_time_stop_counts_trading_days(self):
        h = {**self.h, "buy_date": "2026-09-18"}
        dates = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
        self.assertFalse(stop_loss.check_time_stop(h, 10, "2026-09-21", dates)["triggered"])
        self.assertTrue(stop_loss.check_time_stop(h, 10, "2026-09-23", dates)["triggered"])
        self.assertFalse(stop_loss.check_time_stop(h, 10, self.day, None)["triggered"])

    def test_pending_exit_survives_recovered_price(self):
        h = {**self.h, "pending_exit": {"reasons": ["earlier hard stop"], "half": False}}
        with patch.object(stop_loss, "TIME_STOP_MODE", "D3D5"):
            sells, _ = self.run_stops({"sh600000": 12}, self.pool, h)
        self.assertEqual(sells[0]["shares"], 100)

    def test_latest_quote_per_code_and_cutoff(self):
        def entry(day, code, price, **extra):
            return {"date": day, "session": "收盘简报", "holdings_snapshot": [
                {"code": code, "market_price": price, **extra}]}
        pf = {"daily_log": [entry("2026-08-10", "A", 1), entry("2026-09-23", "A", 2),
                            entry("2026-09-24", "B", 3), entry("2026-09-30", "A", 4)]}
        quotes = latest_closing_quotes(pf, self.day)
        self.assertEqual(quotes["A"]["price"], 2)
        self.assertEqual(quotes["B"]["date"], "2026-09-24")
        self.assertEqual(latest_closing_quotes(pf, "2026-08-11")["A"]["price"], 1)

    def test_stale_quote_adapter(self):
        fields = ["0"] * 50
        fields[3], fields[4], fields[6] = "10", "9", "1000"
        opener = Mock()
        with patch.object(stop_loss.urllib.request, "build_opener", return_value=opener):
            for stamp, expected in (("20260928150000", {}), ("20260929150000", {"sh600000": 10})):
                fields[30] = stamp
                opener.open.return_value.read.return_value = ('v_sh600000="' + '~'.join(fields) + '";').encode("gbk")
                self.assertEqual(stop_loss.fetch_tencent_prices(["sh600000"], self.day), expected)

    def test_pool_failure_is_not_empty_success(self):
        response = Mock()
        with patch.object(data_collector, "em_get", return_value=response):
            response.json.return_value = {"data": {"pool": []}}
            self.assertEqual(data_collector.fetch_dt_pool(self.day), [])
            response.json.return_value = {"data": None}
            self.assertIsNone(data_collector.fetch_dt_pool(self.day))

    def test_historical_collection_fails_before_network_or_write(self):
        with patch.object(data_collector, "fetch_zt_pool") as fetch:
            with self.assertRaises(ValueError):
                data_collector.collect("2000-01-01", force=True)
            fetch.assert_not_called()

    def test_missing_signal_date_not_shifted_to_recent_history(self):
        self.assertEqual(find_forward_prices([{"date": "2026-09-28", "close": 10},
                                              {"date": self.day, "close": 11}],
                                             "2026-07-01", [1]), {1: None})

    def test_fee_backfill_debits_only_new_fees(self):
        pf = {"account": {"initial_capital": 1000, "cash": 900, "total_value": 1100},
              "trades": [{"type": "buy", "amount": 100, "fee": 5.01},
                         {"type": "sell", "amount": 100}]}
        fixed, count, additional = fill_missing_fees(pf)
        self.assertEqual(additional, 5.05)
        self.assertEqual(fixed["account"]["cash"], 894.95)
        self.assertEqual(fixed["account"]["total_value"] - fixed["account"]["cash"], 200)
        self.assertEqual(fill_missing_fees(fixed), (fixed, 0, 0))

    def test_actual_ledger_recovery_is_balanced_and_idempotent(self):
        folder = Path(__file__).resolve().parent.parent / "virtual-portfolio"
        pf = json.loads((folder / "portfolio.json").read_text(encoding="utf-8"))
        sources = [(p.name, json.loads(p.read_text(encoding="utf-8")))
                   for p in folder.glob("portfolio*") if p.is_file()]
        original = copy.deepcopy(pf)
        fixed, report = reconcile(pf, sources)
        self.assertEqual(pf, original)
        self.assertTrue(all(report["checks"].values()))
        again, report2 = reconcile(fixed, sources)
        self.assertEqual(fixed, again)
        self.assertEqual(report2["changes"], [])
        self.assertEqual(len(fixed["trades"]), 98)

    def test_closing_flow_commits_only_real_quotes(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import common_paths
        day = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        source = Path(__file__).parent / "data/cache/2026-09-29"
        for has_quote in (False, True):
            with self.subTest(has_quote=has_quote), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                for name in ("l1_index", "l2_zt_pool", "l2_zb_pool", "l2_rotation", "l3_stocks", "l2_dt_pool"):
                    payload = json.loads((source / (name + ".json")).read_text(encoding="utf-8"))
                    payload["date"] = day
                    if name == "l3_stocks":
                        payload["stocks"] = []
                    if name == "l2_dt_pool":
                        payload.update(status="ok", stocks=[], total=0)
                    (folder / (name + ".json")).write_text(json.dumps(payload), encoding="utf-8")
                h = {**self.h, "buy_date": yesterday, "hybk": "fixture"}
                pf = {"account": {"initial_capital": 1000000, "cash": 999000,
                                  "total_value": 1000000, "pnl": 0, "pnl_pct": 0},
                      "account_meta": {**CALIBRATION_META, "preservation_probe": "keep"},
                      "holdings": [h], "trades": [], "daily_log": [
                          {"date": yesterday, "session": "收盘简报", "holdings_snapshot": [
                              {**h, "market_price": 9, "price_date": yesterday}]}]}
                path = folder / "portfolio.json"
                path.write_text(json.dumps(pf), encoding="utf-8")
                quotes = {h["code"]: {"price": 9.5, "date": day, "prev_close": 9,
                                      "source": "test_quote"}} if has_quote else {}
                with patch.object(sys, "argv", ["closing_briefing.py", "--pf", str(path)]), \
                        patch.object(common_paths, "CACHE_DIR", folder), \
                        patch.object(common_paths, "cache_dir", return_value=folder), \
                        patch.object(data_collector, "fetch_trading_dates", return_value=[yesterday, day]), \
                        patch.object(data_collector, "is_trading_day", return_value=True), \
                        patch.object(data_collector, "find_prev_cache_date", return_value=[]), \
                        patch.object(data_collector, "fetch_qt_batch", return_value={}), \
                        patch.object(stop_loss, "fetch_tencent_prices", return_value=quotes), \
                        patch.object(stop_loss, "fetch_tencent_highs", return_value=(None, None)), \
                        patch("builtins.print") as output:
                    before_dry_run = path.read_bytes()
                    with patch.object(sys, "argv", ["closing_briefing.py", "--pf", str(path), "--dry-run"]):
                        with self.assertRaises(SystemExit) as exited:
                            runpy.run_path(str(Path(__file__).parent / "closing_briefing.py"), run_name="__main__")
                        self.assertEqual(exited.exception.code, 0)
                    self.assertEqual(path.read_bytes(), before_dry_run)
                    runpy.run_path(str(Path(__file__).parent / "closing_briefing.py"), run_name="__main__")
                    self.assertTrue(any(CALIBRATION_META["note"] in str(call) for call in output.call_args_list))
                result = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result["account_meta"], pf["account_meta"])
                self.assertEqual(result["daily_log"][-1]["account_meta"], pf["account_meta"])
                self.assertIn(CALIBRATION_META["note"], result["daily_log"][-1]["observations"])
                if has_quote:
                    self.assertEqual(result["holdings"], [])
                    self.assertEqual(result["trades"][0]["price"], 9.5)
                    self.assertEqual(result["account"]["cash"], 999944.52)
                else:
                    self.assertEqual(result["account"]["cash"], 999000)
                    self.assertEqual(result["trades"], [])
                    self.assertTrue(result["holdings"][0]["pending_exit"])
                    snapshot = result["daily_log"][-1]["holdings_snapshot"][0]
                    self.assertEqual(snapshot["market_price"], 9)
                    self.assertEqual(snapshot["price_date"], yesterday)
                    self.assertTrue(snapshot["valuation_stale"])


if __name__ == "__main__":
    unittest.main()
