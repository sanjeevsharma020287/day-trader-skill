"""trader-news tests. Fixtures in tests/fixtures/trader-news/:

nse-ann-M&M.json, nse-ann-SBIN.json  NSE corporate announcements (api/corporate-announcements,
                                    5 to 12 Nov 2026), real public filings, read 2026-11-12.
chart-M&M.json, chart-SBIN.json    Yahoo range=1d quotes (copies of trader-alerts' fixtures).
gnews-M&M.xml, gnews-SBIN.xml      SYNTHETIC feeds in the shape of Google News RSS for India:
                                    the headlines are made up (the real feed is for personal reading).
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

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-11-12T09:00"          # India time
UTC = datetime.timezone.utc
R = "₹"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_google_news_rss(self):
        items = news.parse_gnews(raw("gnews-M&M.xml"), "M&M")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Example: Mahindra & Mahindra shares rise after Q2 results beat estimates")
        self.assertEqual(first["source"], "Example Business Daily")      # the " - Publisher" tail moved here
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 11, 5, 10, tzinfo=UTC))
        self.assertEqual(first["id"], "gn:SYN-A1")
        self.assertIsNone(items[5]["time"])                              # an empty pubDate

    def test_nse_announcements(self):
        items = news.parse_nse(raw("nse-ann-M&M.json"), "M&M")
        self.assertEqual(len(items), 16)
        top = items[0]
        self.assertEqual(top["title"], "Analysts/Institutional Investor Meet/Con. Call Updates: Mahindra & Mahindra "
                                       "Limited has informed the Exchange about Transcript of M&M Analyst Meet Q2F27")
        self.assertEqual(top["id"], "nse:106855618")
        self.assertEqual(top["kind"], "filing")
        self.assertTrue(top["link"].startswith("https://nsearchives.nseindia.com/corporate/"))
        # NSE's 11-Nov-2026 18:59:05 is India time: 13:29:05 UTC
        self.assertEqual(top["time"], datetime.datetime(2026, 11, 11, 13, 29, 5, tzinfo=UTC))
        cats = {i["category"]: i["important"] for i in items}
        self.assertTrue(cats["Acquisition"])
        self.assertTrue(cats["Change in Management"])
        self.assertTrue(cats["Cessation"])
        self.assertFalse(cats["Copy of Newspaper Publication"])
        self.assertFalse(cats["Investor Presentation"])

    def test_nse_refusals_are_plain_errors(self):
        with self.assertRaises(news.NewsError) as cm:
            news.parse_nse(b"<html>Access Denied</html>", "M&M")
        self.assertIn("NSE may have refused", str(cm.exception))
        self.assertEqual(news.parse_nse(b'{"data": []}', "M&M"), [])
        with self.assertRaises(news.NewsError):
            news.parse_nse(b'"nope"', "M&M")

    def test_times(self):
        self.assertEqual(news.parse_time("Wed, 11 Nov 2026 05:10:00 GMT").hour, 5)
        self.assertEqual(news.parse_time("2026-11-12T09:00").hour, 3)    # no offset: India time
        self.assertEqual(news.parse_time("2026-11-12T03:30:00Z").minute, 30)
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.nse_time("05-Nov-2026 13:04:12"), datetime.datetime(2026, 11, 5, 7, 34, 12, tzinfo=UTC))
        self.assertIsNone(news.nse_time("yesterday"))

    def test_urls(self):
        self.assertEqual(news.yahoo_symbol("m&m"), "M%26M.NS")
        self.assertEqual(news.gnews_url("M&M", "Mahindra & Mahindra Limited"),
                         "https://news.google.com/rss/search?q=%22Mahindra%20%26%20Mahindra%22&hl=en-IN&gl=IN&ceid=IN:en")
        self.assertEqual(news.gnews_url("XYZ"),
                         "https://news.google.com/rss/search?q=%22XYZ%22%20NSE&hl=en-IN&gl=IN&ceid=IN:en")
        self.assertEqual(news.nse_url("M&M", datetime.datetime(2026, 11, 12, 3, 30)),
                         "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=M%26M"
                         "&from_date=05-11-2026&to_date=12-11-2026")
        self.assertEqual(news.short_name("State Bank of India"), "State Bank of India")
        self.assertEqual(news.short_name("Bajaj Auto Ltd."), "Bajaj Auto")


class Tagging(unittest.TestCase):
    def items(self):
        return (news.parse_gnews(raw("gnews-M&M.xml"), "M&M") + news.parse_gnews(raw("gnews-SBIN.xml"), "SBIN")
                + news.parse_nse(raw("nse-ann-M&M.json"), "M&M") + news.parse_nse(raw("nse-ann-SBIN.json"), "SBIN"))

    def test_touches_and_tags(self):
        watch = {"M&M": "Mahindra & Mahindra Limited", "SBIN": "State Bank of India"}
        items = news.tag(self.items(), watch)
        by = {i["id"]: i for i in items}
        self.assertEqual(by["gn:SYN-A3"]["touches"], ["M&M", "SBIN"])   # "(NSE: SBIN)" in an M&M story
        self.assertEqual(by["gn:SYN-A1"]["tags"], ["results"])
        self.assertEqual(by["gn:SYN-A2"]["tags"], ["analyst"])
        self.assertEqual(by["gn:SYN-B1"]["tags"], ["rating"])
        self.assertEqual(by["gn:SYN-A5"]["tags"], ["macro"])
        self.assertEqual(by["nse:106855618"]["tags"], ["results"])      # "Q2F27"

    def test_dedupe_merges_the_same_story(self):
        watch = {"M&M": None, "SBIN": None}
        items = news.dedupe(news.tag(self.items(), watch))
        ids = [i["id"] for i in items]
        self.assertNotIn("gn:SYN-A1-COPY", ids)       # the same title the same day
        self.assertNotIn("gn:SYN-A3-SBIN", ids)       # the same link from the other feed
        a3 = [i for i in items if i["id"] == "gn:SYN-A3"][0]
        self.assertEqual(a3["touches"], ["M&M", "SBIN"])
        sbin = [i for i in items if i["symbol"] == "SBIN" and i["kind"] == "filing"]
        self.assertEqual(len(sbin), 4)                # two 19:00:41 filings, same wording, two different PDFs
        self.assertEqual(len(items), 6 - 1 + 2 - 1 + 16 + 4)
        # M&M filed three documents with the same wording on Nov 5 (outcome of the board meeting): all stay
        board = [i for i in items if i["kind"] == "filing" and i["category"] == "Outcome of Board Meeting"]
        self.assertEqual(len(board), 3)                # three PDFs, 12:57 to 13:02


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "news.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, *extra):
        return run(["--symbols", "M&M", "SBIN", "--offline", DIR, "--now", NOW, "--state", self.state] + list(extra))

    def test_the_reel_morning(self):
        code, text, err = self.check()
        self.assertEqual(code, 0, err)
        lines = text.splitlines()
        self.assertEqual(lines[0], "M&M        %s3,465.00 +5.30%% vs yesterday's close %s3,290.60 at 15:29 IST  MOVING"
                         % (R, R))
        self.assertTrue(lines[1].startswith("SBIN       %s1,061.20 +1.84%%" % R))
        self.assertIn("  2026-11-11 18:59 IST  M&M          NSE filing", text)
        self.assertIn("2026-11-11 15:35 IST  M&M,SBIN     Example Wire", text)
        self.assertNotIn("2026-11-06", text)                             # outside 48 hours
        self.assertIn("time unknown", text)
        self.assertIn("Checked 2026-11-12 09:00 IST", text)
        self.assertIn("Not financial advice", text)
        # the second check shows nothing new
        code, text, _ = self.check()
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", text)

    def test_important_filings_are_marked(self):
        code, text, _ = self.check("--hours", "168", "--no-moves")
        acq = [l for l in text.splitlines() if "Acquisition: Mahindra" in l]
        self.assertEqual(len(acq), 1)
        self.assertTrue(acq[0].startswith("! 2026-11-06 15:36 IST"), acq[0])
        news_paper = [l for l in text.splitlines() if "Copy of Newspaper Publication" in l]
        self.assertTrue(all(l.startswith("  ") for l in news_paper))

    def test_headlines_only(self):
        code, text, _ = self.check("--no-nse", "--no-moves")
        self.assertNotIn("NSE filing", text)
        self.assertIn("Example Wire", text)

    def test_symbols_from_watchlist_plan_and_names_from_the_universe(self):
        wl = os.path.join(self.d, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"hits": [{"symbol": "SBIN"}]}, fh)
        plan = os.path.join(self.d, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"symbol": "M&M"}, fh)
        uni = os.path.join(self.d, "universe.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("M&M|Mahindra & Mahindra Limited|0.1|FO\nSBIN|State Bank of India|0.05|FO\n")
        code, text, _ = run(["--watchlist", wl, "--plan", plan, "--portfolio", os.path.join(self.d, "none"),
                             "--universe", uni, "--offline", DIR, "--now", NOW, "--state", self.state, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(sorted(m["symbol"] for m in doc["moves"]), ["M&M", "SBIN"])
        self.assertEqual(news.watch_from_files(os.path.join(self.d, "none"), wl, plan), {"SBIN": None, "M&M": None})
        a1 = [i for i in doc["items"] if i["id"] == "gn:SYN-A1"][0]
        self.assertEqual(a1["touches"], ["M&M"])

    def test_no_symbols_is_an_error(self):
        code, _, err = run(["--watchlist", os.path.join(self.d, "x"), "--plan", os.path.join(self.d, "x"),
                            "--portfolio", os.path.join(self.d, "x"), "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_a_short_window_hides_nothing_else(self):
        code, text, _ = self.check("--hours", "15", "--no-moves")
        self.assertIn("18:59 IST", text)
        self.assertNotIn("15:35 IST", text)
        code, text, _ = self.check("--no-moves")
        self.assertIn("15:35 IST", text)                                # not remembered by the short check

    def test_a_bad_now_or_a_missing_file_is_an_error_not_a_crash(self):
        code, _, err = self.check("--now", "soon")
        self.assertEqual(code, 2)
        self.assertIn("--now wants", err)
        code, text, _ = run(["--symbols", "TCS", "--offline", DIR, "--now", NOW, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("note: headlines for TCS: offline: no file", text)
        self.assertIn("note: NSE announcements for TCS", text)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00", "new": "2026-11-10T00:00:00+00:00"}}
        news.remember([], state, datetime.datetime(2026, 11, 12, tzinfo=UTC))
        self.assertEqual(sorted(state["seen"]), ["new"])


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-news", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        low = " ".join(text.lower().split())
        for must in ("nse", "google news", "corporate announcements", "india", "leads, not facts"):
            self.assertIn(must, low)
        for never in ("sec edgar", "8-k", "new york", "sec_user_agent", "\u2014"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
