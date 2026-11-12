"""trader-entry tests. Fixtures in tests/fixtures/trader-entry/:

MM-2026-11-11-5m.json    M&M's Wed Nov 11 2026 five-minute bars on NSE (Yahoo chart JSON v8 for
                         M&M.NS, fetched after the close on Nov 12 and cut to that one day; it is
                         the M&M chart in trader-scanner's wall). 75 bars, 9:15 to 15:25; the
                         15:20 bar came as nulls (the closing auction), and Yahoo's 9:15 bar has
                         volume 0.
MM-5m-live-2026-11-12.json  M&M.NS fetched DURING the Nov 12 session: it ends with the
                         in-progress bar and Yahoo's live tick, off the 5-minute grid.
DEMO-1m-synthetic.json   SYNTHETIC one-minute bars (not market data): a wick above the
                         range high, a close AT the high, a null bar, a live tick. 9:15 IST open.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")

EDIR = os.path.join(FIX, "trader-entry")
MM = os.path.join(EDIR, "MM-2026-11-11-5m.json")
LIVE = os.path.join(EDIR, "MM-5m-live-2026-11-12.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
OPEN_TS = 1794368700       # 2026-11-11 09:15 IST
DT = datetime.datetime
R = "₹"


def ist(text):
    return entry.parse_now(text)


def doc(path=MM):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="M&M"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day):
    """The same session moved to another date (9:15 IST is 03:45 UTC on every day: no summer time)."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(3, 45)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 3,421.10 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        if q["close"][i] is None:
            continue
        q["close"][i] = min(q["close"][i], 3421.0)
        q["high"][i] = min(q["high"][i], 3421.05)
        q["open"][i] = min(q["open"][i], 3421.0)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_1015_on_the_1010_bar(self):
        r = at("2026-11-11 10:15")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 3421.1)     # the 9:15 bar's high
        self.assertEqual(r["opening_range"]["low"], 3347.7)      # the 9:15 bar's low
        self.assertEqual(r["opening_range"]["bars"], 3)          # 9:15, 9:20, 9:25
        t = r["trigger"]
        self.assertEqual(t["time"], "10:15 AM")
        self.assertEqual(t["bar_start"], "10:10")
        self.assertEqual(t["bar_index"], 11)
        self.assertEqual(t["price"], 3427.0)
        self.assertEqual(t["pct_over"], 0.17)                    # 3427.00 / 3421.10 - 1
        self.assertEqual(r["level"], 3421.1)
        self.assertEqual(r["prev_close"], 3290.6)
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"]),
                         ("09:15", "15:30", "09:30", "15:00"))

    def test_at_1012_the_1010_bar_is_still_forming(self):
        r = at("2026-11-11 10:12")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "10:05")
        self.assertEqual(r["last"]["close"], 3413.7)
        self.assertEqual(r["forming"]["bar_start"], "10:10")
        self.assertIn(R + "7.40 under the range high " + R + "3,421.10", r["reason"])
        self.assertEqual(at("2026-11-11 10:14:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-11-11 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("the NSE opens at 9:15", r["reason"])
        r = at("2026-11-11 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-11-11 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-11-11 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:15 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_the_null_auction_bar_is_dropped(self):
        c = chart()
        self.assertEqual(len(c["bars"]), 74)                     # 75 stamps, the 15:20 bar all nulls
        r = at("2026-11-11 15:31")
        self.assertNotIn(73, [b["slot"] for b in r["_bars"]])


class Live(unittest.TestCase):
    """M&M's real v8 answer fetched at 9:33:31 AM on Thu Nov 12 2026: bars 9:15 to 9:30 and the live tick."""

    def test_the_range_is_complete_and_the_930_bar_still_forming(self):
        r = at("2026-11-12 09:33:31", doc(LIVE))
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        o = r["opening_range"]
        self.assertEqual((o["high"], o["low"], o["bars"], o["complete"], o["avg_volume"]),
                         (3427.8, 3395.1, 3, True, 70196))
        self.assertEqual((r["last"]["bar_start"], r["last"]["close"]), ("09:25", 3421.1))
        self.assertEqual(r["forming"]["bar_start"], "09:30")
        self.assertNotIn("lag_note", r)                          # still forming by the clock, not Yahoo lagging
        self.assertEqual([b["et"].strftime("%H:%M") for b in r["_bars"]], ["09:15", "09:20", "09:25"])


class TheOwnersWindow(unittest.TestCase):
    """No entry before 9:30 or at or after 15:00, whatever the config says."""

    def test_a_close_before_930_does_not_count(self):
        # a level under the open: the 9:15 and 9:20 bars close above it but end at 9:20 and 9:25
        r = at("2026-11-11 10:30", setup="level", level=3300)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))

    def test_a_close_at_1500_is_too_late(self):
        d = no_breakout()
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        q["close"][68] = q["high"][68] = 3430.0                  # the 14:55 bar closes above, at 15:00
        r = at("2026-11-11 15:05", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 15:00", r["reason"])
        q["close"][67] = q["high"][67] = 3430.0                  # the 14:50 bar closes above at 14:55: in
        r = at("2026-11-11 15:05", d)
        self.assertEqual((r["status"], r["trigger"]["time"]), ("triggered", "2:55 PM"))

    def test_the_config_can_only_tighten(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "config.json")
            for bad, word in (({"no_entries_after": "15:30"}, "later than 15:00"),
                              ({"no_entries_after": "15:01"}, "later than 15:00"),
                              ({"no_entries_before": "09:15"}, "earlier than 09:30"),
                              ({"no_entries_before": None}, "cannot be switched off"),
                              ({"no_entries_after": None}, "cannot be switched off")):
                with open(path, "w") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(entry.EntryError) as cm:
                    entry.read_config(path)
                self.assertIn(word, str(cm.exception), bad)
            with open(path, "w") as fh:
                json.dump({"no_entries_before": "09:45", "no_entries_after": "14:30"}, fh)
            self.assertEqual(entry.read_config(path), {"no_entries_before": "09:45", "no_entries_after": "14:30"})
        finally:
            shutil.rmtree(tmp)

    def test_evaluate_clamps_a_wide_window_passed_in_directly(self):
        r = at("2026-11-11 10:30", setup="level", level=3300,
               cfg={"no_entries_before": "09:00", "no_entries_after": "16:00"})
        self.assertEqual(r["trigger"]["time"], "9:30 AM")
        self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("09:30", "15:00"))


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-11-11 11:30", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 108354)   # (0 + 211,789 + 113,273) / 3
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("11:10 AM", 22, 3434.6))
        self.assertEqual(t["volume_x"], 1.12)                         # 121,143 / 108,354
        r = at("2026-11-11 10:16", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.98x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-11-11 11:30", setup="level", level=3430)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("11:10 AM", 22, 3434.6))
        with self.assertRaises(entry.EntryError):
            at("2026-11-11 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        self.assertEqual(at("2026-11-11 10:49", d)["status"], "waiting")
        self.assertEqual(at("2026-11-11 14:59", d)["status"], "waiting")
        r = at("2026-11-11 15:00", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 15:00", r["reason"])
        self.assertEqual(at("2026-11-11 15:45", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 17 * 300]   # live: up to the 10:40 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 89 * 60 + 40                       # last trade 10:44:40
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:45"}
        r = at("2026-11-11 10:45:10", d, cfg=cfg)                       # the 10:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-11-11 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_cutoff(self):
        r = at("2026-11-11 10:15", cfg={"no_entries_before": "09:30", "no_entries_after": "10:10"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-11-11 10:30", cfg={"no_entries_before": "10:20", "no_entries_after": "15:00"})
        self.assertEqual(r["trigger"]["time"], "10:20 AM")       # the 10:15 bar (3,421.50), first to close by 10:20

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-11 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-11 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-11-11 10:15"), DT(2026, 11, 11, 4, 45))
        self.assertEqual(entry.parse_now("2026-11-11T04:45:00Z"), DT(2026, 11, 11, 4, 45))
        self.assertEqual(entry.parse_now("2026-11-11 10:15 IST"), DT(2026, 11, 11, 4, 45))

    def test_or_minutes_30(self):
        r = at("2026-11-11 10:49", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 3421.1)
        self.assertEqual(r["trigger"]["time"], "10:15 AM")


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-11-11 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-11-11 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, the 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_the_same_clock_all_year(self):
        for day in (datetime.date(2026, 12, 7), datetime.date(2026, 7, 6), datetime.date(2026, 3, 9)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 10:15" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("10:15 AM", 11), day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 10:12" % day))["status"], "waiting", day)
        self.assertEqual(ist("2026-07-06 10:15"), DT(2026, 7, 6, 4, 45))
        self.assertEqual(ist("2026-12-07 10:15"), DT(2026, 12, 7, 4, 45))

    def test_weekend_holiday_special_session_and_empty_data(self):
        r = at("2026-11-14 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-11-16 09:15")
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        r = at("2026-11-08 18:30")
        self.assertEqual(r["status"], "no-data")
        self.assertIn("Muhurat", r["reason"])
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-11-11 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-12 10:15")                                # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        r = at("2027-03-01 10:30")                                # no NSE calendar for 2027 yet: closed
        self.assertEqual(r["status"], "no-data")
        self.assertFalse(r["calendar_known"])
        self.assertIn("no NSE holiday calendar for 2027", r["reason"])

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "M&M")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(MM) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("M&M", "5m", datetime.date(2026, 11, 11), today=datetime.date(2026, 11, 11))
        self.assertEqual(len(c["bars"]), 74)
        self.assertEqual(self.calls,
                         ["https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("M&M", "5m", datetime.date(2026, 11, 11), today=datetime.date(2026, 11, 11))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("M&M", "5m", datetime.date(2026, 11, 11), today=datetime.date(2026, 11, 11))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "M&M", "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 11, 11), today=datetime.date(2026, 11, 11))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 11, 12)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 11, 11), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 21), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 20), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 21), today, "1m")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("bajaj-auto", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 3,425 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 3425.0, max(b["h"], 3425.0)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 3425.0, "h": 3425.0, "l": 3425.0, "c": 3425.0, "v": 0})
        return {"symbol": "M&M", "meta": {}, "bars": bars, "prev_close": 3290.6, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-11-11 10:07", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:15 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 3427.0)
        # it looked at 10:07 (the 10:05 bar spiking to 3,425 in progress) and at 10:10:02 (that bar closed 3,413.70)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["10:07:00", "10:10:02", "10:15:02"])
        text = "\n".join(lines)
        self.assertIn("10:05 bar  closed %s3,413.70  %s7.40 under %s3,421.10" % (R, R, R), text)
        self.assertIn("10:10 bar  closed %s3,427.00  TRIGGERED" % R, text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-11-11 10:15:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-11-11 10:12", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["10:12:00", "10:15:02", "10:15:32"])   # 10:15:02: Yahoo still at 10:14:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:15 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-11-11 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn(" 9:25 bar  closed %s3,394.40  opening range set: high %s3,421.10, low %s3,347.70" % (R, R, R),
                      text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 12)    # 9:15 ... 10:10, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-11-11 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40", " 9:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:15 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-11 14:30", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "15:00")
        until = DT(2026, 11, 11, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-11 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-14 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-11-11 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:15", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:15 AM at %s3,427.00" % R, text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "M&M", "setup": "orb", "side": "buy", "date": "2026-11-11",
                "status": "triggered", "time": "10:15 AM", "bar_start": "10:10", "bar_index": 11, "price": 3427.0,
                "level": 3421.1, "interval": "5m", "opening_range": {"high": 3421.1, "low": 3347.7, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-11-11T04:45:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:12", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:12", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(MM, "rb") as src, gzip.open(os.path.join("charts", "M&M.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "m&m", "--offline", "charts", "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", MM])
        self.assertEqual(code, 1)
        self.assertIn("holds M&M, not TCS", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "M&M", "--offline", MM, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "M&M", "--offline", MM, "--level", "3300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "M&M", "--offline", MM, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "10:10"}, fh)
        code, text, _ = run(["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "15:30"}, fh)
        code, _, err = run(["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("later than 15:00", err)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "M&M", "--offline", MM])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "M&M", "--offline", MM, "--now", "2026-11-11 09:50", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("10:10 bar  closed %s3,427.00  TRIGGERED" % R, text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 11)
        code, text, _ = run(["watch", "M&M", "--offline", MM, "--now", "2026-11-11 09:50", "--until", "10:00",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(MM, os.path.join("charts", "M&M.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "M&M", "setups": ["orb"], "or_high": 3421.1, "or_low": 3347.7},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("M&M         triggered  10:15 AM  %s3,427.00    %s3,421.10" % (R, R)),
                        rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertIn("DEMO        error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-11 10:12", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "10:15 am", R + "3,427.00", "bar 11",
                     "9:30", "3:00 pm"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", " et "):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\s?\d")                     # no dollar amounts; ${CLAUDE_SKILL_DIR} is fine


if __name__ == "__main__":
    unittest.main()
