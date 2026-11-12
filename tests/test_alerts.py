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

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")    # Yahoo range=1d quotes for six NSE stocks, read 2026-11-12 08:55 IST
PLAN = os.path.join(FIX, "trader-budget", "plan-mm.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
R = "₹"


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_parse_now_is_india_time(self):
        self.assertEqual(ist("2026-11-11T09:15"), DT(2026, 11, 11, 3, 45))
        self.assertEqual(ist("2026-07-01 10:15"), DT(2026, 7, 1, 4, 45))   # no summer time
        self.assertEqual(ist("2026-11-11T09:59:54Z"), DT(2026, 11, 11, 9, 59, 54))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-11-11T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"], st["now_india"]), ("09:15", "15:30", "2026-11-11 10:00"))
        before = alerts.market_status(ist("2026-11-11T09:14"))
        self.assertFalse(before["open"])
        self.assertEqual(before["next_open"], "2026-11-11 09:15")
        self.assertTrue(alerts.market_status(ist("2026-11-11T15:29"))["open"])
        after = alerts.market_status(ist("2026-11-11T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["next_open"], "2026-11-12 09:15")
        diwali = alerts.market_status(ist("2026-11-10T11:00"))
        self.assertFalse(diwali["open"])
        self.assertIn("Diwali-Balipratipada", diwali["reason"])
        self.assertEqual(alerts.market_status(ist("2026-11-07T11:00"))["next_open"], "2026-11-09 09:15")  # Sat;
        muhurat = alerts.market_status(ist("2026-11-08T18:30"))                 # Sun 8 Nov: Muhurat
        self.assertFalse(muhurat["open"])
        self.assertIn("Muhurat", muhurat["reason"])
        xmas = alerts.market_status(ist("2026-12-25T10:00"))
        self.assertEqual((xmas["open"], xmas["next_open"]), (False, "2026-12-28 09:15"))
        self.assertEqual(alerts.market_status(ist("2026-11-14T10:00"))["reason"], "weekend")
        y27 = alerts.market_status(ist("2027-03-01T10:00"))
        self.assertFalse(y27["calendar_known"])
        self.assertFalse(y27["open"])                                           # no calendar: closed


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-M&M.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "M&M")
        self.assertEqual((q["price"], q["prev_close"], q["open"], q["currency"]), (3465.0, 3290.6, 3340.0, "INR"))
        self.assertEqual(q["time"], "2026-11-11T09:59:54Z")                      # 15:29:54 IST
        with open(os.path.join(QDIR, "yahoo-ZZZZ.json")) as fh:
            with self.assertRaises(alerts.AlertError) as cm:
                alerts.parse_yahoo(fh.read(), "ZZZZ")
        self.assertIn("ZZZZ.NS", str(cm.exception))

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("m&m"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=1d")
        self.assertIn("//query1.finance.yahoo.com/v8/finance/chart/BAJAJ-AUTO.NS?",
                      alerts.yahoo_url("BAJAJ-AUTO", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))                          # Stooq is gone

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["M&M", "SBIN", "ZZZZ", "NOPE"], "yahoo", QDIR)
        self.assertEqual(sorted(q), ["M&M", "SBIN"])
        self.assertEqual(q["SBIN"]["price"], 1061.2)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline quote file", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-TCS.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-TCS.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["TCS"], "yahoo", d)
            self.assertEqual((q["TCS"]["price"], errors), (2472.1, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-M&M.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["M&M"])
        self.assertEqual((q["M&M"]["price"], errors), (3465.0, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["M&M"])[0]["M&M"]["price"], 3465.0)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("M&M", alerts.get_quotes(["M&M"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("M&M", alerts.get_quotes(["M&M"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["M&M"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/M%26M.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["M&M"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["M&M", "SBIN"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["M&M", "SBIN", "TCS", "HDFCBANK"], "yahoo", QDIR)
        self.open = alerts.market_status(ist("2026-11-11T10:00"))
        self.now = ist("2026-11-11T10:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "sbin-1060", "symbol": "SBIN", "type": "above", "price": 1060}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("SBIN is %s1,061.20: at or above your alert level %s1,060.00" % (R, R), rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "tcs-stop", "symbol": "TCS", "type": "stop", "price": 2480, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop %s2,480.00" % R, rings[0]["message"])
        self.q["TCS"]["price"] = 2490.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("tcs-stop", st["rung"])
        self.q["TCS"]["price"] = 2470.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "sbin-3", "symbol": "SBIN", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "sbin-1", "symbol": "SBIN", "type": "pct_move", "pct": 1, "direction": "up"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("SBIN moved +1.84%% to %s1,061.20 (from %s1,042.00)" % (R, R), rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        down = [{"id": "sbin-dn", "symbol": "SBIN", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        mm = [{"id": "mm-3", "symbol": "M&M", "type": "pct_move", "pct": 3}]
        rings, _ = self.at("2026-11-11 15:29", mm)
        self.assertIn("M&M moved +5.30%% to %s3,465.00 (from %s3,290.60)" % (R, R), rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-11-11T09:00"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-11-11T09:00"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: the NSE is open (2026-11-11 10:00 India time). No orders before 9:30.",
                      rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-11-11T11:00"))[0], [])
        nxt = alerts.market_status(ist("2026-11-12T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-11-12T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_the_1500_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("m&m-plan-stop", "stop"), ("m&m-plan-target", "target"), ("m&m-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (3384.4, 3469.6))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:00", "flat by 3:00 PM"))
        self.assertNotIn("before_close", rules[2])
        self.assertTrue(all(r["date"] == "2026-11-11" for r in rules))
        q = json.loads(json.dumps(self.q))
        q["M&M"]["price"] = 3427.0
        rings, st = self.at("2026-11-11 10:20", rules, None, q)
        self.assertEqual(rings, [])
        q["M&M"]["price"] = 3380.0
        rings, st = self.at("2026-11-11 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["m&m-plan-stop"])
        self.assertIn("M&M is %s3,380.00: at or below your stop %s3,384.40 (plan stop: opening-range midpoint)"
                      % (R, R), rings[0]["message"])
        q["M&M"]["price"] = 3469.6
        self.assertEqual([r["id"] for r in self.at("2026-11-11 12:00", rules, st, q)[0]], ["m&m-plan-target"])
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.rules_from_plan(dict(json.load(open(PLAN)), flat_by="15:20"))
        self.assertIn("later than 15:00", str(cm.exception))

    def test_the_flat_by_rings_once_at_1500(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-11 14:59", rules)[0], [])
        rings, st = self.at("2026-11-11 15:00", rules)
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        self.assertIn("Flat by 3:00 PM: it is 15:00 India time (set for 15:00), the market closes at 15:30", msg)
        self.assertIn("no position is held past 3:00 PM", msg)
        self.assertIn("3:12 PM for an F&O-list stock, 3:25 PM for the rest", msg)
        self.assertEqual(self.at("2026-11-11 15:10", rules, st)[0], [])
        rings, _ = self.at("2026-11-11 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-12 15:01", rules, st)[0], [])     # plan rules: their day only

    def test_a_time_alert_needs_a_session(self):
        rule = [{"id": "t", "type": "time", "at": "14:45", "label": "check"}]
        self.assertEqual(self.at("2026-11-14 14:46", rule)[0], [])          # Saturday: no session
        self.assertEqual(self.at("2026-11-10 14:46", rule)[0], [])          # Diwali-Balipratipada
        rings, st = self.at("2026-11-11 14:45", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["t"], "2026-11-11T09:15:00Z")
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "time", "at": "3pm"})

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
            self.assertNotIn("before_close", r)
        self.assertEqual([r for r in rules if r["id"].startswith("m&m-plan") or r["id"] == "m&m-flat-by"],
                         [dict(r, date="2026-11-11") for r in plan_rules()])

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "SBIN"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "4pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "M&M", "stop": 3384.4})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_reel_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added m&m-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-11 10:16")
        self.assertEqual(code, 0)                                      # the opening bell rings first; the
        self.assertIn("ALERT Opening bell", text)                     # fixture's 3,465.00 is between stop and target
        self.assertNotIn("your stop", text)
        self.assertNotIn("your target", text)
        code, text, _ = self.check("2026-11-11 10:17")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, M&M %s3,465.00." % R, text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"M&M": 3465.0})
        self.assertEqual(doc["detail"]["M&M"]["time"], "2026-11-11T09:59:54Z")
        self.assertEqual(doc["detail"]["M&M"]["fetched"], "2026-11-11T04:47:00Z")

        code, text, _ = self.check("2026-11-11 15:00")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by 3:00 PM", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-11-11 15:02")[0], 1)
        code, text, _ = self.check("2026-11-12 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-12 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules, encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["m&m-flat-by", "m&m-plan-stop", "m&m-plan-target", "opening-bell"])
        with open(self.state, encoding="utf-8") as fh:
            self.assertNotIn("m&m-flat-by", json.load(fh)["rung"])

    def test_rules_for_a_portfolio_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "SBIN", "--above", "1100"], ["--symbol", "TCS", "--stop", "2400"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "SBIN", "--above", "1100"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-11-11T19:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"SBIN": 1061.2, "TCS": 2472.1})

        code, text, _ = self.check("2026-11-12T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-12T09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "14:45", "--label", "flat by 2:45 PM",
                             "--symbol", "m&m"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "M&M", "--target", "3469.6",
                             "--date", "2026-11-11"])
        self.assertEqual(code, 0)
        with open(self.rules, encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1445", "type": "time", "at": "14:45", "label": "flat by 2:45 PM",
                                    "symbol": "M&M"})
        self.assertEqual(rules[1], {"id": "m&m-target-3469.60", "symbol": "M&M", "type": "target",
                                    "price": 3469.6, "date": "2026-11-11"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        with self.assertRaises(SystemExit):                            # argparse: the flag is gone
            run(["add", "--rules", self.rules, "--at", "14:00", "--before-close"])
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "M&M", "SBIN", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-11T09:59:54Z"])
        self.assertEqual(code, 0)
        self.assertIn("M&M           %s3,465.00 (+5.30%% vs prev close %s3,290.60)" % (R, R), text)
        self.assertIn("delayed", text)
        run(["quote", "TCS", "--offline", QDIR, "--write", self.prices])
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["M&M", "SBIN", "TCS"])
        self.assertEqual(doc["detail"]["SBIN"]["prev_close"], 1042.0)
        self.assertEqual(doc["detail"]["SBIN"]["fetched"], "2026-11-11T09:59:54Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"tradingsymbol": "SBIN", "product": "MIS", "date": "2026-11-11",
                                                 "quantity": 1, "average_price": 950.0}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["M&M", "SBIN"])

    def test_clock_exit_codes(self):
        code, text, _ = run(["clock", "--now", "2026-11-11 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("India 2026-11-11 10:15 (Wednesday): NSE OPEN", text)
        self.assertIn("orders only from 09:30, none from 15:00", text)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        code, text, _ = run(["clock", "--now", "2027-01-04T10:00"])
        self.assertIn("treated as closed", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-alerts", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        low = " ".join(text.lower().split())
        for must in ("nse", "india", "3:00 pm", "9:30", "m&m", "never an order"):
            self.assertIn(must, low)
        for never in ("nyse", "new york", "stooq", "early close", "\u2014"):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\s?\d")


if __name__ == "__main__":
    unittest.main()
