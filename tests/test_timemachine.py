import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
MM = os.path.join(DIR, "MM-60d5m.json.gz")   # Yahoo v8 chart for M&M.NS, range=60d interval=5m, fetched 2026-11-12 08:53 IST
with gzip.open(MM, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-11-11 15:35"
NOW_UTC = tm.parse_now(NOW)
R = "₹"

# the reel's plan, as trader-exit writes it
PLAN = {"created_by": "trader-exit", "symbol": "M&M", "side": "buy", "date": "2026-11-11", "setup": "orb",
        "entry": 3427.0, "entry_time": "10:15 AM", "entry_bar": 11, "interval": "5m",
        "opening_range": {"high": 3421.1, "low": 3347.7, "minutes": 15},
        "stop": 3384.4, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 3469.6, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 42.6, "reward_risk": 1.0, "flat_by": "15:00", "tick": 0.1, "fo": True, "exchange": "NSE"}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def reference_run(text, entry, stop, target, k, last, seed=19, replays=10000):
    """An independent re-implementation from the docstring (raw JSON in, no tool functions):
    full 9:15-15:00 sessions before the plan's day, the ratio pool, the replays and the bands."""
    r = json.loads(text)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    days = {}
    for i, ts in enumerate(r["timestamp"]):
        o, hi, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, hi, lo, c) or ts % 300:
            continue
        t = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts + 19800)
        slot = (t.hour * 60 + t.minute - 555) // 5
        if 0 <= slot < 69:
            days.setdefault(t.date().isoformat(), {})[slot] = (o, hi, lo, c)
    full = [[days[d][i] for i in range(69)] for d in sorted(days) if d < PLAN["date"] and len(days[d]) == 69]
    pool = [[] for _ in range(69)]
    for bars in full:
        for i in range(1, 69):
            pool[i].append(tuple(x / bars[i - 1][3] for x in bars[i]))
    rng = random.Random(seed)
    paths, outs = [], []
    for n in range(replays):
        p, oc, path = entry, None, [entry]
        for i in range(k + 1, last + 1):
            o, hi, lo, c = rng.choice(pool[min(68, max(1, i + rng.randint(-2, 2)))])
            if oc is None:
                oc = "stop" if p * lo <= stop else "target" if p * hi >= target else None
            p *= c
            if n < 160:
                path.append(round(p, 2))
        outs.append(oc or "close")
        if n < 160:
            paths.append(path)
    rng = random.Random(seed)
    allp = []
    for n in range(2000):
        p, path = entry, [entry]
        for i in range(k + 1, last + 1):
            p *= rng.choice(pool[min(68, max(1, i + rng.randint(-2, 2)))])[3]
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(col[min(1999, int(qq * 2000))], 2) for qq in (0.05, 0.25, 0.5, 0.75, 0.95)])
    return len(full), outs.count("target") / replays, outs.count("stop") / replays, paths, bands


class Reel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_reel_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:00"))
        self.assertEqual(m["pool_sessions"], 58)
        self.assertEqual(m["bars_left"], 57)                  # 10:15 to 15:00
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-14", "2026-11-09"))
        self.assertEqual(m["p_target"], 0.1611)
        self.assertEqual(m["p_stop"], 0.1869)
        self.assertEqual(m["p_neither"], 0.652)
        self.assertEqual(m["p_green"], 0.4817)
        self.assertEqual(m["median_close"], 3425.34)
        self.assertEqual(m["p5_close"], 3375.26)
        self.assertEqual(m["p95_close"], 3477.82)
        self.assertEqual(m["avg_R"], -0.029)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit_with_an_independent_reference(self):
        m = self.doc["mc"]
        n, pt, ps, paths, bands = reference_run(TEXT, 3427.0, 3384.4, 3469.6, 11, 68)
        self.assertEqual((n, pt, ps), (m["pool_sessions"], m["p_target"], m["p_stop"]))
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(m["sample_paths"], paths)
        self.assertEqual(m["bands"], bands)
        self.assertEqual(sha(m["bands"]), "1345a792f9196f42")
        self.assertEqual(m["hist"]["bins"], [104, 96, 136, 212, 333, 384, 477, 539, 657, 694, 793, 775, 781, 756,
                                             681, 574, 533, 391, 290, 195, 167, 123, 85, 61])
        self.assertEqual((m["hist"]["lo"], m["hist"]["hi"]), (3353.73, 3501.31))
        self.assertEqual(m["bands"][-1], [3376.43, 3403.69, 3423.96, 3445.97, 3476.88])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (58, 32))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (12, 10, 10, 17))
        self.assertEqual(h["avg_R"], 0.038)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0,
                                     "tick": 0.1})
        # by hand, Aug 14: range 3,391.30 to 3,421.50 (mid 3,406.40); the 13:45 bar is the first close above
        # it (3,424.90); neither side is touched by 15:00, where it closes 3,425.50: R = 0.60 / 18.50
        first = h["trades"][0]
        self.assertEqual(first, {"date": "2026-08-14", "time": "1:50 PM", "entry": 3424.9, "stop": 3406.4,
                                 "target": 3443.4, "outcome": "close", "R": 0.03})
        for t in h["trades"]:
            hh, mm = map(int, t["time"].split()[0].split(":"))
            mins = (hh % 12 + (12 if t["time"].endswith("PM") else 0)) * 60 + mm
            self.assertTrue(9 * 60 + 30 <= mins < 15 * 60, t)      # the owner's window
            self.assertAlmostEqual(round(t["stop"] * 10) / 10, t["stop"], places=6)   # on M&M's 0.1 tick

    def test_the_verdict_says_coin_flip(self):
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 16.1 % vs stop first 18.7 %.")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.463, 0.5))
        self.assertEqual(self.doc["verdict_notes"],
                         ["65.2 % of the replays hit neither by 3:00 PM; the price at 3:00 PM decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [3427.0] * 5)
        self.assertEqual(len(m["bands"]), 58)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:15", "15:00", 58))
        self.assertTrue(all(p[0] == 3427.0 and len(p) == 58 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("M&M", 3427.0, 3384.4, 3469.6, 11, "10:15 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["tick"]), (42.6, 1.0, "15:00", 0.1))
        self.assertEqual(self.doc["warnings"], [])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("M&M buy, Wednesday 2026-11-11: entry %s3,427.00 at 10:15 AM (bar 11), stop %s3,384.40, "
                     "target %s3,469.60" % (R, R, R),
                     "10,000 replays", "57 five-minute bars", "seed 19", "58 full sessions",
                     "target first   16.1 %", "stop first     18.7 %", "neither        65.2 %   still open at 3:00 PM",
                     "green          48.2 %", "median %s3,425.34, 90 %% of replays between %s3,375.26 and %s3,477.82"
                     % (R, R, R), "Average R: -0.029", "fired 32 times: 12 target, 10 stop, 10 neither. Average R +0.038",
                     "Close to a coin flip: target first 16.1 % vs stop first 18.7 %.\n65.2 % of the replays "
                     "hit neither by 3:00 PM; the price at 3:00 PM decides those.", "not a prediction", "--seed 19",
                     "Left out 1 short session(s) (gaps or special sessions): 2026-10-15."):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$", " ET"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.1585, 0.1773, 3426.04))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), "1345a792f9196f42")

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "M&M")

    def test_only_915_to_1500_is_used(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # 249 bars from 15:00 on (the last half hour and the closing auction), 113 null auction bars
        self.assertEqual(dropped, {"no_price": 113, "off_grid": 0, "not_complete": 0, "outside_hours": 249})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-11-11", "14:55", 68))
        self.assertTrue(all("09:15" <= b["start"] <= "14:55" for b in bars))
        entry = [b for b in bars if b["date"] == "2026-11-11" and b["slot"] == 11][0]
        self.assertEqual((entry["start"], entry["c"]), ("10:10", 3427.0))

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-11-11 10:12"))[1]
        today = [b for b in bars if b["date"] == "2026-11-11"]
        self.assertEqual((len(today), today[-1]["start"]), (11, "10:05"))
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-11 10:12"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("10:05 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-11 10:15"), replays=200)["mc"]["pool_sessions"], 58)

    def test_india_time_all_year(self):
        stamps = [1786333500,   # 2026-08-10 03:45Z = 9:15 IST
                  1798775100,   # 2027-01-01 03:45Z = 9:15 IST (no summer time either side)
                  1786332600,   # 2026-08-10 03:30Z = 9:00 IST, the pre-open
                  1786354200]   # 2026-08-10 09:30Z = 15:00 IST, after the pack's day
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 4, "high": [1] * 4, "low": [1] * 4, "close": [1] * 4,
                                         "volume": [0] * 4}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2027-01-02T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-08-10", "09:15", 0), ("2027-01-01", "09:15", 0)])
        self.assertEqual(dropped["outside_hours"], 2)

    def test_a_session_with_a_gap_is_left_out(self):
        def cut(res):  # Sep 4 loses its bars from 13:00, as on a halt
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": "2026-09-04", "bars": 45},
                                                                  {"date": "2026-10-15", "bars": 67}])
        self.assertEqual(doc["history"]["sessions"], 57)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-11-09")
        self.assertNotIn("2026-11-11", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 114)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)   # 2026-08-14 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "M&M.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-12")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-10-09")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_a_plan_day_with_no_session_is_refused(self):
        for day, word in (("2026-11-10", "Diwali-Balipratipada"), ("2026-11-08", "Muhurat"),
                          ("2026-11-14", "weekend"), ("2027-01-04", "no NSE holiday calendar")):
            with self.assertRaises(tm.TMError) as cm:
                machine(plan(date=day), replays=10)
            self.assertIn(word, str(cm.exception), day)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for M&M", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-12"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:17 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:00 PM", entry_bar=None), "between 9:30 AM and 2:55 PM"),
            (dict(entry_time="9:25 AM", entry_bar=None), "no entry before 9:30"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=3430.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Nov 11"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(tick=0), "tick must be above 0"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:15 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="10:15", entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="10:15am", entry_bar=None)["entry_bar"], 11)
        self.assertEqual(plan(entry_time="9:30 AM", entry_bar=None)["entry_bar"], 2)
        self.assertEqual(plan(entry_time="2:55 PM", entry_bar=None)["entry_bar"], 67)

    def test_until_cuts_the_replay_and_never_passes_1500(self):
        doc = machine(until="14:45", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("14:45", 54, "14:45"))
        self.assertIn("still open at 2:45 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="2:45 PM", replays=500)["mc"], m)
        self.assertEqual(machine(plan(flat_by="14:45"), replays=500)["mc"]["until"], "14:45")   # the plan's flat_by
        for bad, words in (("10:15 AM", "nothing to replay"), ("15:05", "no later than 3:00 PM"),
                           ("15:30", "the owner's rule"), ("14:47", "5-minute grid"), ("noon", "India time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=3440.0, stop=3400.0, target=3480.0), replays=200)
        self.assertIn("outside that bar's range 3413.50 to 3427.00", doc["warnings"][0])

    def test_stop_rounds_away_from_the_entry_on_the_tick(self):
        self.assertEqual(tm.cents_away(358.325, True), 358.32)
        self.assertEqual(tm.cents_away(358.325, False), 358.33)
        self.assertEqual(tm.cents_away(3384.4, True, 0.1), 3384.4)
        self.assertEqual(tm.cents_away(3384.45, True, 0.1), 3384.4)
        self.assertEqual(tm.cents_away(3384.45, False, 0.1), 3384.5)
        self.assertEqual(tm.cents_away(100.015, True, 0.01), 100.01)
        self.assertEqual(tm.cents_away(49932.5, True, 5), 49930)
        # Yahoo's float for 3,347.70: rule_stop snaps it to paise before the tick
        rule = {"stop_rule": "or_low", "tick": 0.1}
        self.assertEqual(tm.rule_stop(rule, 3421.10009765625, 3347.699951171875, 3427.0, True), 3347.7)
        self.assertEqual(tm.on_tick(3427.04, 0.1), 3427.0)
        self.assertEqual(tm.on_tick(3427.05, 0.1), 3427.1)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=3347.7, target=3506.3), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=3402.5, target=3476.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.7149, 2.0))

    def test_the_history_keeps_the_owners_window(self):
        # synthetic sessions: a flat range, then one close above it at the bar given
        def session(breakout):
            bars = [{"o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0} for _ in range(tm.SESSION_BARS)]
            bars[breakout] = {"o": 100.0, "h": 101.0, "l": 99.9, "c": 101.0}
            return ("2026-11-02", bars)
        rule = {"or_minutes": 5, "stop_rule": "or_low", "target_r": 1.0, "tick": 0.05}
        h = tm.history_replay([session(1)], rule, tm.SESSION_BARS - 1)   # bar 1 closes 9:25: too early
        self.assertEqual(h["triggered"], 0)
        h = tm.history_replay([session(2)], rule, tm.SESSION_BARS - 1)   # bar 2 closes 9:30: in
        self.assertEqual((h["triggered"], h["trades"][0]["time"]), (1, "9:30 AM"))
        h = tm.history_replay([session(68)], rule, tm.SESSION_BARS - 1)  # bar 68 closes 15:00: too late
        self.assertEqual(h["triggered"], 0)
        h = tm.history_replay([session(67)], rule, tm.SESSION_BARS - 1)  # 14:55: in
        self.assertEqual(h["trades"][0]["time"], "2:55 PM")

    def test_a_special_session_never_enters_the_pool(self):
        full = {i: {"slot": i} for i in range(tm.SESSION_BARS)}
        sessions, short = tm.full_sessions({"2026-11-08": full, "2026-11-09": full}, "2026-11-12")
        self.assertEqual([d for d, _ in sessions], ["2026-11-09"])         # Muhurat Sunday left out

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=3469.6, target=3384.4, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "14:45"}),
                         "No replay reached the target or the stop by 2:45 PM: the price at 2:45 PM decides every one.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0}).split(" by ")[1][:7], "3:00 PM")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture_near_break_even(self):
        # stop 3,410.00, 2R target 3,461.00: 23.8 % vs 57.7 % looks lopsided, but 2R only needs a third
        doc = machine(plan(stop=3410.0, target=3461.0, stop_rule="pct"))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["avg_R"]), (2.0, 0.2379, 0.5771, -0.007))
        self.assertEqual(doc["verdict"], "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 29 % (target first 23.8 % vs stop "
                                         "first 57.7 %).")
        self.assertEqual(doc["verdict_notes"], [])

    def test_2r_on_the_fixture_a_clear_lean(self):
        # the reel's entry and stop with the default 2R target 3,512.20
        doc = machine(plan(target=3512.2))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.0092, 0.1872, 0.8036, -0.031))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 5 % (target first 0.9 % vs stop "
                                         "first 18.7 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["80.4 % of the replays hit neither by 3:00 PM; the price at 3:00 PM decides those."])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("m&m"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=60d&interval=5m")
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("bajaj-auto", "query1"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; day-trader-skill/2.0; "
                                "+https://github.com/Jakeschincariol/day-trader-skill)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("M&M"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("M&M"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("M&M"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("M&M")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", MM, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   16.1 %", text)
        self.assertIn("Close to a coin flip: target first 16.1 % vs stop first 18.7 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.1611, -0.029))
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (32, 0.038))
        self.assertEqual(doc["plan"]["from"], self.plan)
        self.assertEqual(doc["data"]["last_bar"], "2026-11-11 14:55 IST")

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", MM, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.1869)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:20], "Close to a coin flip")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "m&m", "--date", "2026-11-11", "--entry", "3427",
                               "--stop", "3384.4", "--target", "3469.6", "--entry-time", "10:15 AM", "--tick", "0.1",
                               "--offline", MM, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.1611, 0.4817))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (12, 10, 0.038))
        code, _, err = run(["run", "--symbol", "M&M", "--entry", "3427", "--offline", MM])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(MM, os.path.join(self.d, "M&M-60d5m.json.gz"))
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", MM, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", MM, "--until", "15:30"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", MM, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:15 AM entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("   3469.60 |") and l.endswith("  target") for l in lines), text)
        self.assertTrue(any(l.startswith("   3384.40 |") and l.endswith("  stop") for l in lines), text)
        self.assertTrue(any(l.startswith("   3427.00 |") and l.endswith("  entry") for l in lines), text)
        self.assertIn("10:15", text)
        self.assertIn("3:00", text)
        self.assertEqual(lines[-2:], ["Close to a coin flip: target first 16.1 % vs stop first 18.7 %.",
                                      "65.2 % of the replays hit neither by 3:00 PM; the price at 3:00 PM decides those."])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", MM, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
