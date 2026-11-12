"""trader-exit tests. Bars for the ATR stop reuse tests/fixtures/trader-entry/
(M&M's real Nov 11 2026 five-minute bars on NSE, see test_entry.py). M&M's NSE
tick is 0.1 (Kite's instrument list, tests/fixtures/trader-scanner/nse-lists-2026-11-12)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

MM = os.path.join(FIX, "trader-entry", "MM-2026-11-11-5m.json")
R = "₹"
UNIVERSE_LINE = "M&M|Mahindra & Mahindra Limited|0.1|FO\nTCS|Tata Consultancy Services Limited|0.1|FO\n"

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "M&M", "side": "buy", "date": "2026-11-11", "setup": "orb",
             "entry": 3427.0, "entry_time": "10:15 AM", "entry_bar": 11, "interval": "5m",
             "opening_range": {"high": 3421.1, "low": 3347.7, "minutes": 15},
             "stop": 3384.4, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 3469.6, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 42.6, "reward_risk": 1.0, "flat_by": "15:00", "tick": 0.1, "fo": True,
             "exchange": "NSE"}
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "M&M", "setup": "orb", "side": "buy", "date": "2026-11-11",
              "status": "triggered", "time": "10:15 AM", "bar_start": "10:10", "bar_index": 11, "price": 3427.0,
              "level": 3421.1, "interval": "5m", "opening_range": {"high": 3421.1, "low": 3347.7, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-11-11T04:45:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=3421.1, ol=3347.7, atr=None, n=None, tick="0.1"):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, Decimal(tick))


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(3427, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("3384.40"))          # (3421.10 + 3347.70) / 2, already on the tick
        self.assertEqual(p["risk"], Decimal("42.60"))
        self.assertEqual(p["target"], Decimal("3469.60"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertNotIn("rounded down", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(3427, "or-mid")
        self.assertEqual(p["target"], Decimal("3512.20"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(3427.0, "or-mid", "1R", 3421.10009765625, 3347.699951171875)
        self.assertEqual((p["entry"], p["stop"], p["target"]),
                         (Decimal("3427.00"), Decimal("3384.40"), Decimal("3469.60")))

    def test_other_stops(self):
        p = c(3427, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("3347.70"), Decimal("79.30"),
                                                               Decimal("3585.60")))
        p = c(3427, "price:3384.49")
        self.assertEqual(p["stop"], Decimal("3384.40"))             # down to the 0.1 tick, away from the entry
        self.assertIn("rounded down to the 0.1 tick", p["stop_detail"])
        p = c(3427, "pct:1")                                        # 3,392.73 -> 3,392.70
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("3392.70"), Decimal("34.30"),
                                                               Decimal("3495.60")))
        p = c(3427, "atr:1.5", atr=16.300008, n=12)                 # 3427 - 24.450012 = 3402.549988
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("3402.50"), Decimal("24.50"),
                                                               Decimal("3476.00")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(12) under the entry")
        self.assertEqual(c(3427, "price:₹3,384.40")["stop"], Decimal("3384.40"))   # a rupee sign is fine

    def test_every_price_lands_on_the_tick(self):
        p = c(100, "price:99.03", "1.33R", None, None, tick="0.05")
        self.assertEqual(p["stop"], Decimal("99.00"))               # floor to 0.05
        self.assertEqual(p["target"], Decimal("101.35"))            # 101.33 half up to 0.05
        p = c(100.02, "price:99", "1R", None, None, tick="0.05")
        self.assertEqual(p["entry"], Decimal("100.00"))             # the entry LIMIT is on the tick too
        p = c(11040, "pct:1", "2R", None, None, tick="1")            # BAJAJ-AUTO ticks at 1 rupee
        self.assertEqual((p["stop"], p["target"]), (Decimal("10929.00"), Decimal("11262.00")))
        with self.assertRaises(exitp.PlanError):
            c(100, "pct:1", tick="0")

    def test_target_rounds_half_up_and_price_targets(self):
        p = c(100, "price:99.01", "1.5R", None, None, tick="0.01")
        self.assertEqual(p["target"], Decimal("101.49"))            # 100 + 1.485
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        p = c(3427, "or-mid", "price:3470")
        self.assertEqual(p["reward_risk"], Decimal("1.01"))         # 43.00 / 42.60
        self.assertEqual(p["target_label"], "your price (1.01R)")
        self.assertEqual(p["target_r"], Decimal("1.01"))

    def test_refusals_say_why(self):
        cases = [("price:3427", "2R", "at or above the entry"),
                 ("price:3430", "2R", "at or above the entry"),
                 ("or-mid", "price:3427", "at or below the entry"),
                 ("price:3425.4", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(3427, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 3,427 is 1.7135: a 1.80 risk is allowed, 1.60 is not
        self.assertEqual(c(3427, "price:3425.2")["risk"], Decimal("1.80"))
        with self.assertRaises(exitp.PlanError):
            c(3380, "or-mid")                                       # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(3427, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(3427, "or-mid", oh=3347.7, ol=3421.1)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("M&M", "5m", None, MM)
        day = exitp.datetime.date(2026, 11, 11)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=11)
        self.assertEqual(len(bars), 12)                             # 9:15 ... the 10:10 entry bar
        # by hand: the true range of each bar, the first one without the overnight gap
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 3421.1 - 3347.7, places=3)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 12)
        self.assertAlmostEqual(atr, sum(trs) / 12, places=9)
        self.assertAlmostEqual(atr, 16.3000, places=4)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 73)   # read as of 15:29:54: the 15:25 bar is open; the null 15:20 bar never counts
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        with open(os.path.join("trader", "universe.txt"), "w", encoding="utf-8") as fh:
            fh.write(UNIVERSE_LINE)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-11-11 10:16"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "M&M", "--offline", MM, "--now", "2026-11-11 10:15", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          %s3,384.40  opening-range midpoint" % R, text)
        self.assertIn("Target        %s3,469.60  1R (one times the risk)" % R, text)
        self.assertIn("Risk a share  %s42.60 (1.24%% of the entry)" % R, text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:00 IST", text)
        self.assertIn("Tick          0.1, F&O list (Zerodha squares off at 3:12 PM)", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 3512.2)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-11 10:20"])
        self.assertEqual(code, 0)
        self.assertIn(R + "3,512.20", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-12 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "m&m", "--entry", "3427", "--or-high", "3421.1", "--or-low", "3347.7",
                                  "--time", "10:15", "--entry-bar", "11", "--date", "2026-11-11", "--target", "1R",
                                  "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_the_tick_comes_from_the_universe_or_the_flag(self):
        os.remove(os.path.join("trader", "universe.txt"))
        code, _, err = self.plan("--symbol", "M&M", "--entry", "3427", "--or-high", "3421.1", "--or-low", "3347.7")
        self.assertEqual(code, 1)
        self.assertIn("no tick size for M&M", err)
        self.assertIn("scan.py universe", err)
        code, text, _ = self.plan("--symbol", "M&M", "--entry", "3427", "--or-high", "3421.1", "--or-low", "3347.7",
                                  "--tick", "0.05", "--json")
        p = json.loads(text)
        self.assertEqual((p["tick"], p["fo"], p["stop"]), (0.05, None, 3384.4))
        code, _, err = self.plan("--symbol", "INFY", "--entry", "1155", "--stop", "pct:1",
                                 "--universe", os.path.join(self.d, "nope.txt"))
        self.assertIn("no tick size for INFY", err)

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", MM, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (3402.5, 24.5, 3476.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 12))
        self.assertAlmostEqual(p["atr"], 16.3, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", MM, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("M&M is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "3421.2", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (3421.2, 3384.4, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=3420.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "M&M")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "M&M", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "M&M", "--entry", "3427", "--or-high", "3421.1")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_is_1500_or_earlier(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "14:45", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "14:45")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        for bad in ({"flat_by": "15:30"}, {"flat_by": "15:01"}):
            self.write(os.path.join("trader", "config.json"), bad)
            code, _, err = self.plan()
            self.assertEqual(code, 1)
            self.assertIn("later than 15:00", err)
        self.write(os.path.join("trader", "config.json"), {"flat_by": None})
        code, _, err = self.plan()
        self.assertIn("cannot be switched off", err)
        os.remove(os.path.join("trader", "config.json"))
        code, _, err = self.plan("--flat-by", "15:25")
        self.assertEqual(code, 1)
        self.assertIn("--flat-by 15:25 is later than 15:00", err)
        self.assertEqual(json.loads(self.plan("--flat-by", "14:00", "--json")[1])["flat_by"], "14:00")
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="2:58 PM"))
        self.assertEqual(self.plan()[0], 0)                          # two minutes to hold it
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="3:00 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", R + "3,384.40", R + "3,469.60",
                     R + "42.60", "3,512.20", "too tight to survive the spread", "tick", "15:00"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york"):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\s?\d")


if __name__ == "__main__":
    unittest.main()
