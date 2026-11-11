import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-tsla.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
BATCH = ["AAPL", "AXP", "GOOGL", "KO", "BAC"]
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
TSLA_PLAIN = ("BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about "
              "$46.88 if the stop fills at 358.32 (it can fill worse in a fast market)")


def tsla(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=AAPL, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes AAPL", "yes $1,250 of AAPL",
                  "yes 1250"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes AXP", "yes 1000", "yes but make it 1000", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 340"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("yes sell 3 KO", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_bracket_yes(self):
        o = tsla()
        for r in ("yes", "yes 8 TSLA", "yes 358.32", "yes max loss 46.88", "yes target 370.04"):
            self.assertTrue(self.yes(r, o, ["TSLA"]), r)
        for r in ("yes 9", "yes 10 TSLA", "yes but stop at 357", "yes, stop 358.32", "yes AAPL", "sure"):
            self.assertFalse(self.yes(r, o, ["TSLA", "AAPL"]), r)


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_alpaca_call(self):
        o = dict(AAPL, why="from a list")
        self.assertEqual(ticket.tool_input(o), AAPL)
        self.assertEqual(ticket.tool_input({"symbol": "KO", "side": "buy", "qty": 3, "type": "limit",
                                            "limit_price": 89.5, "time_in_force": "day"}),
                         {"symbol": "KO", "side": "buy", "qty": "3", "type": "limit", "limit_price": "89.5",
                          "time_in_force": "day"})

    def test_the_bracket_call_leaves_out_why_and_plan(self):
        self.assertEqual(ticket.tool_input(tsla()), TSLA_CALL)
        self.assertEqual(ticket.fingerprint(tsla()), ticket.fingerprint(TSLA_CALL))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(AAPL), "BUY $1,250 of AAPL, market order, good for today")
        self.assertEqual(ticket.plain({"symbol": "KO", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "95", "time_in_force": "gtc"}),
                         "SELL 1 share of KO, limit order at $95, good until canceled")
        self.assertEqual(ticket.plain(tsla()), TSLA_PLAIN)
        lim = tsla(type="limit", limit_price="364.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 8 TSLA, limit 364.00, good for today")
        self.assertEqual(ticket.max_loss(lim), (45.44, 358.32))
        no_plan = dict(TSLA_CALL)
        self.assertEqual(ticket.max_loss(no_plan), (None, 358.32))


class DesktopConfig(unittest.TestCase):
    def test_example_is_paper_with_placeholder_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["alpaca"]
        self.assertEqual((server["command"], server["args"]), ("uvx", ["alpaca-mcp-server"]))
        self.assertEqual(server["env"]["ALPACA_PAPER_TRADE"], "true")
        for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            self.assertIn("NEVER_IN_CHAT", server["env"][k])


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_the_full_two_yes_flow(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER account, Alpaca", text)
        for part in TSLA_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn('"order_class": "bracket"', text)
        self.assertIn('"stop_loss_stop_price": "358.32"', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn("Allow once", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("place_stock_order", text)
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1]), TSLA_CALL)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc123",
                                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf,
                                      now="2026-10-19 10:16"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 46.88, TODAY))
        self.assertEqual(appr["at"], "2026-10-19T10:15:00-04:00")
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"]), ("abc123", 46.88, "entry"))
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 2086.4)
        self.assertEqual(pf["positions"][0]["shares"], 8)

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss $46.88", text)

        # trader-risk now counts it: a second TSLA buy today is a duplicate, and $46.88 is used
        res = check.check([tsla()], {"budget": 5000}, state=st, now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 46.88))

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf))
        late = "2026-10-19 15:55"
        code, text, _ = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 8 shares of TSLA, market order, good for today (flat by 15:55 New York)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancel_order_by_id", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(ticket.tool_input(doc["orders"][0]),
                         {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"})
        self.assertEqual(self.run_risk(now=late), 0)       # after the 15:30 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 8 TSLA", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                   "--filled-price", "366.10", "--portfolio", self.pf, now="2026-10-19 15:56"))
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual((pf["cash"], pf["positions"]), (5015.2, []))
        self.assertEqual(check.day_trades(self.state_doc(), TODAY), (1, ["2026-10-19 TSLA"]))
        code, _, err = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "tsla", "--qty", "8", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_open_position", text)
        self.assertEqual(run(["flatten", "--symbol", "TSLA", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "9"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 10:15")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def risk_doc(self, **change):
        with open(self.risk) as fh:
            doc = json.load(fh)
        for k, v in change.items():
            for d in [doc] + (doc["results"] if k == "entries_until" else []):  # the file's and each order's
                if v is None:
                    d.pop(k, None)
                else:
                    d[k] = v
        with open(self.risk, "w") as fh:
            json.dump(doc, fh)

    def test_a_pass_holds_for_15_minutes(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:30"))
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:31"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: the risk check is 16 minutes old (from 10:15 New York; it holds 15)", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:31"))
        self.assertEqual(code, 1)
        self.assertIn("16 minutes old", err)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:14"))
        self.assertIn("NOT READY: the risk check is timed 10:15 New York, after now (10:14)", text)

    def test_a_pass_before_the_cutoff_is_no_yes_after_it(self):
        # the review's case: PASS at 15:29, then a yes after the window or the close
        self.assertEqual(self.run_risk(now="2026-10-19 15:29"), 0)
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["entries_until"], "15:30")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 15:30"))
        self.assertIn("READY: trader-risk PASS", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 15:31"))
        self.assertIn("NOT READY: no new entries after 15:30 New York today (it is 15:31)", text)
        for late in ("2026-10-19 15:31", "2026-10-19 16:30"):
            code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now=late))
            self.assertEqual(code, 1, late)
            self.assertIn("not ready", err)
        self.assertFalse(os.path.exists(self.state))

    def test_the_cutoff_follows_the_risk_check(self):
        self.run_risk()
        self.risk_doc(entries_until="")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("NOT READY: the market is closed today", text)
        self.risk_doc(entries_until=None)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("NOT READY: the risk check is from an older trader-risk", text)
        self.risk_doc(now_new_york=None, entries_until="15:30")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("NOT READY: the risk check has no time on it", text)

    def test_a_yes_left_open_expires_with_its_pass(self):
        # Fable R1: a yes at 15:29 with the card left open must not read as "place it" at 16:30
        self.assertEqual(self.run_risk(now="2026-10-19 15:29"), 0)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 15:29"))
        self.assertEqual(code, 0, err)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 15:30"))
        self.assertIn("APPROVED by you at 2026-10-19T15:29:00-04:00, waiting for the place_stock_order", text)
        for late, why in (("2026-10-19 15:31", "no new entries after 15:30 New York today (it is 15:31)"),
                          ("2026-10-19 16:30", "the risk check is 61 minutes old (from 15:29 New York; it holds 15); "
                                              "run check.py again")):
            code, text, _ = run(self.args("show", "--index", "1", now=late))
            self.assertIn("EXPIRED: your yes at 2026-10-19T15:29:00-04:00 no longer holds (%s). "
                          "Do not place it" % why, text)
            self.assertNotIn("waiting for the place_stock_order", text)
        code, _, err = run(self.args("result", "--index", "1", "--status", "failed", "--note", "expired",
                                     now="2026-10-19 16:30"))
        self.assertEqual(code, 0, err)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "failed"])

    HELD = ("NOT READY: your yes at %s was on an earlier check. Look at the broker's open orders and "
            "positions for TSLA, then record ticket.py result --status placed (or failed or denied) if it "
            "was sent, or ticket.py result --status unsent if it was not")

    def test_a_new_check_holds_an_old_yes_until_unsent(self):
        # Fable R5(a) + R8: yes 10:20 on the 10:15 check, card left open, a new check at 14:00
        self.assertEqual(self.run_risk(), 0)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_risk(now="2026-10-19 14:00"), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:00"))
        self.assertEqual(code, 1)
        self.assertNotIn("APPROVED", text)
        self.assertIn(self.HELD % "2026-10-19T10:20:00-04:00", text)
        before = self.state_doc()
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 14:00"))
        self.assertEqual(code, 1)
        self.assertIn("was on an earlier check", err)
        self.assertEqual(self.state_doc(), before)  # refused, nothing written
        code, text, _ = run(self.args("result", "--index", "1", "--status", "unsent", now="2026-10-19 14:00"))
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:00"))
        self.assertIn("READY: trader-risk PASS, waiting for your yes", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 14:00"))
        self.assertEqual(code, 0, err)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:01"))
        self.assertIn("APPROVED by you at 2026-10-19T14:00:00-04:00, waiting for the place_stock_order", text)
        recs = self.state_doc()["orders"]
        self.assertEqual([r["status"] for r in recs], ["approved", "unsent", "approved"])
        with open(self.risk) as fh:
            self.assertEqual(recs[2]["risk_check"], json.load(fh)["check_id"])
        self.assertNotEqual(recs[0]["risk_check"], recs[2]["risk_check"])
        # the log names an unsent record for what it is
        code, text, _ = run(["log", "--state", self.state, "--now", "2026-10-19 14:01"])
        self.assertIn("unsent", text)
        self.assertIn("(never sent; the yes no longer counts)", text)
        self.assertIn("0 placed today.", text)
        # even a re-check in the same minute is a new check: the 14:00 yes is held again
        self.assertEqual(self.run_risk(now="2026-10-19 14:01"), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:01"))
        self.assertIn(self.HELD % "2026-10-19T14:00:00-04:00", text)

    def test_an_expired_yes_takes_unsent_then_a_new_check(self):
        # Fable R5(b) + R8: EXPIRED on the same check, then the steps in order
        self.assertEqual(self.run_risk(), 0)
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:31"))
        self.assertIn("EXPIRED: your yes at 2026-10-19T10:20:00-04:00 no longer holds", text)
        self.assertIn("Do not place it; record ticket.py result --status unsent (or what really happened), "
                      "run check.py again, then ask for a new yes", text)
        code, _, err = run(self.args("result", "--index", "1", "--status", "unsent", now="2026-10-19 10:31"))
        self.assertEqual(code, 0, err)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:31"))
        self.assertIn("NOT READY: the risk check is 16 minutes old", text)  # unsent alone is no PASS
        self.assertEqual(self.run_risk(now="2026-10-19 10:31"), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:31"))
        self.assertEqual(code, 0)
        self.assertIn("READY: trader-risk PASS", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:31"))
        self.assertEqual(code, 0, err)

    def test_placed_against_a_held_yes_is_done_for_good(self):
        self.assertEqual(self.run_risk(), 0)
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        self.assertEqual(self.run_risk(now="2026-10-19 14:00"), 0)
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                                     now="2026-10-19 14:00"))
        self.assertEqual(code, 0, err)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:00"))
        self.assertIn("DONE: placed at 2026-10-19T14:00:00-04:00", text)
        # the real re-check blocks it as a duplicate (TSLA placed today)
        self.assertEqual(self.run_risk(now="2026-10-19 14:05"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:05"))
        self.assertIn("BLOCKED by trader-risk: duplicate", text)
        # even a fresh PASS that did not see the log leaves it DONE: placed is terminal
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf, "--prices",
                        self.none, "--state", self.none, "--now", "2026-10-19 14:05", "--out", self.risk])
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 14:05"))
        self.assertIn("DONE: placed at 2026-10-19T14:00:00-04:00", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 14:05"))
        self.assertEqual(code, 1)

    def test_unsent_needs_a_yes_before_it(self):
        self.assertEqual(self.run_risk(), 0)
        code, _, err = run(self.args("result", "--index", "1", "--status", "unsent"))
        self.assertEqual(code, 1)
        self.assertIn("a result can only follow an approval", err)
        self.assertFalse(os.path.exists(self.state))
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "unsent"))
        code, _, err = run(self.args("result", "--index", "1", "--status", "unsent"))
        self.assertEqual(code, 1)  # not twice: unsent is no yes
        self.assertIn("a result can only follow an approval", err)

    def test_a_failed_order_stays_done_and_a_yes_needs_a_check_id(self):
        # a real failed is untouched by R5: the broker may hold it
        self.assertEqual(self.run_risk(), 0)
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:16"))
        run(self.args("result", "--index", "1", "--status", "failed", "--note", "rejected", now="2026-10-19 10:17"))
        self.assertEqual(self.run_risk(now="2026-10-19 10:20"), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:20"))
        self.assertIn("DONE: failed at 2026-10-19T10:17:00-04:00", text)
        # a risk.json with no check id can show a done order but cannot take a yes
        self.risk_doc(check_id=None)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:20"))
        self.assertIn("DONE: failed", text)
        os.remove(self.state)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:20"))
        self.assertIn("NOT READY: the risk check is from an older trader-risk with no check id", text)
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        self.assertFalse(os.path.exists(self.state))  # refused: not READY
        # a yes from an older trader-broker (no risk_check) on such a check is bound to nothing: held
        with open(self.risk) as fh:
            fp = json.load(fh)["results"][0]["fingerprint"]
        with open(self.state, "w") as fh:
            json.dump({"orders": [{"at": "2026-10-19T10:20:00-04:00", "date": TODAY, "index": 1, "symbol": "TSLA",
                                   "fingerprint": fp, "status": "approved"}]}, fh)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:21"))
        self.assertNotIn("APPROVED", text)
        self.assertIn(self.HELD % "2026-10-19T10:20:00-04:00", text)

    def test_a_yes_expires_after_15_minutes_too(self):
        self.assertEqual(self.run_risk(), 0)
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 10:31"))
        self.assertIn("EXPIRED: your yes at 2026-10-19T10:20:00-04:00 no longer holds (the risk check is "
                      "16 minutes old", text)

    def test_a_cutoff_that_is_not_a_time_fails_closed(self):
        # Fable R2: "10:00" > "9:30" and "15:31" > "True" are both False as strings
        self.run_risk()
        for bad in ("9:30", True, "15:30:00", "soon", 1530):
            self.risk_doc(entries_until=bad)
            code, text, _ = run(self.args("show", "--index", "1"))
            self.assertIn("NOT READY: the risk check's entry cut-off %r is not a time" % (bad,), text)
        self.risk_doc(entries_until="15:30")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("READY: trader-risk PASS", text)

    def test_window_off_a_plain_day_entry_still_ends_at_the_close(self):
        # owner, R4: the window off lifts no_entries_* only; the close still binds a plain day order
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(no_entries_before=None, no_entries_after=None)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        self.assertEqual(self.run_risk(now="2026-10-19 15:55"), 0)
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["entries_until"], "15:59")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 15:59"))
        self.assertIn("READY: trader-risk PASS", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 16:05"))
        self.assertIn("NOT READY: no new entries after 15:59 New York today (it is 16:05)", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 16:05"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state))
        # the same order as gtc: no cut-off, only the 15 minutes
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["time_in_force"] = "gtc"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        self.assertEqual(self.run_risk(now="2026-10-19 16:05"), 0)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 16:10"))
        self.assertIn("READY: trader-risk PASS", text)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 16:21"))
        self.assertIn("16 minutes old", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:40"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:40"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:45", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 09:40"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_ibkr_tickets_send_the_final_yes_to_ibkr(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["broker"] = "ibkr"
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("Interactive Brokers", text)
        self.assertIn("after you approve it there", text)
        self.assertNotIn("place_stock_order", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "KO", "shares": 3, "lots": [
            {"id": "KO-1", "date": "2025-01-02", "shares": 2, "cost": 120.0},
            {"id": "KO-2", "date": "2026-01-02", "shares": 1, "cost": 70.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "2.5", "--price", "90",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 225.0)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["KO-2"])
        self.assertEqual(lots[0]["shares"], 0.5)
        self.assertEqual(lots[0]["cost"], 35.0)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "5", "--price", "90",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
