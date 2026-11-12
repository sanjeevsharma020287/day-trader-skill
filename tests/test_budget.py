import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-mm.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-mm.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
NOW = "2026-11-11 10:16"
TODAY = "2026-11-11"
R = "₹"
# SYNTHETIC prices and ticks for the weights examples (not market data)
PRICES = {"RELIANCE": 1500.0, "HDFCBANK": 1000.0, "INFY": 1600.0, "TCS": 3200.0, "SBIN": 950.0}
UNIVERSE = "".join("%s|%s|0.1|FO\n" % (s, s) for s in PRICES)
# A fresh last price for M&M, 30 seconds before NOW (10:15:30 India time is 04:45:30 UTC)
LAST = {"price": 3428.5, "source": "yahoo", "at": "2026-11-11T04:45:30Z", "age_s": 30}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price=3428.5, quote_time="2026-11-11T04:45:30Z", source="yahoo"):
    d = {"as_of": quote_time, "prices": {"M&M": price}}
    if quote_time:
        d["detail"] = {"M&M": {"time": quote_time, "source": source}}
    return d


class Charges(unittest.TestCase):
    # Worked by hand from the plan's F12 rates (brokerage min(0.03 %, Rs 20) a side, STT 0.025 % sell,
    # NSE 0.00307 %, SEBI Rs 10/crore, stamp 0.003 % buy, GST 18 % on brokerage + NSE + SEBI).
    def test_the_estimate(self):
        self.assertEqual(size.est_charges(32, 3427.0, 3367.4), 85.56)
        self.assertEqual(size.est_charges(33, 3427.0, 3367.4), 86.76)
        self.assertEqual(size.est_charges(1, 100, 99), 0.11)               # brokerage under the Rs 20 cap
        self.assertEqual(size.est_charges(1000, 3000, 2990), 1108.76)      # brokerage capped at Rs 20 a side

    def test_b0_provisional_is_one_named_set(self):
        self.assertEqual(size.B0_PROVISIONAL["tag"], "traderpack")
        self.assertEqual(size.B0_PROVISIONAL["entry_validity"], "DAY")
        self.assertEqual(size.B0_PROVISIONAL["stop_limit_band_pct"], 0.5)


class TheReel(unittest.TestCase):
    def test_config_example_is_the_contract(self):
        self.assertEqual(cfg(), dict(size.DEFAULTS, budget=200000))
        self.assertTrue(cfg()["dry_run"])

    def test_mm_is_32_shares_bound_by_the_max_loss(self):
        s = size.size_trade(plan(), cfg(), last=LAST, today=TODAY)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["entry"], s["stop"], s["stop_limit"], s["target"]), (3427.0, 3384.4, 3367.4, 3469.6))
        self.assertEqual((s["risk_per_share"], s["reward_risk"]), (59.6, 1.0))
        self.assertEqual((s["qty"], s["bound_by"], s["cost"]), (32, "max loss", 109664.0))
        self.assertEqual((s["charges_est"], s["max_loss"]), (85.56, 1992.76))   # 32 x 59.60 + 85.56
        shares = {l["name"]: l["shares"] for l in s["limits"]}
        self.assertEqual(shares, {"max loss": 32, "position cap": 43, "max order": 58, "cash": 52})

    def test_the_kite_pair(self):
        s = size.size_trade(plan(), cfg(), last=LAST, today=TODAY)
        e, st = size.kite_pair(s, "P1")
        self.assertEqual({k: e[k] for k in ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity",
                                            "product", "order_type", "price", "validity", "tag")},
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "BUY",
                          "quantity": 32, "product": "MIS", "order_type": "LIMIT", "price": 3427.0,
                          "validity": "DAY", "tag": "traderpack"})
        self.assertNotIn("trigger_price", e)
        self.assertEqual((st["transaction_type"], st["order_type"], st["quantity"], st["trigger_price"], st["price"],
                          st["product"]), ("SELL", "SL", 32, 3384.4, 3367.4, "MIS"))
        self.assertEqual((e["leg"], st["leg"], e["pair"], st["pair"]), ("entry", "stop", "P1", "P1"))
        self.assertEqual((e["tick"], e["fo"]), (0.1, True))
        self.assertEqual(e["price_source"]["last_source"], "yahoo")
        self.assertEqual(e["price_source"]["last_age_s"], 30)
        self.assertIsInstance(e["quantity"], int)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "orders.json")
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(), fh)
            code, _, err = run(["--plan", PLAN, "--config", CFG, "--prices", pp, "--portfolio", "none.json",
                                "--now", NOW, "--write", out])
            self.assertEqual(code, 0, err)
            with open(out, encoding="utf-8") as fh:
                got = json.load(fh)
            with open(ORDERS, encoding="utf-8") as fh:
                want = json.load(fh)
            self.assertEqual(got["orders"], want["orders"])
            self.assertIs(got["dry_run"], True)
            self.assertEqual(got["orders"][0]["pair"], "M&M-2026-11-11-101600")
        finally:
            shutil.rmtree(d)

    def test_a_limit_of_your_own_on_the_tick(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0.5), limit=3430.05, last=LAST, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"]), (3430.1, "limit"))   # 3430.05 to the 0.1 tick
        s = size.size_trade(plan(), cfg(min_reward_risk=0.5), limit=3430.0, last=LAST, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["qty"], s["charges_est"], s["max_loss"]), (62.6, 30, 83.17, 1961.17))
        s = size.size_trade(plan(), cfg(), limit=3430.0, last=LAST, today=TODAY)
        self.assertIn("reward:risk 0.87", s["refusal"])                       # 39.60 / 45.60

    def test_report_words(self):
        s = size.size_trade(plan(), cfg(), last=LAST, today=TODAY)
        text = size.plan_report(s, size.kite_pair(s, "P1"))
        self.assertIn("Max loss at the stop: 32 x 59.60 + %s85.56 charges (estimate) = %s1,992.76." % (R, R), text)
        self.assertIn("SL trigger 3384.40 limit 3367.40", text)
        self.assertIn("Zerodha's %s50 + GST fee for a position it squares off is not in it." % R, text)
        self.assertIn("nothing has been placed", text)


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=50), last=LAST, today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("one share risks %s59.60 plus charges" % R, s["refusal"])
        self.assertEqual(size.kite_pair(s, "P"), [])

    def test_each_limit_can_bind(self):
        s = size.size_trade(plan(), cfg(max_position_pct=10), last=LAST, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (5, "position cap"))    # 20,000 / 3427
        s = size.size_trade(plan(), cfg(max_order_inr=10000), last=LAST, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (2, "max order"))       # 10,000 / 3427
        s = size.size_trade(plan(), cfg(), last=LAST, cash=50000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (8, "cash"))            # (50,000 - 20,000) / 3427

    def test_no_fresh_price_is_no_order(self):
        s = size.size_trade(plan(), cfg(), last=None, today=TODAY)
        self.assertIn("no last price for M&M under 15 minutes old", s["refusal"])
        self.assertEqual(size.kite_pair(s, "P"), [])

    def test_the_cli_reads_the_price_with_its_source_and_age(self):
        d = tempfile.mkdtemp()
        try:
            pp = os.path.join(d, "prices.json")
            args = ["--plan", PLAN, "--config", CFG, "--prices", pp, "--portfolio", "none.json", "--now", NOW]
            with open(pp, "w") as fh:
                json.dump(prices_doc(source="kite_ltp"), fh)
            code, text, _ = run(args)
            self.assertEqual(code, 0)
            self.assertIn("Last price %s3,428.50 (kite_ltp, 30 s old)." % R, text)
            with open(pp, "w") as fh:
                json.dump(prices_doc(quote_time="2026-11-11T04:25:00Z"), fh)        # 9:55, 21 minutes old
            code, text, _ = run(args)
            self.assertEqual(code, 1)
            self.assertIn("from 2026-11-11 09:55 India time: not used (older than 15 minutes", text)
            self.assertIn("No order: no last price for M&M", text)
            with open(pp, "w") as fh:
                json.dump({"prices": {"M&M": 3428.5}}, fh)                          # no time anywhere
            code, text, _ = run(args)
            self.assertEqual(code, 1)
            self.assertIn("is of unknown age: not used", text)
        finally:
            shutil.rmtree(d)

    def test_reward_risk_and_minimum_order(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=1.5), last=LAST, today=TODAY)
        self.assertIn("reward:risk 1.00", s["refusal"])
        s = size.size_trade(plan(), cfg(min_order_inr=200000), last=LAST, today=TODAY)
        self.assertIn("under your %s2,00,000 minimum order" % R, s["refusal"])

    def test_bad_plans(self):
        for p, words in ((plan(side="sell"), "long (buy) day trades only"),
                         (plan(tick=None), "no tick size for M&M"),
                         (plan(stop=3430.0), "is not below the entry"),
                         (plan(target=3420.0), "the move has already happened"),
                         (plan(date="2026-11-10"), "the plan is for 2026-11-10 and today is 2026-11-11"),
                         (plan(symbol=""), "no symbol"),
                         (plan(entry=None), "a positive entry")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), last=LAST, today=TODAY)
            self.assertIn(words, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["m&m"]), last=LAST, today=TODAY)
        self.assertIn("blocked list", str(e.exception))

    def test_an_older_plan_without_the_fo_flag_counts_as_fo(self):
        p = plan()
        del p["fo"]
        s = size.size_trade(p, cfg(), last=LAST, today=TODAY)
        self.assertEqual((s["fo"], s["fo_unknown"]), (True, True))
        self.assertIn("F&O list unknown (an older plan): counted as F&O, square-off 15:12",
                      size.plan_report(s, size.kite_pair(s, "P")))
        s = size.size_trade(plan(fo=False), cfg(), last=LAST, today=TODAY)
        self.assertEqual((s["fo"], s["fo_unknown"]), (False, False))

    def test_a_last_price_at_or_under_the_stop_is_no_order(self):
        # R32: the buy would fill at once and Kite refuses an SL sell triggered above the last price.
        s = size.size_trade(plan(), cfg(), last=dict(LAST, price=3384.4), today=TODAY)
        self.assertEqual(s["refusal"], "the last price %s3,384.40 is at or under the stop's trigger 3384.40: the "
                         "buy would fill at once and Kite refuses an SL sell triggered above the last price (or it "
                         "fires at once), leaving no stop. No order." % R)
        self.assertEqual(size.kite_pair(s, "P"), [])
        self.assertIsNone(size.size_trade(plan(), cfg(), last=dict(LAST, price=3384.5), today=TODAY)["refusal"])

    def test_a_limit_that_is_not_a_number_is_one_line(self):
        # R34: "2,000" is neither switched off in silence nor a traceback, in both modes and at the CLI.
        for key in size.NUMBER_KEYS:
            for bad in ("2,000", True):
                with self.assertRaises(size.SizeError) as cm:
                    size.size_trade(plan(), cfg(**{key: bad}), last=LAST, today=TODAY)
                self.assertEqual(str(cm.exception), "trader/config.json: %s must be a number (or null), got %r"
                                 % (key, bad))
        with self.assertRaises(size.SizeError):
            size.size(size.parse_weights(size.weights_from_arg("SBIN=100")), cfg(max_order_inr="2,000"),
                      prices=PRICES)
        r = size.size(size.parse_weights(size.weights_from_arg("SBIN=100")),
                      cfg(cash_buffer_pct=None, max_position_pct=None),
                      prices=PRICES)        # null: no buffer, no cap (not a traceback)
        self.assertEqual(r["cash_buffer_pct"], 0.0)
        self.assertIsNone(size.size_trade(plan(), cfg(max_order_inr=None), last=LAST, today=TODAY)["refusal"])
        d = tempfile.mkdtemp()
        try:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(cfg(max_loss_per_trade_inr="2,000"), fh)
            code, _, err = run(["--plan", PLAN, "--config", c, "--now", NOW])
            self.assertEqual((code, err.strip()), (1, "size.py: trader/config.json: max_loss_per_trade_inr must "
                                                      "be a number (or null), got '2,000'"))
        finally:
            shutil.rmtree(d)

    def test_an_old_us_config_is_refused_by_name(self):
        d = tempfile.mkdtemp()
        try:
            c = os.path.join(d, "config.json")
            for bad, words in (({"paper": True}, "'paper' (use dry_run)"),
                               ({"max_loss_per_trade_usd": 50}, "'max_loss_per_trade_usd' (use max_loss_per_trade_inr)"),
                               ({"pdt_guard": False}, "'pdt_guard'"),
                               ({"broker": "alpaca"}, "broker 'alpaca': this pack trades on NSE through Zerodha only")):
                with open(c, "w") as fh:
                    json.dump(dict(cfg(), **bad), fh)
                code, _, err = run(["--plan", PLAN, "--config", c, "--now", NOW])
                self.assertEqual(code, 1)
                self.assertIn(words, err)
        finally:
            shutil.rmtree(d)

    def test_bare_limit_is_the_plans_entry_and_errors_are_one_line(self):
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--limit", "abc", "--now", NOW])
        self.assertEqual((code, err.strip()), (1, "size.py: --limit wants a price, like --limit 3428.5"))
        code, _, err = run(["--plan", "nope.json", "--config", CFG, "--now", NOW])
        self.assertIn("no plan (run trader-exit first", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "soon"])
        self.assertIn("--now wants a time like", err)
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--limit", "--now", NOW, "--json"])
        self.assertEqual(json.loads(text)["sizing"]["entry_from"], "plan")


class WeightsMode(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.uni = os.path.join(self.d, "universe.txt")
        with open(self.uni, "w") as fh:
            fh.write(UNIVERSE)
        with open(TARGETS) as fh:
            self.rows = size.parse_weights(json.load(fh))

    def tearDown(self):
        shutil.rmtree(self.d)

    def shares(self, result):
        return {p["symbol"]: p["shares"] for p in result["positions"]}

    def test_the_example_split(self):
        r = size.size(self.rows, cfg(), prices=PRICES)
        # slices 54,000 / 45,000 / 36,000 / 27,000 / 18,000; floors, then one more SBIN and one more INFY
        self.assertEqual(self.shares(r), {"RELIANCE": 36, "HDFCBANK": 45, "INFY": 23, "TCS": 8, "SBIN": 19})
        self.assertEqual((r["invested"], r["idle"], r["cash_buffer"]), (179450.0, 550.0, 20000.0))

    def test_orders_are_kite_limits_on_the_tick(self):
        r = size.size(self.rows, cfg(), prices=dict(PRICES, TCS=3200.07))
        orders = size.orders_from(r, size.read_universe(self.uni), "the example")
        self.assertEqual(len(orders), 5)
        o = [x for x in orders if x["tradingsymbol"] == "TCS"][0]
        self.assertEqual((o["transaction_type"], o["order_type"], o["product"], o["price"], o["quantity"], o["tag"]),
                         ("BUY", "LIMIT", "MIS", 3200.0, 8, "traderpack"))   # 3200.07 down to the 0.1 tick
        self.assertIn("weights from the example", o["why"])

    def test_cap_redistributes_until_nobody_is_over(self):
        r = size.size(self.rows, cfg(max_position_pct=20), prices=PRICES)
        self.assertEqual(self.shares(r), {"RELIANCE": 26, "HDFCBANK": 40, "INFY": 25, "TCS": 11, "SBIN": 27})
        self.assertEqual({p["symbol"] for p in r["positions"] if p["capped"]}, {"RELIANCE", "HDFCBANK", "INFY"})
        self.assertEqual((r["invested"], r["idle"]), (179850.0, 150.0))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        r = size.size(self.rows, cfg(min_order_inr=20000), prices=PRICES)
        self.assertEqual(r["dropped"], [{"symbol": "SBIN",
                                         "reason": "its slice (%s18,050) is under your %s20,000 minimum order" % (R, R)}])
        self.assertEqual(self.shares(r), {"RELIANCE": 40, "HDFCBANK": 51, "INFY": 25, "TCS": 9})

    def test_blocked_top_and_errors(self):
        r = size.size(self.rows, cfg(blocked=["tcs"]), prices=PRICES)
        self.assertIn({"symbol": "TCS", "reason": "on your blocked list"}, r["dropped"])
        r = size.size(self.rows, cfg(), prices=PRICES, top=2)
        self.assertEqual(sorted(self.shares(r)), ["HDFCBANK", "RELIANCE"])
        with self.assertRaises(size.SizeError) as e:
            size.size(self.rows, cfg(), prices={"RELIANCE": 1500.0})
        self.assertIn("whole shares need a price for: HDFCBANK, INFY, TCS, SBIN", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.orders_from(size.size(self.rows, cfg(), prices=PRICES), {"RELIANCE": (0.1, True)})
        self.assertIn("no tick size for HDFCBANK, INFY, TCS, SBIN", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size(self.rows + self.rows[:1], cfg(), prices=PRICES)
        with self.assertRaises(size.SizeError):
            size.size(self.rows, cfg(budget=None), prices=PRICES)

    def test_cli(self):
        pp = os.path.join(self.d, "prices.json")
        with open(pp, "w") as fh:
            json.dump({"prices": PRICES}, fh)
        out = os.path.join(self.d, "orders.json")
        code, text, err = run(["--targets", TARGETS, "--config", CFG, "--prices", pp, "--universe", self.uni,
                               "--write", out, "--whole-shares", "--cents"])
        self.assertEqual(code, 0, err)
        self.assertIn("BUY 36 RELIANCE, LIMIT 1500.00, MIS, DAY", text)
        self.assertIn("These buys have no stop", text)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["orders"]), 5)
        code, text, _ = run(["--weights", "RELIANCE=1,SBIN=1", "--config", CFG, "--prices", pp,
                             "--universe", self.uni, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(text)["orders"]), 2)


if __name__ == "__main__":
    unittest.main()
