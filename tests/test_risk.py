import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
TSLA_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
V1_ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reel.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
V1_TODAY = "2026-10-13"
V1_NOW = "2026-10-13 10:15"  # a Tuesday in the session: a plain day order needs the market open
PRICES = {"AAPL": 341.27, "AXP": 309.84, "GOOGL": 353.78, "KO": 90.11, "BAC": 53.96}
QUOTE_TIME = datetime.datetime(2026, 10, 19, 14, 52, 13)   # Yahoo's TSLA quote 366.475, 10:52:13 New York
# v1's rules with every day-trade check switched off: the long-term checks still hold
V1 = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_usd": 2000, "min_order_usd": 5,
      "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None, "no_entries_after": None,
      "max_trades_per_day": 0, "max_daily_loss_usd": 0, "max_loss_per_trade_usd": 0, "min_reward_risk": 0}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def tsla(**over):
    with open(TSLA_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop,
            "plan": {"entry": entry, "stop": stop, "target": target}}


def v1_orders():
    with open(V1_ORDERS_PATH) as fh:
        return json.load(fh)["orders"]


def buy(sym, usd):
    return {"symbol": sym, "side": "buy", "notional": str(usd), "type": "market", "time_in_force": "day"}


def placed(sym, side="buy", day=TODAY, max_loss=46.88, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T10:20:00-04:00"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheReel(unittest.TestCase):
    def test_the_tsla_bracket_passes_every_check(self):
        res = check.check([tsla()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 364.18, 358.32, 370.04))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["reward_risk"]), (5.86, 46.88, 1.0))
        self.assertEqual(r["value_usd"], 2913.44)
        self.assertEqual(res["cash_after"], 2086.56)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_nine_shares_is_blocked_by_the_max_loss_only(self):
        res = check.check([tsla(qty="9")], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 9 shares x 5.86 risk a share = $52.74 at the stop, over your $50 max loss per trade"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = tsla()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty="9")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss_stop_price="358.00")), check.fingerprint(o))
        self.assertEqual(check.ALPACA_FIELDS, ticket.ALPACA_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", TSLA_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy TSLA 8 shares, entry", text)
            self.assertIn("entry 364.18 (the plan's entry), stop 358.32, target 370.04: risk 5.86 a share", text)
            self.assertIn("max loss $46.88 (limit $50), reward:risk 1.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-19 10:15 New York", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(tsla()))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = tsla()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 364.18}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = tsla()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price"):
            o.pop(k)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 46.88)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 364.18", reasons(self.one(tsla(stop_loss_stop_price="365.00"))))
        res = self.one(tsla(take_profit_limit_price="364.00"))
        self.assertIn("target: the target 364.00 must be above the entry 364.18", reasons(res))

    def test_reward_risk(self):
        res = self.one(tsla(take_profit_limit_price="368.00"))
        self.assertIn("reward:risk: 0.65", reasons(res))
        self.assertEqual(verdicts(self.one(tsla(take_profit_limit_price="368.00"), min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        later = at("2026-10-19 10:55")
        res = check.check([tsla()], cfg(), prices={"TSLA": 366.475}, now=later, price_times={"TSLA": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (366.48, "price", 8.16))
        self.assertIn("loss: 8 shares x 8.16 risk a share = $65.28", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-10-19 11:30", "2026-10-19 10:15"):
            res = check.check([tsla()], cfg(), prices={"TSLA": 366.475}, now=at(now),
                              price_times={"TSLA": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 364.18)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([tsla()], cfg(), prices={"TSLA": 360.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 364.18)

    def test_a_limit_order_uses_its_limit_price(self):
        o = tsla(type="limit", limit_price="364.00")
        res = check.check([o], cfg(), prices={"TSLA": 366.475}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["verdict"]), (364.0, 45.44, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("AAPL"), placed("AMD")]}
        res = check.check([tsla()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, $93.76 of $150 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("AAPL"), placed("AMD"), placed("NVDA")]}
        res = check.check([tsla()], cfg(), state=three, now=at(NOW))
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: $140.64 already at risk today plus $46.88 for this one = $187.52, over your $150",
                      why)
        yesterday = {"orders": [placed(s, day="2026-10-16") for s in ("AAPL", "AMD", "NVDA")]}
        self.assertEqual(verdicts(check.check([tsla()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("AAPL"), placed("AAPL", side="sell", kind="exit", max_loss=None),
                         placed("AMD", status="declined"), placed("NVDA", status="failed")]}
        res = check.check([tsla()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [bracket(s, 1, 50.0, 10.0, 90.0) for s in ("AAA", "BBB", "CCC", "DDD")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: $120 already at risk today plus $40 for this one = $160", why)

    def test_entry_window_new_york_time(self):
        cases = [("2026-10-19 09:44", "no new entries before 09:45 New York (it is 09:44)"),
                 ("2026-10-19 15:31", "no new entries after 15:30 New York (it is 15:31)"),
                 ("2026-10-19 08:00", "the market is closed (Mon 2026-10-19 08:00"),
                 ("2026-10-19 16:05", "the market is closed"),
                 ("2026-10-17 10:15", "the market is closed today (weekend"),
                 ("2026-11-26 10:15", "the market is closed today (NYSE holiday: Thanksgiving Day"),
                 ("2026-11-27 12:31", "no new entries after 12:30 New York"),
                 ("2026-11-27 13:05", "the market is closed"),
                 ("2026-11-02T14:40:00Z", "no new entries before 09:45 New York (it is 09:40)")]
        for now, want in cases:
            res = self.one(tsla(), now=now)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-10-19 09:45", "2026-10-19 15:30", "2026-11-27 12:30", "2026-11-02T15:15:00Z",
                    "2026-10-30T14:40:00Z", "2026-03-09T13:50:00Z"):
            self.assertEqual(verdicts(self.one(tsla(), now=now)), ["PASS"], now)
        self.assertIn("early close", " ".join(self.one(tsla(), now="2026-11-27 12:30")["warnings"]))
        # the window off lifts no_entries_* only: a plain day entry still needs the market open (owner, R4)
        off = dict(no_entries_before=None, no_entries_after=None)
        for now, want in (("2026-10-17 10:15", "window: the market is closed today (weekend, Sat 2026-10-17 10:15"),
                          ("2026-10-19 16:05", "window: the market is closed (Mon 2026-10-19 16:05 New York"),
                          ("2026-10-19 09:29", "window: the market is closed (Mon 2026-10-19 09:29 New York"),
                          ("2026-11-27 13:00", "window: the market is closed (Fri 2026-11-27 13:00 New York"),
                          ("2026-11-26 10:15", "window: the market is closed today (NYSE holiday")):
            res = self.one(tsla(), now=now, **off)
            self.assertEqual(verdicts(res), ["BLOCK"], now)
            self.assertIn(want, reasons(res), now)
        self.assertEqual(verdicts(self.one(tsla(), now="2026-10-19 09:30", **off)), ["PASS"])
        self.assertEqual(verdicts(self.one(tsla(), now="2026-10-19 15:59", **off)), ["PASS"])
        # extended hours and gtc keep today's behaviour
        self.assertEqual(verdicts(self.one(tsla(time_in_force="gtc"), now="2026-10-17 10:15", **off)), ["PASS"])
        ext = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "limit_price": "90",
               "time_in_force": "day", "extended_hours": True}
        res = check.check([ext], v1(), prices=PRICES, now=at("2026-10-19 17:30"))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIsNone(res["results"][0]["entries_until"])
        # Fable R6: only a real true is extended hours; "false", 1, "true" fail closed
        for bad in ("false", "true", 1, 0, "yes"):
            res = check.check([dict(ext, extended_hours=bad)], v1(), prices=PRICES, now=at("2026-10-19 17:30"))
            self.assertEqual(verdicts(res), ["BLOCK"], bad)
            self.assertIn("format: extended_hours must be true or false, got %r" % (bad,), reasons(res))
            self.assertEqual(res["results"][0]["entries_until"], "15:59", bad)  # treated as plain day, not extended
        res = check.check([dict(ext, extended_hours=False)], v1(), prices=PRICES, now=at("2026-10-19 17:30"))
        self.assertIn("window: the market is closed", reasons(res))  # false is a plain day entry
        # Fable R7: with the window off a plain day entry still leans on the holiday list, so it warns
        cal = "the holiday list covers 2026 and 2027 only: check nyse.com for 2028-03-15"
        res = check.check([tsla()], cfg(**off), now=at("2028-03-15 10:15"))
        self.assertIn(cal, res["warnings"])
        self.assertEqual(res["warnings"].count(cal), 1)
        res = check.check([tsla(), tsla(symbol="AMD")], cfg(**off), now=at("2028-03-15 10:15"))
        self.assertEqual(res["warnings"].count(cal), 1)  # once, not per order
        res = check.check([tsla(time_in_force="gtc")], cfg(**off), now=at("2028-03-15 10:15"))
        self.assertNotIn(cal, res["warnings"])  # a gtc entry does not use the list
        with self.assertRaises(check.RiskError):
            self.one(tsla(), no_entries_before="9.45am")

    def test_entries_until_is_the_last_minute_for_a_yes(self):
        cases = [("2026-10-19 10:15", {}, "15:30"),
                 ("2026-11-27 10:15", {}, "12:30"),                                   # early close
                 ("2026-10-19 10:15", {"no_entries_after": None}, "15:59"),
                 ("2026-11-27 10:15", {"no_entries_after": None}, "12:59"),
                 ("2026-10-19 10:15", {"no_entries_after": "16:30"}, "15:59"),         # never past the close
                 ("2026-10-17 10:15", {}, ""),                                        # weekend
                 ("2026-11-26 10:15", {}, ""),                                        # holiday
                 ("2026-10-19 10:15", {"no_entries_before": None, "no_entries_after": None}, "15:59"),         # window off: a plain day entry ends at the close
                 ("2026-11-27 10:15", {"no_entries_before": None, "no_entries_after": None}, "12:59"),
                 ("2026-10-17 10:15", {"no_entries_before": None, "no_entries_after": None}, "")]
        for now, over, want in cases:
            res = check.check([tsla()], cfg(**over), now=at(now))
            self.assertEqual(res["entries_until"], want, (now, over))
            self.assertEqual(res["results"][0]["entries_until"], want, (now, over))
        # each result carries its own: gtc and extended hours have none with the window off; an exit has none
        off = {"no_entries_before": None, "no_entries_after": None}
        res = check.check([tsla(time_in_force="gtc"), tsla(time_in_force="day", extended_hours=True)],
                          cfg(**off), now=at("2026-10-19 10:15"))
        self.assertEqual([r["entries_until"] for r in res["results"]], [None, None])
        res = check.check([tsla(time_in_force="gtc")], cfg(), now=at("2026-10-19 10:15"))
        self.assertEqual(res["results"][0]["entries_until"], "15:30")  # the window on: it binds gtc too
        sell = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}
        pf = {"cash": 0, "positions": [{"ticker": "TSLA", "shares": 8}]}
        res = check.check([sell], cfg(**off), portfolio=pf, now=at("2026-10-19 16:30"))
        self.assertEqual((verdicts(res), res["results"][0]["entries_until"]), (["PASS"], None))

    def test_the_window_uses_new_york_time_not_the_machine(self):
        self.assertEqual(check.to_eastern(at("2026-10-19 10:15")), datetime.datetime(2026, 10, 19, 10, 15))
        self.assertEqual(at("2026-10-19 10:15"), datetime.datetime(2026, 10, 19, 14, 15))
        self.assertEqual(at("2026-12-01 10:15"), datetime.datetime(2026, 12, 1, 15, 15))

    def test_pdt_guard(self):
        def trade(day):
            return [placed("X" + day[-2:], day=day), placed("X" + day[-2:], side="sell", day=day, kind="exit",
                                                            status="filled", max_loss=None)]
        st = {"orders": trade("2026-10-13") + trade("2026-10-14") + trade("2026-10-15")}
        self.assertEqual(verdicts(self.one(tsla())), ["PASS"])
        res = check.check([tsla()], cfg(pdt_guard=True), state=st, now=at(NOW))
        self.assertIn("pdt: 3 day trades in the last 5 business days", reasons(res))
        self.assertEqual(verdicts(check.check([tsla()], cfg(), state=st, now=at(NOW))), ["PASS"])
        old = {"orders": trade("2026-10-12") + trade("2026-10-14") + trade("2026-10-15")}
        self.assertEqual(verdicts(check.check([tsla()], cfg(pdt_guard=True), state=old, now=at(NOW))), ["PASS"])
        pending = {"orders": trade("2026-10-14") + trade("2026-10-15") + [placed("AMD")]}
        res = check.check([tsla()], cfg(pdt_guard=True), state=pending, now=at(NOW))
        self.assertIn("pdt: 3 day trades", reasons(res))
        buy_only = {"orders": [placed("AMD", day="2026-10-15"), placed("NVDA", day="2026-10-16")]}
        self.assertEqual(check.day_trades(buy_only, TODAY)[0], 0)

    def test_a_short_entry_when_allowed(self):
        short = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day",
                 "order_class": "bracket", "take_profit_limit_price": "358.32", "stop_loss_stop_price": "370.04",
                 "plan": {"entry": 364.18}}
        res = self.one(short)
        self.assertIn("short: selling more TSLA than you hold", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 46.88, 1.0))
        res = self.one(dict(short, stop_loss_stop_price="360.00"), allow_short=True)
        self.assertIn("must be above the entry 364.18 for a sell", reasons(res))

    def test_bracket_format(self):
        self.assertIn("whole-share qty", reasons(self.one(dict(tsla(qty=None), notional="2900"))))
        self.assertIn("whole shares only", reasons(self.one(tsla(qty="8.5"))))
        self.assertIn("time_in_force day or gtc", reasons(self.one(tsla(time_in_force="ioc"))))
        self.assertEqual(verdicts(self.one(tsla(time_in_force="gtc"))), ["PASS"])
        o = tsla()
        o.pop("take_profit_limit_price")
        self.assertIn("needs both take_profit_limit_price and stop_loss_stop_price", reasons(self.one(o)))
        self.assertIn("needs a stop_loss_stop_price", reasons(self.one(dict(tsla(), stop_loss_stop_price=None,
                                                                            stop_loss_limit_price="357.00"))))
        self.assertIn("as a string", warnings(self.one(tsla(stop_loss_stop_price=358.32))))

    def test_extended_hours(self):
        lim = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "limit_price": "90",
               "time_in_force": "gtc", "extended_hours": True}
        res = check.check([lim], v1(), prices=PRICES, today=V1_TODAY, now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        res = check.check([dict(lim, type="market", limit_price=None)], v1(), prices=PRICES, today=V1_TODAY, now=at(V1_NOW))
        self.assertIn("extended hours needs a limit order", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 2086.56, "positions": [{"ticker": "TSLA", "shares": 8,
                                           "lots": [{"date": TODAY, "shares": 8, "cost": 2913.44}]}]}
    SELL = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("TSLA"), placed("AMD"), placed("NVDA")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("TSLA", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["TSLA"], max_order_usd=100, max_daily_loss_usd=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-10-19 15:58"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("price", why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-10-17 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty="4")
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"TSLA": 366.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"TSLA": 366.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, type="limit")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, qty="9")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more TSLA than you hold (8 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([tsla(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class V1Checks(unittest.TestCase):
    """v1's long-term checks still hold (day-trade checks off)."""

    def one(self, order, **over):
        return check.check([order], v1(**over), prices=PRICES, today=V1_TODAY, now=at(V1_NOW))["results"][0]

    def test_the_five_buys_pass_on_a_fresh_5000_paper_account(self):
        res = check.check(v1_orders(), v1(), today=V1_TODAY, now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["PASS"] * 5)
        self.assertEqual(res["cash_after"], 500)
        self.assertEqual(res["results"][0]["value_usd"], 1250)
        self.assertEqual(res["results"][0]["amount"], "$1,250")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(v1_orders() + [buy("AAPL", 100)], v1(), today=V1_TODAY, now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_v2_config_blocks_buys_without_a_stop(self):
        res = check.check(v1_orders(), cfg(), now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your $2,000 max order", " ".join(self.one(buy("KO", 2500), max_position_pct=100)["reasons"]))
        self.assertIn("under your $5 minimum", " ".join(self.one(buy("KO", 2))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("KO", 100), blocked=["KO"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_options_off_unless_allowed(self):
        opt = {"symbol": "BE270617C00100000", "side": "buy", "qty": "1", "type": "limit",
               "limit_price": "12.50", "time_in_force": "day"}
        r = self.one(opt)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("option", " ".join(r["reasons"]))
        r = self.one(opt, allow_options=True)
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(r["warnings"])

    def test_crypto_and_junk_symbols(self):
        crypto = {"symbol": "BTC/USD", "side": "buy", "notional": "50", "type": "market", "time_in_force": "day"}
        self.assertIn("stocks and ETFs only", " ".join(self.one(crypto)["reasons"]))

    def test_format_rules(self):
        both = dict(buy("KO", 100), qty="1")
        self.assertIn("exactly one of qty or notional", " ".join(self.one(both)["reasons"]))
        lim = dict(buy("KO", 100), type="limit", limit_price="90")
        self.assertIn("market order with time_in_force day", " ".join(self.one(lim)["reasons"]))
        nolimit = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day"}
        self.assertIn("needs a limit_price", " ".join(self.one(nolimit)["reasons"]))
        bad = {"symbol": "KO", "side": "hold", "notional": "100"}
        self.assertIn("side must be buy or sell", " ".join(self.one(bad)["reasons"]))

    def test_numbers_should_be_strings(self):
        r = self.one({"symbol": "KO", "side": "buy", "notional": 100, "type": "market", "time_in_force": "day"})
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("as a string", r["warnings"][0])

    def test_share_orders_need_a_price(self):
        q = {"symbol": "ZZZZ", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}
        self.assertIn("no price for ZZZZ", " ".join(self.one(q)["reasons"]))
        q["symbol"] = "KO"
        r = self.one(q)
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_usd"], 180.22)

    def test_margin(self):
        r = check.check([buy("KO", 1500)], v1(cash_buffer_pct=0, max_position_pct=100),
                        portfolio={"cash": 1000, "positions": []}, today=V1_TODAY, now=at(V1_NOW))["results"][0]
        self.assertIn("margin", " ".join(r["reasons"]))

    PF = {"cash": 500.0, "positions": [{"ticker": "KO", "shares": 7.8571,
                                         "lots": [{"date": "2026-10-13", "shares": 7.8571, "cost": 708.0}]}]}

    def test_short_sales_are_blocked(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=V1_TODAY, now=at(V1_NOW))["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = "3"
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=V1_TODAY, now=at(V1_NOW))["results"][0]
        self.assertEqual(r["verdict"], "PASS")

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [{"symbol": "KO", "side": "sell", "qty": "5", "type": "market", "time_in_force": "day"},
                  buy("BAC", 450)]
        res = check.check(orders, v1(max_position_pct=100), portfolio=self.PF, prices=PRICES, today=V1_TODAY, now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("BAC", 450)], v1(max_position_pct=100), portfolio=self.PF, prices=PRICES,
                          today=V1_TODAY, now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d" % i, "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}
                         for i in range(10)]}
        res = check.check([buy("KO", 100)], v1(), state=st, today=V1_TODAY, now=at(V1_NOW))
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("KO", 100)], v1(), state=st, today="2026-10-14", now=at(V1_NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        ko = {"orders": [{"symbol": "KO", "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}]}
        res = check.check([buy("KO", 100)], v1(), state=ko, today=V1_TODAY, now=at(V1_NOW))
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "KO", "side": "buy", "status": "declined", "at": V1_TODAY + "T10:00:00"}]}
        self.assertEqual(verdicts(check.check([buy("KO", 100)], v1(), state=declined, today=V1_TODAY, now=at(V1_NOW))), ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("KO", 100)], v1(paper=False), today=V1_TODAY, now=at(V1_NOW))
        self.assertIn("LIVE", res["warnings"][0])


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-19T14:55:00Z", "prices": {"TSLA": 366.475},
                           "detail": {"TSLA": {"time": "2026-10-19T14:52:13Z", "fetched": "2026-10-19T14:55:00Z"}}},
                          fh)
            none = os.path.join(d, "none")
            base = ["--orders", TSLA_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 10:55"]), 1)
            self.assertIn("8 shares x 8.16 risk a share = $65.28", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-19 11:30"]), 0)

    def test_v1_orders_with_a_v1_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(v1(), fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", V1_ORDERS_PATH, "--config", c, "--portfolio", none, "--prices", none,
                                   "--state", none, "--today", V1_TODAY, "--now", V1_NOW, "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], V1_TODAY)
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))
            bad = os.path.join(d, "orders.json")
            with open(bad, "w") as fh:
                json.dump({"orders": v1_orders() + [buy("AAPL", 100)]}, fh)
            with redirect_stdout(io.StringIO()):
                code = check.main(["--orders", bad, "--config", c, "--portfolio", none, "--prices", none,
                                   "--state", none, "--today", V1_TODAY, "--now", V1_NOW, "--no-write"])
            self.assertEqual(code, 1)

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", TSLA_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", TSLA_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())


if __name__ == "__main__":
    unittest.main()
