import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from account_context import CALIBRATION_META
import email_briefing


class AccountContextTests(unittest.TestCase):
    def test_notice_in_closing_and_morning_mail_both_formats(self):
        portfolio = {"account_meta": dict(CALIBRATION_META), "daily_log": [{"date": "2026-09-29"}]}
        observation = {"account_meta": dict(CALIBRATION_META), "date": "2026-09-30"}
        for render, payload in ((email_briefing.build_briefing_html, portfolio),
                                (email_briefing.build_plain_briefing, portfolio),
                                (email_briefing.build_morning_html, observation),
                                (email_briefing.build_morning_plain, observation)):
            with self.subTest(renderer=render.__name__):
                text = render(payload)
                self.assertEqual(text.count(CALIBRATION_META["note"]), 1)
                self.assertLess(text.index(CALIBRATION_META["note"]), text.index("不构成投资建议"))

    def test_html_escapes_account_note(self):
        payload = {"account_meta": {**CALIBRATION_META, "note": "<script>bad</script>"},
                   "daily_log": [{"date": "2026-09-29"}]}
        text = email_briefing.build_briefing_html(payload)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", text)
        self.assertNotIn("<script>bad</script>", text)

    def test_existing_morning_observation_gets_persistent_account_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pf = root / "portfolio.json"
            pf.write_text(json.dumps({"account_meta": CALIBRATION_META}), encoding="utf-8")
            observation = root / f"morning_briefing_{date.today().isoformat()}.json"
            observation.write_text('{"signals": []}', encoding="utf-8")
            before = observation.read_bytes()
            with patch.object(email_briefing, "PORTFOLIO", pf), patch.object(email_briefing, "CACHE_DIR", root):
                source, payload = email_briefing.resolve_source("morning")
            self.assertEqual(source, "morning")
            self.assertEqual(payload["account_meta"], CALIBRATION_META)
            self.assertEqual(observation.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
