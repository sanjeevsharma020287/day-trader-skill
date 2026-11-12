import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-11-11.json.gz")        # 218 real 5-min charts (the F&O list), Wed Nov 11 2026
DAILY = os.path.join(SDIR, "daily-spark-2026-11-11.json")     # 20 real daily charts as one spark answer, read 08:29 IST Nov 12
LISTS = os.path.join(SDIR, "nse-lists-2026-11-12")            # trimmed EQUITY_L.csv, fo_mktlots.csv, Kite instruments/NSE
SPARK5 = os.path.join(SDIR, "spark-5m-live-2026-11-12.json")  # real spark, fetched during the Nov 12 session
MM5 = os.path.join(SDIR, "MM-5m-live-2026-11-12.json")        # real v8 M&M chart, fetched with it
DT = datetime.datetime
D = datetime.date


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """India wall-clock text -> unix seconds (IST is UTC+5:30, no summer time)."""
    local = DT.strptime(ist_text, "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M")
    return int((local - datetime.timedelta(hours=5, minutes=30) - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


class Universe(unittest.TestCase):
    def setUp(self):
        self.texts = {}
        for f, _ in scan.UNIVERSE_FILES:
            with open(os.path.join(LISTS, f), encoding="utf-8") as fh:
                self.texts[f] = fh.read()
        self.kept, self.counts = scan.build_universe(self.texts)

    def test_the_kept_list(self):
        self.assertEqual([k[0] for k in self.kept],
                         ["20MICRONS", "360ONE", "3MINDIA", "BAJAJ-AUTO", "HDFCBANK", "INFY", "J&KBANK", "M&M",
                          "NAM-INDIA", "RELIANCE", "SBIN", "TCS"])
        self.assertEqual(self.counts, {"rows": 17, "keep": 12, "series BE": 2, "series BZ": 2,
                                       "not in Kite's instrument list": 1, "fo": 9})
        mm = dict((k[0], k[1:]) for k in self.kept)["M&M"]
        self.assertEqual(mm, ("Mahindra & Mahindra Limited", 0.1, True))
        # ticks come from Kite's dump, not a guess: 0.01, 0.05, 0.1, 1 and 5 all occur
        ticks = dict((k[0], k[2]) for k in self.kept)
        self.assertEqual((ticks["J&KBANK"], ticks["SBIN"], ticks["TCS"], ticks["BAJAJ-AUTO"], ticks["3MINDIA"]),
                         (0.01, 0.05, 0.1, 1.0, 5.0))
        fo = sorted(k[0] for k in self.kept if k[3])
        self.assertEqual(fo, ["360ONE", "BAJAJ-AUTO", "HDFCBANK", "INFY", "M&M", "NAM-INDIA", "RELIANCE", "SBIN",
                              "TCS"])

    def test_one_case_per_rule(self):
        ticks = scan.parse_kite_ticks(self.texts["instruments_NSE.csv"])
        rows = {r["SYMBOL"]: r for r in scan.parse_equity_list(self.texts["EQUITY_L.csv"])}
        cases = {"21STCENMGM": "series BE", "SHIVAM-RE1": "series BE",      # trade-for-trade: no intraday
                 "AGSTRA": "series BZ", "IL&FSENGG": "series BZ",
                 "ESFL": "not in Kite's instrument list",                    # lists on Nov 13, not in Kite yet
                 "M&M": "keep", "BAJAJ-AUTO": "keep", "3MINDIA": "keep"}
        for sym, want in cases.items():
            self.assertEqual(scan.classify(rows[sym], ticks), want, sym)
        self.assertEqual(scan.classify({"SYMBOL": "BAD SYM", "SERIES": "EQ"}, {"BAD SYM": 0.05}),
                         "not a plain symbol")
        self.assertNotIn("NIFTY 50", ticks)                                  # an index row, not an equity

    def test_not_the_files_it_wants(self):
        for bad, fn in (("<html>maintenance</html>", scan.parse_equity_list),
                        ("a,b\n1,2\n", scan.parse_fo_list), ("", scan.parse_kite_ticks)):
            with self.assertRaises(scan.ScanError):
                fn(bad)
        texts = dict(self.texts)
        del texts["fo_mktlots.csv"]
        with self.assertRaises(scan.ScanError) as cm:
            scan.build_universe(texts)
        self.assertIn("fo_mktlots.csv", str(cm.exception))


class Time(unittest.TestCase):
    def test_india_clock(self):
        self.assertEqual(scan.ts_to_ist(ts("2026-11-11 09:15")), DT(2026, 11, 11, 9, 15))
        self.assertEqual(scan.ts_to_ist(1794455100), DT(2026, 11, 12, 9, 15))   # Yahoo's regular start, Nov 12
        self.assertEqual(scan.parse_now("2026-11-11 10:15"), DT(2026, 11, 11, 4, 45))
        self.assertEqual(scan.parse_now("2026-07-01 10:15"), DT(2026, 7, 1, 4, 45))  # no summer time
        self.assertEqual(scan.parse_now("2026-11-11T04:45:00Z"), DT(2026, 11, 11, 4, 45))
        self.assertEqual(scan.clock_label(DT(2026, 11, 11, 10, 15)), "10:15 AM")
        self.assertEqual(scan.clock_label(datetime.time(15, 0)), "3:00 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_session_is_nse(self):
        self.assertEqual(scan.session(D(2026, 11, 11)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 11, 10)))     # Diwali-Balipratipada
        self.assertIsNone(scan.session(D(2026, 11, 8)))      # Muhurat Sunday: not a session for this pack
        self.assertIsNone(scan.session(D(2027, 1, 4)))       # no 2027 calendar yet: fail closed

    def test_money_is_rupees_with_indian_grouping(self):
        self.assertEqual(scan.money(3427.0), "₹3,427.00")
        self.assertEqual(scan.money(49930), "₹49,930.00")
        self.assertEqual(scan.money(1234567.8), "₹12,34,567.80")
        self.assertEqual(scan.grouped(500000), "5,00,000")
        self.assertEqual(scan._p(49205.0), "49,205.00")


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        with open(DAILY, encoding="utf-8") as fh:
            text = fh.read()
        data = scan.parse_spark(text)
        self.assertEqual(len(data), 20)
        self.assertIn("M&M", data)                  # M&M.NS comes back as ours
        self.assertIn("BAJAJ-AUTO", data)
        t = data["M&M"]
        self.assertEqual(len(t["t"]), 63)
        self.assertIsNone(t["h"])                   # spark is closes only
        self.assertEqual(t["c"][-1], 3465.0)
        self.assertEqual(t["meta"]["regularMarketVolume"], 5574186)
        closes = scan.daily_closes(t, scan.parse_now("2026-11-12 08:30"))
        self.assertEqual(closes[-1], (D(2026, 11, 11), 3465.0))
        self.assertNotIn(D(2026, 10, 20), dict(closes))   # Dussehra: no bar
        self.assertNotIn(D(2026, 11, 10), dict(closes))   # Diwali-Balipratipada: no bar

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls(self):
        self.assertEqual(scan.to_yahoo("m&m"), "M&M.NS")
        self.assertEqual(scan.from_yahoo("BAJAJ-AUTO.NS"), "BAJAJ-AUTO")
        self.assertEqual(scan.spark_path(["M&M", "reliance", "BAJAJ-AUTO"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,RELIANCE.NS,BAJAJ-AUTO.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("M&M", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(DAILY, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=M%26M.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/M%26M.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = sorted(scan.parse_spark(self.body)) + ["ZZZZQX"] + ["X%02d" % i for i in range(19)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 20)
        self.assertEqual(len(failed), 20)
        self.assertIn("HTTP 429", failed["ZZZZQX"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-12 08:30"), 50, 500000)
        self.assertEqual(counts["requested"], 40)
        self.assertEqual(counts["fetched"], 20)

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Checked from the closes with a separate reference script (not this tool), through Wed Nov 11 2026.
    EXPECT = {
        "BHARTIARTL": ["breakout_20d", "bull_flag"],
        "ATHERENERG": ["breakout_20d", "bull_flag"],
        "M&M": ["breakout_20d"],         # 3465.00 > 3290.60, the best close of the 20 before
        "RELIANCE": ["breakout_20d"],
        "SBIN": ["breakout_20d"],
        "HDFCBANK": ["breakout_20d"],
        "BAJAJ-AUTO": ["breakout_20d"],
        "DRREDDY": ["bull_flag"],        # 1229.50 (Oct 23) over 1218.80 ... up more than 10 %, then a tight 5 days
        "INDHOTEL": ["bull_flag"],
        "ZYDUSLIFE": ["bull_flag"],
        "COALINDIA": ["reclaim_ma20"],   # 401.50 under its average, then 406.30 over it
        "COLPAL": ["reclaim_ma20"],      # its last bar is Nov 9: Yahoo had no Nov 11 bar for it
        "OFSS": ["reclaim_ma20"],
        "POLYCAB": ["reclaim_ma20"],
        "ENRIN": ["reclaim_ma20"],
        "PIIND": ["oversold", "oversold_bounce"],  # RSI 28.8 after the Nov 5 gap down, closed up 1712.90 -> 1761.00
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        self.assertEqual(len(self.syms), 20)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-12 08:30"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 11, 11))      # the date most charts end on (COLPAL ends Nov 9)
        self.assertEqual(counts, {"requested": 20, "fetched": 20, "illiquid": 0, "short": 0, "scanned": 20,
                                  "hits": 16})
        mm = [h for h in hits if h["symbol"] == "M&M"][0]
        self.assertEqual((mm["price"], mm["rsi"], mm["close_date"]), (3465.0, 81.1, "2026-11-11"))
        piind = [h for h in hits if h["symbol"] == "PIIND"][0]
        self.assertEqual(piind["rsi"], 28.8)

    def test_the_in_progress_bar_is_dropped(self):
        s = bars_series("2026-11-12", [10.0])
        s["t"] = [ts("2026-11-11 09:15"), ts("2026-11-12 09:15")]
        s["c"] = [10.0, 11.0]
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-11-12 15:29"))), 1)
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-11-12 15:30"))), 2)   # done at the close
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-12 08:30"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-12 11:00"), 0, 0)
        self.assertEqual(before, during)

    def test_liquidity_filter(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-12 08:30"), 50, 500000)
        # out: IDEA and YESBANK under Rs 50; OFSS, POLYCAB, COLPAL, BAJAJ-AUTO under 5,00,000 shares
        self.assertEqual(counts["illiquid"], 6)
        self.assertEqual(counts["scanned"], 14)
        self.assertEqual(len(hits), 12)
        for gone in ("IDEA", "YESBANK", "OFSS", "POLYCAB", "COLPAL", "BAJAJ-AUTO"):
            self.assertNotIn(gone, [h["symbol"] for h in hits])
        self.assertEqual(hits[0]["symbol"], "HDFCBANK")  # most rupees traded first

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(1, 11)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-11-12 08:30"), 50, 500000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_matches_the_textbook(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(15, 0))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_reel_scan_at_1015(self):
        hits, counts, failed = self.scan_at("2026-11-11 10:15")
        self.assertEqual(len(self.syms), 218)
        self.assertEqual(self.errors, {})
        self.assertEqual(counts["scanned"], 218)
        self.assertEqual(len(hits), 100)                  # the same count from the reference script
        mm = [h for h in hits if h["symbol"] == "M&M"][0]
        self.assertEqual(mm, {"symbol": "M&M", "setups": ["orb"], "price": 3427.0, "at": "10:15 AM",
                              "bar_start": "10:10", "bar_index": 11, "or_high": 3421.1, "or_low": 3347.7,
                              "pct_over": 0.17})
        self.assertEqual(hits[0]["symbol"], "360ONE")     # sorted by time, then symbol
        ats = [h["at"] for h in hits]
        self.assertEqual(sum(1 for a in ats if a == "9:35 AM"), 36)
        self.assertEqual(ats.count("10:15 AM"), 1)
        self.assertEqual(ats[-1], "10:15 AM")
        hdfc = [h for h in hits if h["symbol"] == "HDFCBANK"][0]
        self.assertEqual((hdfc["at"], hdfc["price"], hdfc["or_high"], hdfc["pct_over"]), ("9:40 AM", 814.1, 813.7,
                                                                                          0.05))

    def test_a_short_range_never_counts_a_bar_closing_before_930(self):
        # R27, SYNTHETIC: a 5-minute range (9:15 bar); the 9:20 bar closes above it at 9:25, before entries open.
        day, cut = D(2026, 11, 11), DT(2026, 11, 11, 9, 50)
        early = bars_series("2026-11-11", [100.0, 101.0, 99.0, 99.0, 99.0, 99.0, 99.0])
        bars = scan.session_bars(early, day, cut)
        self.assertFalse(scan.orb_candidate(bars, 1))
        self.assertIsNone(scan.orb_hit(bars, 1))
        later = bars_series("2026-11-11", [100.0, 101.0, 100.05, 100.5, 99.0, 99.0, 99.0])
        bars = scan.session_bars(later, day, cut)
        self.assertTrue(scan.orb_candidate(bars, 1))
        self.assertEqual(scan.orb_hit(bars, 1)["at"], "9:35 AM")       # the 9:30 bar, not the 9:20 one
        hits, _, _ = scan.scan_orb({"AAA": early, "BBB": later}, ["AAA", "BBB"], day, cut, 5)
        self.assertEqual([(h["symbol"], h["at"]) for h in hits], [("BBB", "9:35 AM")])
        with self.assertRaises(scan.ScanError) as e:
            scan.orb_window(scan.parse_now("2026-11-11 09:27"), None, 5, datetime.time(15, 0))
        self.assertIn("the first bar that can count closes at 9:30 AM. Run orb again after 9:30 AM", str(e.exception))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-11-11 10:15", min_price=50)
        self.assertEqual(len(hits), 97)                   # SUZLON, SAGILITY, YESBANK, IDEA under Rs 50
        self.assertEqual(counts["illiquid"], 4)
        for s in ("SUZLON", "SAGILITY", "YESBANK", "IDEA"):
            self.assertNotIn(s, [h["symbol"] for h in hits])

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-11-11 10:14", syms=["M&M"])
        self.assertEqual(hits, [])                         # the 10:10 bar is not done until 10:15
        hits, _, _ = self.scan_at("2026-11-11 10:15", syms=["M&M"])
        self.assertEqual(hits[0]["at"], "10:15 AM")

    def test_stage_one_is_a_necessary_condition(self):
        day, cut = D(2026, 11, 11), DT(2026, 11, 11, 10, 15)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, day, cut)
            if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, D(2026, 11, 11), DT(2026, 11, 11, 10, 15), 15,
                                             0, 0, confirm)
        self.assertEqual(len(hits), 100)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertEqual(len(asked), 152)                  # stage 1 skipped 66 charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["M&M", "HDFCBANK"], D(2026, 11, 11),
                                             DT(2026, 11, 11, 10, 15), 15, 0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["M&M"])

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-11-11", [100, 100.2, 100.1, None, 100.5, 100.6, 100.7, 100.8, 100.9])
        bars = scan.session_bars(s, D(2026, 11, 11), DT(2026, 11, 11, 10, 0))
        self.assertFalse(scan.usable(bars, D(2026, 11, 11), DT(2026, 11, 11, 10, 0), 3))

    def test_a_30_minute_range_and_the_live_tick(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-11-11", closes, highs=[c + 0.2 for c in closes],
                        extra=[("2026-11-11 12:03:41", 105.0)])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 11, 11), DT(2026, 11, 11, 12, 5), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")         # range 9:15 to 9:45 (high 101.4), the 9:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        bars = scan.session_bars(s, D(2026, 11, 11), DT(2026, 11, 11, 12, 5))
        self.assertEqual(len(bars), 34)                    # 9:15 to 12:00; the 12:03:41 tick off the grid dropped

    def test_window_messages(self):
        until = datetime.time(15, 0)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-11-14 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("Muhurat", msg("2026-11-08 18:15"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-11 09:25"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-11-11 09:34"))
        self.assertIn("expired", msg("2026-11-11 15:00"))                 # the owner's 3:00 PM rule
        self.assertIn("expired", msg("2026-11-11 15:10", until=datetime.time(16, 0)))  # cannot be pushed later
        self.assertIn("expired", msg("2026-11-11 11:30", until=datetime.time(11, 0)))  # but can be made earlier
        self.assertIn("closed at 3:30 PM", msg("2026-11-11 16:30"))
        self.assertIn("later than now", msg("2026-11-11 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-11-11 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-11 16:30"), datetime.time(10, 15), 15, until),
                         (D(2026, 11, 11), DT(2026, 11, 11, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-11 14:59"), None, 15, until)[1],
                         DT(2026, 11, 11, 14, 59))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-11 10:15"), None, 7, until)

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        day, cut = D(2026, 11, 11), DT(2026, 11, 11, 10, 15)
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, day, cut, 15, 0, 0, confirm, max_confirm=10)
        self.assertEqual((counts["candidates"], counts["confirmed"], counts["unconfirmed"]), (152, 10, 142))
        self.assertEqual(sorted(asked), sorted(["KALYANKJIL", "HDFCBANK", "ICICIBANK", "SBIN", "RELIANCE", "M&M",
                                                "COCHINSHIP", "BSE", "INDIGO", "WAAREEENER"]))
        self.assertEqual(len(hits), 8)                     # ICICIBANK and COCHINSHIP did not break out


class LiveFiveMinute(unittest.TestCase):
    """A real spark 5-minute answer and M&M's v8 chart, fetched at 9:33 AM on Thu Nov 12 2026."""

    def setUp(self):
        with open(SPARK5, encoding="utf-8") as fh:
            self.doc = json.load(fh)
        self.spark = scan.parse_spark(self.doc["spark_answer"])
        self.v8 = scan.parse_chart(self.doc["mm_chart"], "M&M")
        self.now = scan.parse_now(self.doc["fetched_at_utc"])

    def test_in_progress_bar_and_live_tick_are_dropped(self):
        exp = self.doc["expect"]
        self.assertEqual(len(self.spark), exp["symbols"])
        self.assertIn("M&M", self.spark)
        t = self.spark["M&M"]
        last = scan.ts_to_ist(t["t"][-1])
        self.assertTrue(last.second or last.minute % 5, "the last stamp is the live tick, off the grid")
        # Fetched at 9:33:31 AM: orb is refused until the 9:30 bar closes, so cut at the fetch minute here.
        with self.assertRaises(scan.ScanError) as e:
            scan.orb_window(self.now, None, 15, datetime.time(15, 0))
        self.assertIn("Run orb again after 9:35 AM", str(e.exception))
        day, cut = D(2026, 11, 12), datetime.datetime(2026, 11, 12, 9, 33)
        bars = scan.session_bars(t, day, cut)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars], exp["complete_bars"])
        self.assertEqual(len(scan.session_bars(self.v8, day, cut)), len(exp["complete_bars"]))
        # Five minutes on, the 9:30 bar is complete; the 9:33:32 tick is still not a bar.
        later = [b["start"].strftime("%H:%M") for b in scan.session_bars(t, day, datetime.datetime(2026, 11, 12, 9, 40))]
        self.assertEqual(later, exp["complete_bars"] + ["09:30"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", LISTS, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 12 NSE stocks in the EQ series with a Kite tick size; 9 of them are on the F&O list",
                      text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 12)
        self.assertIn("M&M|Mahindra & Mahindra Limited|0.1|FO", lines)
        self.assertIn("J&KBANK|The Jammu & Kashmir Bank Limited|0.01|", lines)

        # daily over that universe with the fixture charts: 7 are in both, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-11-12 08:30",
                             "--out", self.wl, "--min-volume", "0"])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 7 of 12 charts (5 failed or had no data)", text)
        self.assertIn("(Rs 50+ and 0+ shares)", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"]), ("daily", 12, 7, 5))
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["HDFCBANK", "SBIN", "RELIANCE", "M&M", "BAJAJ-AUTO"])
        self.assertEqual(doc["as_of"], "2026-11-11 close")

        # the reel's 10:15 scan
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-11 10:15", "--min-price", "0",
                             "--out", self.wl, "--show", "100"])
        self.assertEqual(code, 0)
        self.assertIn("100 of 218 charts closed above their opening-range high by 10:15 AM", text)
        self.assertIn("10:15 AM  M&M           ₹3,427.00  range 3,347.70 to 3,421.10  +0.17% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source")},
                         {"created_by": "trader-scanner", "created_at": "2026-11-11T04:45:00Z", "mode": "orb",
                          "as_of": "2026-11-11 10:15 IST", "universe": 218, "scanned": 218, "source": "yahoo"})
        self.assertEqual(len(doc["hits"]), 100)

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "10:15", "--watchlist", self.wl, "--min-price", "0",
                             "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (100, 100, 100))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the Nov 11 bundle."""
        bundle = json.loads(scan._read_maybe_gz(WALL))
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    sym = scan.from_yahoo(y.replace("%26", "&"))
                    r = ((bundle.get(sym) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": sym + ".NS",
                                    "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[scan.from_yahoo(path.split("/chart/")[1].split("?")[0].replace("%26", "&"))])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-11 10:20")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-11-11 10:15", "--min-price", "0",
                                 "--max-confirm", "10", "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("218 usable; 152 candidates, 10 confirmed", text)
        self.assertIn("PARTIAL: 142 more candidates were not checked", text)
        # spark has no volumes, so stage 2 ranks by the day's volume in the meta (reference script: 9 of 10)
        self.assertIn("9 of the 10 checked charts closed above their opening-range high by 10:15 AM", text)
        self.assertNotIn("ICICIBANK", text)                # checked, did not break out
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 10)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 11)   # 218 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertTrue(any("M%26M.NS" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-14 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-11-14 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-11 09:31", "--out", self.wl])
        self.assertIn("after 9:35 AM", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-11-11 10:15", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-11-11 10:10", "--symbols", "M&M",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 10:10 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("M&M", "HDFCBANK", "ICICIBANK"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        with open(os.path.join(folder, "NOPE.json"), "w") as fh:
            json.dump({"chart": {"result": None, "error": {"code": "Not Found",
                                                           "description": "No data found, symbol may be delisted"}}},
                      fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-11 10:15", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["HDFCBANK", "M&M"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nM&M|Mahindra & Mahindra Limited|0.1|FO\nsbin\n\nM&M\n")
        self.assertEqual(scan.symbols_from_file(txt), ["M&M", "SBIN", "M&M"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "m&m"}, {"symbol": "SBIN"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["M&M", "SBIN"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-12 08:30",
                             "--json", "--no-write", "--min-volume", "0"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-12 08:30", "--json",
                             "--no-write", "--min-volume", "0"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-12 08:30", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 16)
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


if __name__ == "__main__":
    unittest.main()
