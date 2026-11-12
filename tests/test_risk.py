import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-mm.json")
NOW = "2026-11-11 10:16"
TODAY = "2026-11-11"
R = "₹"


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def pair(qty=None, **over):
    """The reel's two Kite orders (entry, stop) from trader-budget, optionally changed."""
    with open(ORDERS, encoding="utf-8") as fh:
        e, s = json.load(fh)["orders"]
    if qty is not None:
        e["quantity"] = s["quantity"] = qty
    e.update(over)
    return [e, s]


def prices(price=3428.5, at="2026-11-11T04:45:30Z", source="yahoo"):
    doc = {"prices": {"M&M": price}, "detail": {"M&M": {"source": source}}}
    if at:
        doc["detail"]["M&M"]["time"] = at
    return check.load_prices(doc)


def at(when):
    return check.parse_now(when)


def go(orders=None, c=None, portfolio=None, p=None, state=None, now=NOW):
    return check.check(orders if orders is not None else pair(), c if c is not None else cfg(), portfolio,
                       p if p is not None else prices(), state, None, at(now))


def placed(*entries):
    """trader/state/broker.json with entries placed today: (symbol, max_loss)."""
    return {"orders": [{"status": "placed", "date": TODAY, "kind": "entry", "symbol": s, "transaction_type": "BUY",
                        "max_loss": ml} for s, ml in entries]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_the_mm_pair_passes_every_check(self):
        res = go()
        self.assertEqual((res["pass"], res["block"]), (2, 0), res)
        e, s = res["results"]
        self.assertEqual((e["kind"], e["leg"], s["kind"], s["leg"]), ("entry", "entry", "exit", "stop"))
        self.assertEqual((e["risk_per_share"], e["charges_est"], e["max_loss"], e["reward_risk"]),
                         (59.6, 85.56, 1992.76, 1.0))
        self.assertEqual((e["stop"], e["target"], e["value_inr"]), (3384.4, 3469.6, 109664.0))
        self.assertEqual((res["entries_until"], res["now_india"], res["dry_run"]), ("15:00", "2026-11-11 10:16", True))
        self.assertIn("last price %s3,428.50 (yahoo, 30 s old)" % R, e["info"])
        self.assertEqual(s["reasons"], [])

    def test_33_shares_is_blocked_by_the_max_loss_only(self):
        e = go(pair(33))["results"][0]
        self.assertEqual(e["reasons"], ["loss: 33 shares x %s59.60 (entry 3427.00 to the stop's limit 3367.40) + "
                                        "%s86.76 charges = %s2,053.56, over your %s2,000.00 max loss per trade"
                                        % (R, R, R, R)])

    def test_fingerprint_covers_the_kite_fields_only(self):
        e, _ = pair()
        f = check.fingerprint(e)
        self.assertEqual(check.fingerprint(dict(e, why="other words", plan={})), f)
        self.assertNotEqual(check.fingerprint(dict(e, price=3427.1)), f)
        self.assertNotEqual(check.fingerprint(dict(e, quantity=31)), f)
        self.assertNotIn("iceberg_legs", check.KITE_FIELDS)


class TheWindow(unittest.TestCase):
    def test_0930_to_1500_india_time(self):
        self.assertEqual(self.reasons_fresh("09:29"), ["window: no orders before 09:30 India time (it is 09:29)"])
        self.assertEqual(self.reasons_fresh("09:30"), [])
        self.assertEqual(self.reasons_fresh("14:59"), [])
        self.assertIn("window: no new entries from 15:00 India time (it is 15:00): the owner's rule",
                      self.reasons_fresh("15:00"))

    def reasons_fresh(self, hm):
        now = at("2026-11-11 " + hm)
        stamp = (now).strftime("%Y-%m-%dT%H:%M:%SZ")
        return check.check(pair(), cfg(), None, prices(at=stamp), None, None, now)["results"][0]["reasons"]

    def test_a_config_can_only_tighten_it(self):
        now = at("2026-11-11 14:00")
        res = lambda c, t=now: check.check(pair(), c, None, prices(at=t.strftime("%Y-%m-%dT%H:%M:%SZ")), None,
                                           None, t)["results"][0]["reasons"]
        self.assertEqual(res(cfg(no_entries_after="14:00")), [])               # 14:00 itself is allowed
        self.assertIn("window: no new entries from 14:01 India time (it is 14:01)",
                      res(cfg(no_entries_after="14:00"), at("2026-11-11 14:01")))
        self.assertEqual(res(cfg(no_entries_before="10:00"), at("2026-11-11 09:45")),
                         ["window: no orders before 10:00 India time (it is 09:45)"])
        self.assertEqual(res(cfg(no_entries_before="10:00"), at("2026-11-11 10:00")), [])

    def test_a_looser_or_switched_off_window_is_a_config_block(self):
        for over, words in (({"no_entries_before": "09:15"}, "no_entries_before 09:15 is earlier than 09:30"),
                            ({"no_entries_after": "15:30"}, "no_entries_after 15:30 is later than 15:00"),
                            ({"no_entries_after": None}, "no_entries_after is switched off"),
                            ({"flat_by": "15:10"}, "flat_by 15:10 is later than 15:00"),
                            ({"flat_by": None}, "flat_by is switched off")):
            res = go(c=cfg(**over))
            self.assertEqual(res["block"], 2, over)                             # every order, the stop leg too
            self.assertTrue(any(words in r for r in res["results"][1]["reasons"]), (over, res["results"][1]))

    def test_no_session_days(self):
        for day, words in (("2026-11-14 10:16", "weekend"), ("2026-11-24 10:16", "NSE holiday: Prakash Gurpurb"),
                           ("2026-11-08 18:30", "NSE special session (no entries): Muhurat"),
                           ("2027-01-04 10:16", "no NSE holiday calendar for 2027")):
            now = at(day)
            r = check.check(pair(), cfg(), None, prices(at=now.strftime("%Y-%m-%dT%H:%M:%SZ")), None, None, now)
            self.assertTrue(any(words in x for x in r["results"][0]["reasons"]), (day, r["results"][0]["reasons"]))
            self.assertIsNone(r["entries_until"])


class Format(unittest.TestCase):
    def block(self, words, **over):
        e = go(pair(**over))["results"][0]
        self.assertTrue(any(words in r for r in e["reasons"]), (over, e["reasons"]))

    def test_the_kite_shape(self):
        self.block("variety 'amo'", variety="amo")
        self.block("exchange 'BSE'", exchange="BSE")
        self.block("product 'CNC': intraday (MIS) only", product="CNC")
        self.block("order_type 'MARKET': LIMIT and SL only", order_type="MARKET")
        self.block("order_type 'SL-M'", order_type="SL-M")
        self.block("validity 'TTL'", validity="TTL")
        self.block("validity 'IOC'", validity="IOC")
        self.block("quantity must be a whole number above 0", quantity=32.5)
        self.block("quantity must be a whole number above 0", quantity=0)
        self.block("price 3427.05 is off the 0.1 tick", price=3427.05)
        self.block("tag None: every order this pack places carries the tag 'traderpack'", tag=None)
        self.block("disclosed quantity, TTL and iceberg fields", disclosed_quantity=10)
        self.block("'M&M.NS' is not an NSE equity symbol", tradingsymbol="M&M.NS")
        self.block("transaction_type must be BUY or SELL", transaction_type="HOLD")

    def test_the_stop_leg_shape(self):
        e, s = pair()
        res = go([e, dict(s, price=3390.0)])
        self.assertTrue(any("an SL sell's limit 3390.00 is above its trigger 3384.40" in r
                            for r in res["results"][1]["reasons"]))
        res = go([e, dict(s, trigger_price=None)])
        self.assertTrue(any("an SL order needs a trigger_price" in r for r in res["results"][1]["reasons"]))
        self.assertIn("stop: the stop leg needs a trigger_price and a limit price", res["results"][0]["reasons"])
        res = go([e, dict(s, trigger_price=3384.45)])
        self.assertTrue(any("trigger_price 3384.45 is off the 0.1 tick" in r for r in res["results"][1]["reasons"]))

    def test_a_sell_with_nothing_held_is_a_short_sale(self):
        _, s = pair()
        r = go([s])["results"][0]
        self.assertEqual(r["kind"], "entry")
        self.assertTrue(any(x.startswith("short: selling 32 M&M with 0 held") for x in r["reasons"]), r["reasons"])

    def test_short_and_options_cannot_be_switched_on(self):
        for over, words in (({"allow_short": True}, "this pack is long only"),
                            ({"allow_options": True}, "options stay off")):
            self.assertTrue(any(words in r for r in go(c=cfg(**over))["results"][0]["reasons"]), over)


class Prices(unittest.TestCase):
    # R16(a): every price carries its source and age; no price, or one of unknown age, is no entry.
    def reasons(self, p):
        return go(p=p)["results"][0]["reasons"]

    def test_no_price_unknown_age_stale_and_future(self):
        self.assertTrue(any(r.startswith("price: no last price for M&M") for r in self.reasons({})))
        self.assertIn("price: the last price %s3,428.50 (yahoo, age unknown) has no time: age unknown, not used for "
                      "an entry" % R, self.reasons(prices(at=None)))
        self.assertIn("price: the last price %s3,428.50 (yahoo, 16 min old) is older than 15 minutes" % R,
                      self.reasons(prices(at="2026-11-11T04:30:00Z")))
        self.assertTrue(any(r.startswith("price: the last price") for r in
                            self.reasons(prices(at="2026-11-11T04:48:00Z"))))           # 2 minutes in the future
        self.assertEqual(self.reasons(prices(at="2026-11-11T04:31:00Z")), [])           # 15 minutes is fresh

    def test_the_source_is_named_and_the_time_falls_back(self):
        e = go(p=prices(source="kite_ltp"))["results"][0]
        self.assertIn("last price %s3,428.50 (kite_ltp, 30 s old)" % R, e["info"])
        p = check.load_prices({"as_of": "2026-11-11T04:45:00Z", "source": "yahoo", "prices": {"M&M": 3428.5}})
        self.assertIn("last price %s3,428.50 (yahoo, 1 min old)" % R, go(p=p)["results"][0]["info"])

    def test_a_price_above_the_limit_warns(self):
        e = go(p=prices(3440.0))["results"][0]
        self.assertEqual(e["verdict"], "PASS")
        self.assertTrue(any("above your limit %s3,427.00: the order may not fill" % R in w for w in e["warnings"]))


class Limits(unittest.TestCase):
    def reasons(self, **kw):
        return go(**kw)["results"][0]["reasons"]

    def test_the_stop_leg_is_required_and_must_match(self):
        e, s = pair()
        self.assertTrue(any(r.startswith("stop: no stop leg") for r in self.reasons(orders=[e])))
        self.assertEqual(go([e], c=cfg(require_stop=False))["block"], 0)
        r = go([e, dict(s, quantity=31)])["results"][0]["reasons"]
        self.assertIn("stop: the stop leg's quantity 31 is not the entry's 32", r)
        r = go([dict(e, price=3380.0), s])["results"][0]["reasons"]
        self.assertIn("stop: the stop 3384.40 must be below the entry 3380.00", r)

    def test_reward_risk_and_target(self):
        e, s = pair()
        e["plan"] = dict(e["plan"], target=3440.0)
        self.assertIn("reward:risk: 0.31 (target 3440.00, entry 3427.00, stop 3384.40) is under your minimum 1",
                      go([e, s])["results"][0]["reasons"])
        e["plan"] = dict(e["plan"], target=3420.0)
        self.assertIn("target: the target 3420.00 must be above the entry 3427.00", go([e, s])["results"][0]["reasons"])

    def test_daily_loss_and_trades_per_day(self):
        self.assertEqual(self.reasons(state=placed(("TCS", 2000), ("SBIN", 2000))), [])          # 5,992.76 of 6,000
        self.assertIn("today before this: 2 of 3 trades, %s4,000.00 of %s6,000.00 daily loss" % (R, R),
                      go(state=placed(("TCS", 2000), ("SBIN", 2000)))["results"][0]["info"])
        r = self.reasons(state=placed(("TCS", 2100), ("SBIN", 2000)))
        self.assertIn("daily loss: %s4,100.00 already at risk today plus %s1,992.76 for this one = %s6,092.76, over "
                      "your %s6,000.00 daily loss limit" % (R, R, R, R), r)
        r = self.reasons(state=placed(("TCS", 1), ("SBIN", 1), ("INFY", 1)))
        self.assertIn("trades: 3 entries placed today plus 0 passed earlier in this batch, your limit is 3 a day", r)
        r = self.reasons(state=placed(("M&M", 1)))
        self.assertIn("duplicate: a BUY M&M entry was already placed today", r)
        st = placed(("TCS", 1))
        st["orders"][0]["date"] = "2026-11-10"                                                  # yesterday's
        self.assertEqual(self.reasons(state=st), [])

    def test_a_second_pair_in_the_batch(self):
        res = go(pair() + pair())
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS", "BLOCK", "BLOCK"])
        self.assertIn("duplicate: a second BUY M&M in this batch", res["results"][2]["reasons"])
        # its stop has no entry behind it: it says which entry was blocked, rather than only "a short sale"
        self.assertIn("short: the entry this stop protects (#3) was blocked, so there is nothing for it to sell; "
                      "alone it would be a short sale", res["results"][3]["reasons"])

    def test_size_cash_position_and_blocked(self):
        self.assertTrue(any("over your %s1,00,000.00 max order" % R in r
                            for r in self.reasons(c=cfg(max_order_inr=100000))))
        self.assertTrue(any("under your %s2,00,000.00 minimum order" % R in r
                            for r in self.reasons(c=cfg(min_order_inr=200000))))
        self.assertTrue(any(r.startswith("position: M&M would be") for r in self.reasons(c=cfg(max_position_pct=50))))
        r = self.reasons(portfolio={"cash": 100000, "positions": []})
        self.assertIn("cash: needs %s1,09,664.00 but only %s1,00,000.00 cash; MIS leverage is not used "
                      "(\"allow_margin\": false)" % (R, R), r)
        self.assertIn("blocked: M&M is on your blocked list", self.reasons(c=cfg(blocked=["m&m"])))

    def test_daily_order_limit_counts_but_never_traps_the_stop(self):
        res = go(c=cfg(max_orders_per_day=1))
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS"])
        self.assertTrue(any(w.startswith("exit, not blocked: daily limit") for w in res["results"][1]["warnings"]))


class OldConfigs(unittest.TestCase):
    def test_old_us_keys_and_broker_are_config_blocks(self):
        for over, words in (({"paper": True}, "config: 'paper' is from the old US pack; use dry_run"),
                            ({"max_loss_per_trade_usd": 50},
                             "config: 'max_loss_per_trade_usd' is from the old US pack; use max_loss_per_trade_inr"),
                            ({"pdt_guard": False}, "config: 'pdt_guard' is from the old US pack"),
                            ({"broker": "alpaca"}, "config: broker 'alpaca': this pack trades on NSE through Zerodha")):
            res = go(c=cfg(**over))
            self.assertEqual(res["block"], 2, over)
            self.assertIn(words, " | ".join(res["results"][0]["reasons"]))
            self.assertTrue(any(words in w for w in res["warnings"]))

    def test_live_warns(self):
        self.assertTrue(any(w.startswith("LIVE (\"dry_run\": false)") for w in go(c=cfg(dry_run=False))["warnings"]))


class Exits(unittest.TestCase):
    HELD = {"cash": 100000, "positions": [{"tradingsymbol": "M&M", "quantity": 32, "average_price": 3427.0}]}

    def sell(self, **over):
        o = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "SELL",
             "quantity": 32, "product": "MIS", "order_type": "LIMIT", "price": 3400.0, "validity": "DAY",
             "tag": "traderpack", "tick": 0.1, "fo": True}
        o.update(over)
        return o

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        res = go([self.sell()], portfolio=self.HELD, state=placed(("A", 9000), ("B", 1), ("C", 1)),
                 now="2026-11-11 15:05", p={})
        r = res["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"))
        self.assertEqual(r["warnings"], [])                                    # 15:05 is inside its session

    def test_exit_session_by_the_f_and_o_flag(self):
        for fo, now, warned in ((True, "15:13", True), (False, "15:13", False), (False, "15:26", True),
                                (None, "15:13", True)):                    # unknown counts as F&O: 15:12
            r = go([self.sell(fo=fo)], portfolio=self.HELD, now="2026-11-11 " + now, p={})["results"][0]
            self.assertEqual(r["verdict"], "PASS")
            self.assertEqual(any("exit outside the session" in w for w in r["warnings"]), warned, (fo, now))

    def test_format_still_applies_to_a_sell(self):
        r = go([self.sell(product="CNC")], portfolio=self.HELD, p={})["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "BLOCK"))
        r = go([self.sell(quantity=40)], portfolio=self.HELD, p={})["results"][0]
        self.assertEqual(r["kind"], "entry")
        self.assertTrue(any(x.startswith("short: selling 40 M&M with 32 held") for x in r["reasons"]))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "prices.json")
        with open(self.p, "w") as fh:
            json.dump({"prices": {"M&M": 3428.5}, "detail": {"M&M": {"time": "2026-11-11T04:45:30Z",
                                                                     "source": "yahoo"}}}, fh)
        self.out = os.path.join(self.d, "risk.json")
        self.args = ["--orders", ORDERS, "--config", CFG, "--prices", self.p, "--portfolio", "none.json",
                     "--state", "none.json", "--now", NOW, "--out", self.out]

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_writes_the_verdicts_with_fingerprints(self):
        code, text, err = run(self.args)
        self.assertEqual(code, 0, err)
        self.assertIn("2 passed, 0 blocked at 2026-11-11 10:16 India time. Entries until 15:00.", text)
        self.assertIn("(dry run)", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["entries_until"], doc["now_india"], doc["orders_file"]), ("15:00", "2026-11-11 10:16",
                                                                                     ORDERS))
        self.assertEqual(len(doc["check_id"]), 16)
        with open(ORDERS, encoding="utf-8") as fh:
            orders = json.load(fh)["orders"]
        self.assertEqual([r["fingerprint"] for r in doc["results"]], [check.fingerprint(o) for o in orders])

    def test_blocked_exit_code_and_no_write(self):
        code, text, _ = run(self.args[:-4] + ["--now", "2026-11-11 15:00", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("Blocked orders do not go to the broker", text)
        self.assertFalse(os.path.exists(self.out))

    def test_errors_are_one_line(self):
        bad = os.path.join(self.d, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{")
        for argv, words in ((["--orders", bad], "is not valid JSON"), (["--orders", "nope.json"], "no orders file"),
                            (["--orders", ORDERS, "--now", "soon"], "--now wants a time like"),
                            (["--orders", ORDERS, "--today", "11/11"], "--today wants a date")):
            code, _, err = run(argv + ["--no-write", "--config", CFG])
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn(words, err)


if __name__ == "__main__":
    unittest.main()
