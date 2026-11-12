"""trader-analyst tests. Fixtures in tests/fixtures/trader-analyst/, read 2026-11-12:

yts-M&M.json, yts-HDFCBANK.json   Yahoo fundamentals-timeseries answers (quarterly, INR). M&M has the
                                  2025-09-30 quarter missing; HDFCBANK stops at 2025-06-30 and, as a
                                  bank, has no gross profit, cost of revenue or operating income.
ann-M&M.json, ann-HDFCBANK.json   NSE corporate announcements, 15 Jul to 12 Nov 2026 (public filings).
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")
NOW = "2026-11-12T09:00"
D = datetime.date
R = "₹"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


class Numbers(unittest.TestCase):
    def setUp(self):
        self.q = facts.parse_yts(raw("yts-M&M.json"))

    def test_the_quarters_and_the_hole(self):
        self.assertEqual(sorted(self.q), [D(2025, 6, 30), D(2025, 12, 31), D(2026, 3, 31), D(2026, 6, 30),
                                          D(2026, 9, 30)])
        self.assertEqual(self.q[D(2026, 9, 30)]["revenue"], 575149800000.0)
        self.assertEqual(self.q[D(2026, 9, 30)]["eps_diluted"], 40.9)

    def test_yoy_is_matched_by_date_not_by_position(self):
        rows, gaps, ttm = facts.analyse(self.q)
        by = {r["period_end"]: r for r in rows}
        # Sep 2026: the year-earlier quarter (2025-09-30) is the hole, so no growth figure,
        # even though another row sits four places down
        self.assertIsNone(by["2026-09-30"]["revenue_yoy"])
        self.assertIsNone(by["2026-09-30"]["yoy_vs"])
        # Jun 2026 against Jun 2025: 5,75,334.4 / 4,54,358.8 million - 1 = +26.6 %
        self.assertEqual((by["2026-06-30"]["revenue_yoy"], by["2026-06-30"]["eps_yoy"]), (26.6, 33.4))
        self.assertEqual(by["2026-06-30"]["yoy_vs"], "2025-06-30")
        self.assertEqual(gaps, ["2025-09-30"])

    def test_ttm_needs_four_quarters_in_a_row(self):
        rows, gaps, ttm = facts.analyse(self.q)
        # Sep 26, Jun 26, Mar 26, Dec 25: four in a row (the hole is older)
        self.assertEqual((ttm["eps_diluted"], ttm["through"]), (172.66, "2026-09-30"))
        self.assertAlmostEqual(ttm["revenue"], 575149800000 + 575334400000 + 548915500000 + 515799500000)
        self.assertAlmostEqual(ttm["net_income"], 45878800000 + 54545400000 + 46675700000 + 46746400000)
        q = dict(self.q)
        del q[D(2026, 3, 31)]
        self.assertIsNone(facts.analyse(q)[2])                    # a hole inside the four: no TTM

    def test_margins(self):
        rows, _, _ = facts.analyse(self.q)
        top = rows[0]
        self.assertEqual((top["gross_margin"], top["operating_margin"], top["net_margin"]), (35.3, 14.6, 8.0))

    def test_a_bank_has_no_gross_or_operating_margin(self):
        q = facts.parse_yts(raw("yts-HDFCBANK.json"))
        rows, gaps, ttm = facts.analyse(q)
        self.assertEqual([r["period_end"] for r in rows], ["2025-06-30", "2025-03-31"])
        self.assertNotIn("gross_profit", rows[0]["fields"])          # absent stays absent
        self.assertIsNone(rows[0]["gross_margin"])
        self.assertIsNone(rows[0]["operating_margin"])
        self.assertEqual(rows[0]["net_margin"], 19.0)
        self.assertIsNone(ttm)
        self.assertNotIn("eps_diluted", rows[1]["fields"])            # Mar 2025 EPS not given

    def test_a_currency_other_than_inr_is_refused(self):
        doc = json.loads(raw("yts-M&M.json"))
        for r in doc["timeseries"]["result"]:
            for v in r.get(r["meta"]["type"][0]) or []:
                if v:
                    v["currencyCode"] = "USD"
        with self.assertRaises(facts.FactsError) as cm:
            facts.parse_yts(json.dumps(doc))
        self.assertIn("not INR", str(cm.exception))
        for bad in ("<html>", '{"timeseries": {"result": null}}', '{"chart": {}}'):
            with self.assertRaises(facts.FactsError):
                facts.parse_yts(bad)

    def test_dates(self):
        self.assertEqual(facts.months_before(D(2026, 9, 30), 12), D(2025, 9, 30))
        self.assertEqual(facts.months_before(D(2026, 3, 31), 3), D(2025, 12, 31))
        self.assertEqual(facts.months_before(D(2024, 5, 31), 3), D(2024, 2, 29))
        for text, want in (("September 30, 2026", D(2026, 9, 30)), ("30 June 2026", D(2026, 6, 30)),
                           ("Jun 30, 2026", D(2026, 6, 30)), ("30th September 2026", D(2026, 9, 30)),
                           ("Sept 30, 2026", D(2026, 9, 30))):
            self.assertEqual(facts.parse_text_date(text), want, text)
        self.assertIsNone(facts.parse_text_date("the end of the quarter"))
        self.assertEqual(facts.crore(575149800000), R + "57,515 cr")
        self.assertEqual(facts.crore(2215198000000), R + "2,21,520 cr")


class Filings(unittest.TestCase):
    def test_the_newest_results_filing(self):
        f = facts.parse_filings(raw("ann-M&M.json"))
        self.assertEqual([x["period_end"] for x in f[:2]], [D(2026, 9, 30), D(2026, 9, 30)])
        top = f[0]
        self.assertEqual(top["desc"], "Outcome of Board Meeting")          # the outcome before the press release
        self.assertTrue(top["link"].endswith("Pooja_05112026125638_Outcome5Nov.pdf"))
        self.assertEqual(top["filed"], datetime.datetime(2026, 11, 5, 12, 57, 20))
        self.assertIn(D(2026, 6, 30), [x["period_end"] for x in f])
        texts = " ".join(x["text"] for x in f)
        self.assertNotIn("Voting Results", texts)                          # results of a vote are not results

    def test_only_financial_results_count(self):
        # SYNTHETIC rows: a dated filing that is not a results filing must not be taken for one.
        rows = [{"desc": "Shareholders meeting", "an_dt": "12-Nov-2026 10:00:00", "attchmntFile": "v.pdf",
                 "attchmntText": "Voting results of the postal ballot for the quarter ended 30 September 2026"},
                {"desc": "Outcome of Board Meeting", "an_dt": "05-Aug-2026 15:00:00", "attchmntFile": "r.pdf",
                 "attchmntText": "Unaudited financial results for the quarter ended 30 June 2026"}]
        f = facts.parse_filings(json.dumps(rows).encode("utf-8"))
        self.assertEqual([x["link"] for x in f], ["r.pdf"])

    def test_a_refusal_is_a_plain_error(self):
        with self.assertRaises(facts.FactsError):
            facts.parse_filings(b"<html>Access Denied</html>")
        self.assertEqual(facts.parse_filings(b"[]"), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_the_reel_mm(self):
        code, text, err = run(["M&M", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("Yahoo, unofficial: check against the filing", text)
        self.assertIn("Basis: not stated by the source (consolidated or standalone)", text)
        self.assertIn("2026-09-30     %s57,515 cr      %s4,588 cr    %s40.90     35.3%%" % (R, R, R), text)
        self.assertIn("Gap: no numbers for the quarter ended 2025-09-30", text)
        self.assertIn("Trailing twelve months through 2026-09-30: revenue %s2,21,520 cr" % R, text)
        self.assertIn("Newest results filing on NSE: quarter ended 2026-09-30", text)
        self.assertNotIn("NOTE:", text)                                   # the numbers are not behind
        self.assertIn("not a recommendation", text)

    def test_numbers_behind_the_filings_are_said_first(self):
        code, text, _ = run(["HDFCBANK", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith("NOTE: NSE has a results filing for the quarter ended 2026-09-30 "
                                            "(filed 2026-10-17) that the numbers below do not include."), lines[1])
        self.assertIn("2025-06-30     %s85,346 cr" % R, text)
        self.assertIn("No trailing twelve months", text)

    def test_no_numbers_is_still_a_report(self):
        empty = os.path.join(self.d, "off")
        os.makedirs(empty)
        shutil.copy(os.path.join(DIR, "ann-M&M.json"), empty)
        with open(os.path.join(empty, "yts-M&M.json"), "w") as fh:
            json.dump({"timeseries": {"result": [], "error": None}}, fh)
        code, text, _ = run(["M&M", "--offline", empty, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("NOTE: NSE has a results filing for the quarter ended 2026-09-30", text)
        self.assertIn("(there are no numbers)", text)
        self.assertIn("No table:", text)
        with open(os.path.join(empty, "yts-M&M.json"), "w") as fh:
            fh.write("<html>Will be right back</html>")
        code, text, _ = run(["M&M", "--offline", empty, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("No table: Yahoo's answer is not the shape this tool knows", text)

    def test_metrics_file(self):
        path = os.path.join(self.d, "metrics.json")
        code, text, _ = run(["M&M", "HDFCBANK", "--offline", DIR, "--now", NOW, "--metrics", path])
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["as_of"], "2026-11-12")
        mm, hdfc = doc["companies"][1], doc["companies"][0]
        self.assertEqual((mm["symbol"], mm["period_end"], mm["eps_ttm"], mm["gaps"]), ("M&M", "2026-09-30", 172.66,
                                                                                       ["2025-09-30"]))
        self.assertEqual(mm["source"], "Yahoo fundamentals time series (unofficial)")
        self.assertEqual(mm["fetched_at"], "2026-11-12T03:30:00Z")
        self.assertFalse(mm["behind_filings"])
        self.assertTrue(hdfc["behind_filings"])
        self.assertEqual(hdfc["newest_filing"]["period_end"], "2026-09-30")
        self.assertIsNone(hdfc["gross_margin"])

    def test_the_example_metrics_file_has_the_shape(self):
        with open(os.path.join(ROOT, "trader-analyst", "metrics.example.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        for c in doc["companies"]:
            for k in ("symbol", "source", "fetched_at", "gaps", "newest_filing", "behind_filings", "basis"):
                self.assertIn(k, c)

    def test_json_and_errors(self):
        code, text, _ = run(["M&M", "--offline", DIR, "--now", NOW, "--json"])
        doc = json.loads(text)
        self.assertEqual(doc[0]["rows"][0]["period_end"], "2026-09-30")
        code, text, _ = run(["TCS", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("No table: offline: no file", text)
        code, _, err = run(["M&M", "--quarters", "0", "--offline", DIR])
        self.assertEqual(code, 1)
        self.assertIn("--quarters", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-analyst", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        low = " ".join(text.lower().split())
        for must in ("nse", "unofficial", "check against the filing", "year on year", "gap"):
            self.assertIn(must, low)
        for never in ("sec edgar", "10-q", "8-k", "sec_user_agent", "\u2014"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
