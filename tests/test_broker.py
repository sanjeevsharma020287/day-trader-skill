import datetime
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
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-mm.json")
TODAY = "2026-11-11"                                  # a Wednesday, an NSE trading day
PAIR = "M&M-2026-11-11-101600"                         # the reel pair id from trader-budget
R = "₹"


def utc(india):
    """"2026-11-11 10:16" India time -> naive UTC."""
    return datetime.datetime.strptime(india, "%Y-%m-%d %H:%M") - datetime.timedelta(hours=5, minutes=30)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


LIVE = dict(dry_run=False, b0_proven_on="2026-11-10")


def orders():
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        return json.load(fh)["orders"]


def risk_for(os_, at="2026-11-11 10:16", check_id="chk1", c=None, ran_at=None, **over):
    """trader/state/risk.json as check.py writes it: the check on the clock `at`, run (checked_at) at `ran_at`."""
    now = utc(at)
    p = check.load_prices({"prices": {"M&M": 3428.5},
                           "detail": {"M&M": {"source": "yahoo", "time": (now - datetime.timedelta(seconds=30))
                                              .strftime("%Y-%m-%dT%H:%M:%SZ")}}})
    res = check.check(os_, c or cfg(), None, p, None, None, now)
    res["checked_at"] = utc(ran_at or at).strftime("%Y-%m-%dT%H:%M:%SZ")
    res["check_id"] = check_id
    res.update(over)
    return res


class Clocked(unittest.TestCase):
    """Every test runs on a pinned real clock: ticket.real_now is the only clock ticket.py reads (R36)."""
    CLOCK = "2026-11-11 10:17"

    def setUp(self):
        self._real = ticket.real_now
        self.set_clock(self.CLOCK)

    def tearDown(self):
        ticket.real_now = self._real

    def set_clock(self, india):
        t = utc(india)
        ticket.real_now = lambda: t

    def status(self, os_, idx=1, risk=None, state=None, now=None):
        return ticket.status_of(os_, idx, risk if risk is not None else risk_for(os_), state or {"orders": []},
                                TODAY, utc(now or self.CLOCK))


# ---------------------------------------------------------------- R14: real orders are the owner's to unlock

class B0Gate(unittest.TestCase):
    def test_dry_run_unless_false_and_b0_recorded(self):
        self.assertEqual(ticket.mode({}, TODAY)[0], "dry_run")
        self.assertEqual(ticket.mode(cfg(), TODAY), ("dry_run", "DRY RUN, nothing is sent"))
        self.assertEqual(ticket.mode(cfg(dry_run="false", b0_proven_on="2026-11-10"), TODAY)[0], "dry_run")
        self.assertEqual(ticket.mode(cfg(**LIVE), TODAY), ("live", "LIVE, REAL MONEY (B0 recorded on 2026-11-10)"))
        self.assertEqual(ticket.mode(cfg(dry_run=False, b0_proven_on=TODAY), TODAY)[0], "live")

    def test_absent_bad_and_future_dates_block(self):
        for v, why in ((None, "b0_proven_on is not recorded"), ("", "b0_proven_on is not recorded"),
                       ("13/11/2026", "b0_proven_on '13/11/2026' is not a date like"),
                       (20261110, "b0_proven_on 20261110 is not a date like"),
                       ("2026-02-30", "b0_proven_on '2026-02-30' is not a real date"),
                       ("2026-11-12", "b0_proven_on 2026-11-12 is in the future (today is 2026-11-11 in India)")):
            c = cfg(dry_run=False)
            if v is not None:
                c["b0_proven_on"] = v
            m, line = ticket.mode(c, TODAY)
            self.assertEqual(m, "dry_run", v)
            self.assertIn(why, line)
            self.assertIn("real orders stay blocked until the owner records B0 by hand", line)

    def test_no_tool_writes_the_owner_keys(self):
        # R14: both keys are the owner's hand edit. No example config ships them, and no code writes them
        # (a dict literal, an item assignment, a keyword argument or setdefault).
        for folder in sorted(f for f in os.listdir(ROOT) if f.startswith("trader-")):
            for name in os.listdir(os.path.join(ROOT, folder)):
                if not (name.endswith(".py") or name.endswith(".json")):
                    continue
                with open(os.path.join(ROOT, folder, name), encoding="utf-8") as fh:
                    text = fh.read()
                for key in ("b0_proven_on", "close_gate_proven_on"):
                    if name.endswith(".json"):
                        self.assertNotIn(key, text, (folder, name))
                        continue
                    for written in ('"%s":' % key, '["%s"] =' % key, "%s=" % key, 'setdefault("%s"' % key):
                        self.assertNotIn(written, text, (folder, name, written))


class CloseGate(unittest.TestCase):
    # R17: while close_gate_proven_on is unset the yes says the 3 pm close is not active.
    def test_the_words_until_the_owner_records_it(self):
        lines = ticket.close_gate_lines(cfg(), TODAY)
        self.assertEqual(len(lines), 1)
        for words in ("The 3 pm close is not active (close_gate_proven_on is not recorded)",
                      "a resting BUY can still fill after 15:00", "at 15:12 (F&O list) or 15:25",
                      "for %s50 + GST an order" % R):
            self.assertIn(words, lines[0])
        self.assertEqual(len(ticket.close_gate_lines(cfg(close_gate_proven_on="2026-12-01"), TODAY)), 1)
        self.assertEqual(ticket.close_gate_lines(cfg(close_gate_proven_on="2026-11-10"), TODAY), [])


# ---------------------------------------------------------------- the order as sent

class Order(unittest.TestCase):
    def test_tool_input_is_the_kite_fields_as_numbers(self):
        e, s = orders()
        self.assertEqual(ticket.tool_input(e), {
            "variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "BUY",
            "quantity": 32, "product": "MIS", "order_type": "LIMIT", "price": 3427.0, "validity": "DAY",
            "tag": "traderpack"})
        self.assertEqual(ticket.tool_input(s)["trigger_price"], 3384.4)

    def test_text_numbers_are_never_sent(self):
        # P5, read from source: the kite MCP server reads a missing or text quantity as 1 share.
        e, _ = orders()
        for over in ({"quantity": "32"}, {"quantity": None}, {"quantity": 32.5}, {"quantity": True},
                     {"price": "3427.0"}):
            with self.assertRaises(ticket.TicketError, msg=over):
                ticket.tool_input(dict(e, **over))

    def test_same_fingerprint_as_trader_risk(self):
        for o in orders():
            self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))
        self.assertEqual(ticket.KITE_FIELDS, check.KITE_FIELDS)

    def test_plain_words(self):
        e, s = orders()
        self.assertEqual(ticket.plain(e), "BUY 32 M&M, limit 3427.00, intraday (MIS), good for today")
        self.assertEqual(ticket.plain(s), "SELL 32 M&M, stop-loss: trigger 3384.40, limit 3367.40, intraday (MIS), "
                                          "good for today")


class Yes(unittest.TestCase):
    def ok(self, reply, others=("M&M", "SBIN")):
        e, s = orders()
        return ticket.parse_yes(reply, e, others, s, {"max_loss": 1992.76})[0]

    def test_only_a_plain_yes_for_this_ticket(self):
        for reply in ("yes", "Yes, M&M", "y", "yes 32", "yes 3427", "yes 3384.4", "yes 1,992.76", "yes 3469.6"):
            self.assertTrue(self.ok(reply), reply)
        for reply in ("ok", "sure", "go", "", "yes?", "yes to all", "yes both", "yes but wait", "yes if it dips",
                      "yes 33", "yes SBIN", "no"):
            self.assertFalse(self.ok(reply), reply)


# ---------------------------------------------------------------- READY, on the real clock

class Ready(Clocked):
    def test_the_pair_is_one_ticket(self):
        os_ = orders()
        ready, label, rv = self.status(os_)
        self.assertEqual((ready, label), (True, "READY: trader-risk PASS on the pair, waiting for your yes"))
        ready, label, _ = self.status(os_, 2)
        self.assertFalse(ready)
        self.assertEqual(label, "the stop of order 1: it goes in after that entry fills (ticket.py stop --pair %s)"
                         % PAIR)

    def test_a_blocked_or_missing_stop_leg_blocks_the_entry(self):
        os_ = orders()
        risk = risk_for(os_)
        risk["results"][1] = dict(risk["results"][1], verdict="BLOCK", reasons=["format: x"])
        ready, label, _ = self.status(os_, risk=risk)
        self.assertEqual((ready, label), (False, "BLOCKED by trader-risk: its stop (order 2): format: x"))
        risk["results"] = risk["results"][:1]
        ready, label, _ = self.status(os_, risk=risk)
        self.assertFalse(ready)
        self.assertTrue(label.startswith("NOT READY: its stop (order 2): this exact order was not in the last"), label)

    def test_a_changed_order_needs_a_new_check(self):
        os_ = orders()
        risk = risk_for(os_)
        os_[0]["quantity"] = 31
        ready, label, _ = self.status(os_, risk=risk)
        self.assertFalse(ready)
        self.assertIn("this exact order was not in the last risk check", label)

    def test_a_pinned_check_is_refused(self):
        # R36: check.py --now 10:16 run for real at 10:20, or --today another day.
        os_ = orders()
        ready, label, _ = self.status(os_, risk=risk_for(os_, at="2026-11-11 10:16", ran_at="2026-11-11 10:20"))
        self.assertFalse(ready)
        self.assertEqual(label, "NOT READY: the risk check was run with a pretended clock (it says 2026-11-11 "
                                "10:16, but it ran at 2026-11-11 10:20 India time); run check.py again without "
                                "--now or --today")
        ready, label, _ = self.status(os_, risk=risk_for(os_, date="2026-11-12"))
        self.assertTrue(label.startswith("NOT READY: the risk check is from 2026-11-12, not today"), label)
        risk = risk_for(os_)
        del risk["checked_at"]
        self.assertIn("no checked_at time", self.status(os_, risk=risk)[1])
        risk = risk_for(os_)
        del risk["check_id"]
        self.assertEqual(self.status(os_, risk=risk)[1], "NOT READY: the risk check has no check id; run check.py "
                                                         "again (without --no-write)")

    def test_the_check_holds_fifteen_minutes(self):
        os_ = orders()
        self.assertTrue(self.status(os_, now="2026-11-11 10:31")[0])
        ready, label, _ = self.status(os_, now="2026-11-11 10:32")
        self.assertEqual(label, "NOT READY: the risk check is 16 minutes old (from 10:16 India time; it holds 15); "
                                "run check.py again")
        self.assertIn("timed after now", self.status(os_, now="2026-11-11 10:14")[1])

    def test_the_window_is_judged_on_the_real_clock(self):
        # R36: a check made at 14:50 is fresh at 15:00, but 15:00 is the owner's line.
        os_ = orders()
        risk = risk_for(os_, at="2026-11-11 14:50")
        self.assertTrue(self.status(os_, risk=risk, now="2026-11-11 14:59")[0])
        ready, label, _ = self.status(os_, risk=risk, now="2026-11-11 15:00")
        self.assertEqual(label, "NOT READY: no new entries from 15:00 India time (it is 15:00)")
        risk = risk_for(os_, at="2026-11-11 10:16", entries_until="10:20")
        for r in risk["results"]:
            r["entries_until"] = "10:20"
        self.assertIn("no new entries from 10:20", self.status(os_, risk=risk, now="2026-11-11 10:20")[1])

    def test_window_now_for_entries_and_exits(self):
        e, s = orders()
        w = lambda o, kind, at: ticket.window_now(o, {"kind": kind}, {}, utc(at))
        self.assertEqual(w(e, "entry", "2026-11-11 09:29"), "no orders before 09:30 India time (it is 09:29)")
        self.assertIsNone(w(e, "entry", "2026-11-11 09:30"))
        self.assertIn("no NSE session for this pack today (weekend)", w(e, "entry", "2026-11-14 10:00"))
        self.assertIn("no NSE session", w(e, "entry", "2026-11-10 10:00"))                    # Diwali holiday
        self.assertIsNone(w(s, "exit", "2026-11-11 15:11"))                                   # fo true: 15:12
        self.assertIn("09:15 to 15:12", w(s, "exit", "2026-11-11 15:12"))
        self.assertIsNone(w(dict(s, fo=False), "exit", "2026-11-11 15:24"))
        self.assertIn("09:15 to 15:25", w(dict(s, fo=False), "exit", "2026-11-11 15:25"))
        self.assertIn("09:15 to 15:12", w(dict(s, fo=None), "exit", "2026-11-11 15:13"))      # unknown: F&O
        self.assertIn("it is 09:14", w(s, "exit", "2026-11-11 09:14"))


# ---------------------------------------------------------------- the flow through the CLI

class Flow(Clocked):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.p = {k: os.path.join(self.d, k + ".json") for k in ("orders", "config", "risk", "state", "portfolio")}
        with open(ORDERS_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.write("orders", doc)
        self.write("config", cfg(**LIVE))
        self.write("risk", risk_for(doc["orders"]))

    def tearDown(self):
        shutil.rmtree(self.d)
        super().tearDown()

    def write(self, k, data):
        with open(self.p[k], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def read(self, k):
        with open(self.p[k], encoding="utf-8") as fh:
            return json.load(fh)

    def run_(self, *argv, files=("orders", "config", "risk", "state")):
        args = list(argv)
        for k in files:
            args += ["--" + k, self.p[k]]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ticket.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_show_the_pair(self):
        code, out, _ = self.run_("show")
        self.assertEqual(code, 0)
        for words in ("ORDER 1 of 2: LIVE, REAL MONEY (B0 recorded on 2026-11-10) (Zerodha Kite, NSE)",
                      "  BUY 32 M&M, limit 3427.00, intraday (MIS), good for today",
                      "  its stop, order 2: SELL 32 M&M, stop-loss: trigger 3384.40, limit 3367.40, intraday (MIS), "
                      "good for today; it goes in only after this buy fills, for the filled quantity",
                      "  target 3469.60: an alert, not an order",
                      "  max loss about %s1,992.76 at the stop's limit, including %s85.56 estimated charges" % (R, R),
                      "  last price %s3,428.50 (yahoo, 30 s old)" % R,
                      "  READY: trader-risk PASS on the pair, waiting for your yes",
                      '      "quantity": 32,', '      "tag": "traderpack"',
                      "  Reply \"yes\" to this pair (the buy, then its stop), or \"no\" to skip it.",
                      "  The 3 pm close is not active (close_gate_proven_on is not recorded)",
                      "ORDER 2 of 2", "  the stop of order 1: it goes in after that entry fills"):
            self.assertIn(words, out)

    def test_dry_run_records_the_yes_and_sends_nothing(self):
        self.write("config", cfg())
        code, out, _ = self.run_("show")
        self.assertIn("ORDER 1 of 2: DRY RUN, nothing is sent (Zerodha Kite, NSE)", out)
        self.assertIn("Dry run: after the yes, Claude does not call place_order.", out)
        code, out, _ = self.run_("approve", "--index", "1", "--reply", "yes")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN, nothing is sent. Do not call place_order. This is the input it would get:", out)
        self.assertNotIn("Now call place_order", out)
        rec = self.read("state")["orders"][-1]
        self.assertEqual((rec["status"], rec["mode"]), ("dry_run", "dry_run"))
        code, out, _ = self.run_("show")
        self.assertIn("DONE: dry_run at 2026-11-11T10:17:00+05:30", out)
        self.assertEqual(self.run_("stop", "--pair", PAIR, "--filled-qty", "32")[0], 1)
        code, _, err = self.run_("result", "--index", "1", "--status", "placed", "--order-id", "1")
        self.assertIn("order 1 has no live yes recorded today", err)       # a dry run has nothing to record

    def placed_entry(self, order_id="251111000000001"):
        self.run_("approve", "--index", "1", "--reply", "yes")
        return self.run_("result", "--index", "1", "--status", "placed", "--order-id", order_id)

    def test_live_flow_entry_then_stop_for_the_filled_quantity(self):
        code, out, _ = self.run_("approve", "--index", "1", "--reply", "yes, M&M")
        self.assertEqual(code, 0, out)
        self.assertIn("Recorded your yes for order 1 and its stop (order 2)", out)
        self.assertIn("Now call place_order with exactly:", out)
        self.assertIn("Never retry a failed call", out)
        self.assertIn("ticket.py stop --pair %s --filled-qty Q" % PAIR, out)
        self.assertIn("The 3 pm close is not active", out)
        rec = self.read("state")["orders"][-1]
        self.assertEqual((rec["status"], rec["kind"], rec["max_loss"], rec["risk_check"], rec["symbol"],
                          rec["transaction_type"]), ("approved", "entry", 1992.76, "chk1", "M&M", "BUY"))
        self.assertEqual(rec["stop_leg"], orders()[1])                     # R38: the stop the yes covers
        out = self.run_("show")[1]
        self.assertIn("APPROVED by you at", out)
        self.assertNotIn("UNPROTECTED", out)                               # not placed yet
        code, _, err = self.run_("result", "--status", "placed", "--order-id", "x")
        self.assertIn("say which: --index N (an entry or exit in orders.json) or --pair P (its stop)", err)
        # the stop cannot go before the entry is recorded as placed
        code, _, err = self.run_("stop", "--pair", PAIR, "--filled-qty", "32")
        self.assertEqual(code, 1)
        self.assertIn("has no entry recorded as placed today", err)
        code, _, err = self.run_("result", "--index", "1", "--status", "placed")
        self.assertIn("placed needs --order-id", err)
        code, out, _ = self.run_("result", "--index", "1", "--status", "placed", "--order-id", "251111000000001")
        self.assertEqual(code, 0)
        self.assertIn("UNPROTECTED: M&M, entry order_id 251111000000001 (pair %s): stops cover 0 of 32; whatever has "
                      "filled beyond that has no stop. When get_orders shows a fill: ticket.py stop --pair %s "
                      "--filled-qty Q" % (PAIR, PAIR), out)
        for bad in ("33", "0", "20.5"):
            self.assertEqual(self.run_("stop", "--pair", PAIR, "--filled-qty", bad)[0], 1, bad)
        self.assertEqual(self.run_("stop", "--pair", "OTHER", "--filled-qty", "20")[0], 1)
        code, out, _ = self.run_("stop", "--pair", PAIR, "--filled-qty", "20")
        self.assertEqual(code, 0)
        self.assertIn('"quantity": 20,', out)
        self.assertIn('"trigger_price": 3384.4', out)
        self.assertIn("Stops then cover 20 of 32", out)
        self.assertEqual(self.read("state")["orders"][-1]["status"], "stop_ready")
        self.assertIn("stops cover 20 of 32", self.run_("show")[1])         # a prepared stop may be at the broker
        code, _, err = self.run_("stop", "--pair", PAIR, "--filled-qty", "12")
        self.assertIn("has no result yet", err)                            # one prepared stop at a time
        code, _, err = self.run_("result", "--index", "2", "--status", "placed", "--order-id", "x")
        self.assertIn("order 2 is a stop: record its result with --pair %s" % PAIR, err)
        code, out, _ = self.run_("result", "--pair", PAIR, "--status", "placed", "--order-id", "251111000000002")
        self.assertEqual(code, 0)
        self.assertIn("stops cover 20 of 32", self.run_("show")[1])         # R37: the 12 left still have no stop
        code, _, err = self.run_("stop", "--pair", PAIR, "--filled-qty", "13")
        self.assertIn("stops cover 20 of 32, so 1 to 12", err)
        self.assertEqual(self.run_("stop", "--pair", PAIR, "--filled-qty", "12")[0], 0)
        self.run_("result", "--pair", PAIR, "--status", "placed", "--order-id", "251111000000003")
        self.assertNotIn("UNPROTECTED", self.run_("show")[1])
        code, _, err = self.run_("stop", "--pair", PAIR, "--filled-qty", "1")
        self.assertIn("stops already cover all 32 shares of pair %s" % PAIR, err)
        code, _, err = self.run_("result", "--pair", PAIR, "--status", "placed", "--order-id", "again")
        self.assertIn("has no stop prepared (ticket.py stop) without a result today", err)
        placed = [r for r in self.read("state")["orders"] if r["status"] == "placed"]
        self.assertEqual([(r["leg"], r["quantity"], r["order_id"], r.get("stop_id")) for r in placed],
                         [("entry", 32, "251111000000001", None),
                          ("stop", 20, "251111000000002", PAIR + "/stop-1"),
                          ("stop", 12, "251111000000003", PAIR + "/stop-2")])

    def test_a_stop_that_did_not_go_in_covers_nothing(self):
        self.placed_entry()
        for status in ("failed", "denied", "unsent"):
            self.assertEqual(self.run_("stop", "--pair", PAIR, "--filled-qty", "32")[0], 0, status)
            self.run_("result", "--pair", PAIR, "--status", status)
            self.assertIn("stops cover 0 of 32", self.run_("show")[1], status)

    def test_the_stop_lives_in_the_log_not_in_orders_json(self):
        # R38: flatten (or a new plan) replaces orders.json; the stop the yes covered is still there, unedited.
        self.placed_entry()
        doc = self.read("orders")
        doc["orders"][1]["trigger_price"] = 3300.0                         # edited after the yes
        self.write("orders", doc)
        code, out, _ = self.run_("stop", "--pair", PAIR, "--filled-qty", "10")
        self.assertIn('"trigger_price": 3384.4', out)
        self.assertNotIn("3300", out)
        self.run_("result", "--pair", PAIR, "--status", "placed", "--order-id", "S1")
        self.write("orders", {"orders": [dict(orders()[0], tradingsymbol="SBIN", pair="SBIN-x", leg="exit",
                                              transaction_type="SELL")]})
        self.assertIn("UNPROTECTED: M&M, entry order_id 251111000000001 (pair %s): stops cover 10 of 32" % PAIR,
                      self.run_("show")[1])
        code, out, _ = self.run_("stop", "--pair", PAIR, "--filled-qty", "22")
        self.assertEqual(code, 0)
        self.assertIn('"quantity": 22,', out)
        self.assertIn('"trigger_price": 3384.4', out)

    def test_a_real_position_keeps_its_stop_after_a_switch_to_dry_run(self):
        self.placed_entry()
        self.write("config", cfg())                                       # the owner switches back to dry run
        code, out, _ = self.run_("stop", "--pair", PAIR, "--filled-qty", "32")
        self.assertEqual(code, 0)
        self.assertIn('"quantity": 32,', out)

    def test_the_stop_waits_for_the_exit_session(self):
        self.placed_entry()
        self.set_clock("2026-11-11 15:12")
        code, _, err = self.run_("stop", "--pair", PAIR, "--filled-qty", "32")
        self.assertEqual(code, 1)
        self.assertIn("the stop cannot go in now: outside the session for an exit (09:15 to 15:12", err)

    def test_a_yes_expires_and_a_new_check_holds_it(self):
        self.run_("approve", "--index", "1", "--reply", "yes")
        self.set_clock("2026-11-11 10:32")
        self.assertIn("EXPIRED: your yes at 2026-11-11T10:17:00+05:30 no longer holds (the risk check is 16 "
                      "minutes old", self.run_("show")[1])
        self.write("risk", risk_for(self.read("orders")["orders"], at="2026-11-11 10:31", check_id="chk2"))
        self.assertIn("NOT READY: your yes at 2026-11-11T10:17:00+05:30 was on an earlier check",
                      self.run_("show")[1])
        self.run_("result", "--index", "1", "--status", "unsent")
        self.assertIn("READY: trader-risk PASS on the pair", self.run_("show")[1])

    def test_refusals(self):
        code, out, _ = self.run_("approve", "--index", "1", "--reply", "sure")
        self.assertEqual(code, 1)
        self.assertIn("Not recorded: only a reply that starts with \"yes\" counts", out)
        code, _, err = self.run_("approve", "--index", "2", "--reply", "yes")
        self.assertIn("order 2 is not ready: the stop of order 1", err)
        self.set_clock("2026-11-11 15:00")
        self.write("risk", risk_for(self.read("orders")["orders"], at="2026-11-11 14:59"))
        code, _, err = self.run_("approve", "--index", "1", "--reply", "yes")
        self.assertIn("no new entries from 15:00 India time", err)
        code, _, err = self.run_("result", "--index", "1", "--status", "placed", "--order-id", "x")
        self.assertIn("order 1 has no live yes recorded today", err)

    def test_a_failed_call_is_never_retried(self):
        self.run_("approve", "--index", "1", "--reply", "yes")
        code, out, _ = self.run_("result", "--index", "1", "--status", "failed")
        self.assertIn("Do not retry. Read get_orders: its status_message says why", out)

    def test_there_is_no_pretend_clock(self):
        # R36: the yes is judged on the real clock only.
        with redirect_stderr(io.StringIO()):
            for cmd in ("show", "approve", "result", "stop", "log", "fill", "flatten"):
                with self.assertRaises(SystemExit, msg=cmd):
                    ticket.main([cmd, "--now", "2026-11-11 10:16"])

    def test_json_ticket(self):
        code, out, _ = self.run_("show", "--index", "1", "--json")
        t = json.loads(out)
        self.assertEqual((t["ready"], t["mode"], t["tool"], t["stop_index"], t["max_loss"], t["close_gate"]),
                         (True, "live", "place_order", 2, 1992.76, False))
        self.assertEqual(t["tool_input"]["quantity"], 32)

    def test_log(self):
        self.run_("approve", "--index", "1", "--reply", "yes")
        self.run_("result", "--index", "1", "--status", "placed", "--order-id", "77")
        code, out, _ = self.run_("log", files=("state",))
        self.assertIn('#1 approved   BUY 32 M&M, limit 3427.00, intraday (MIS), good for today reply "yes"', out)
        self.assertIn("#1 placed     BUY 32 M&M, limit 3427.00, intraday (MIS), good for today max loss %s1,992.76 "
                      "order_id 77" % R, out)
        self.assertIn("1 placed today.", out)


# ---------------------------------------------------------------- fills, R35 and flatten

class Positions(Clocked):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "portfolio.json")
        self.st = os.path.join(self.d, "broker.json")
        self.ords = os.path.join(self.d, "orders.json")
        self.cfgp = os.path.join(self.d, "config.json")
        with open(self.cfgp, "w") as fh:
            json.dump(cfg(**LIVE), fh)

    def tearDown(self):
        shutil.rmtree(self.d)
        super().tearDown()

    def run_(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ticket.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def fill(self, side, qty, price="3427", date=None):
        args = ["fill", "--symbol", "M&M", "--side", side, "--qty", qty, "--price", price, "--portfolio", self.pf,
                "--state", self.st, "--config", self.cfgp]
        return self.run_(*(args + (["--date", date] if date else [])))

    def test_a_fill_is_todays_mis_position(self):
        self.assertEqual(self.fill("buy", "32")[0], 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["positions"], [{"tradingsymbol": "M&M", "exchange": "NSE", "product": "MIS",
                                            "date": TODAY, "quantity": 32, "average_price": 3427.0}])
        self.assertEqual(pf["cash"], 200000 - 109664.0)
        # R35 end to end: trader-risk sees today's MIS position as held, and yesterday's as not
        sell = dict(orders()[1], order_type="LIMIT", trigger_price=None, price=3400.0, leg="exit")
        r = check.check([sell], cfg(), pf, {}, None, None, utc("2026-11-11 10:30"))["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"))
        r = check.check([sell], cfg(), pf, {}, None, None, utc("2026-11-12 10:30"))["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("entry", "BLOCK"))
        self.assertEqual(self.fill("sell", "40")[0], 1)
        self.assertEqual(self.fill("sell", "32", "3460")[0], 0)
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["positions"], [])
        self.assertEqual(self.fill("buy", "32.5")[0], 1)

    def test_flatten_writes_a_limit_sell_on_the_tick(self):
        uni = os.path.join(self.d, "universe.txt")
        with open(uni, "w") as fh:
            fh.write("M&M|Mahindra & Mahindra|0.1|FO\n")
        base = ["flatten", "--symbol", "M&M", "--orders", self.ords, "--portfolio", self.pf, "--config", self.cfgp,
                "--universe", uni]
        code, _, err = self.run_(*(base + ["--price", "3460.5"]))
        self.assertIn("nothing to flatten: no MIS position of M&M today", err)
        self.fill("buy", "32")
        self.fill("buy", "5", date="2026-11-10")              # yesterday's: not today's position
        code, _, err = self.run_(*(base + ["--price", "3460.55"]))
        self.assertIn("--price 3460.55 is off the 0.1 tick", err)
        code, out, _ = self.run_(*(base + ["--price", "3460.5"]))
        self.assertEqual(code, 0, out)
        with open(self.ords, encoding="utf-8") as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual(ticket.tool_input(o), {
            "variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "SELL",
            "quantity": 32, "product": "MIS", "order_type": "LIMIT", "price": 3460.5, "validity": "DAY",
            "tag": "traderpack"})
        self.assertEqual((o["leg"], o["fo"], o["tick"]), ("exit", True, 0.1))
        code, _, err = self.run_(*(base[:-2] + ["--universe", "none.txt", "--price", "3460.5"]))
        self.assertIn("no tick size for M&M: pass --tick", err)

    def test_an_exit_cancels_the_stop_first(self):
        os_ = orders()
        def stop(sid, status, order_id=None, at="2026-11-11T10:40:00+05:30"):
            r = {"date": TODAY, "at": at, "status": status, "leg": "stop", "symbol": "M&M", "pair": os_[0]["pair"],
                 "stop_id": sid, "quantity": 10}
            if order_id:
                r["order_id"] = order_id
            return r
        # R37: two stops placed, a third prepared with no result, a fourth that failed
        state = {"orders": [stop("p/stop-1", "stop_ready"), stop("p/stop-1", "placed", "S9"),
                            stop("p/stop-2", "stop_ready"), stop("p/stop-2", "placed", "S10"),
                            stop("p/stop-3", "stop_ready", at="2026-11-11T10:50:00+05:30"),
                            stop("p/stop-4", "stop_ready"), stop("p/stop-4", "failed")]}
        with open(self.st, "w") as fh:
            json.dump(state, fh)
        self.fill("buy", "32")
        with open(self.st) as fh:                              # the fill appended to the log
            state = json.load(fh)
        exit_ = dict(os_[1], order_type="LIMIT", trigger_price=None, price=3460.5, leg="exit", pair=None)
        with open(self.ords, "w", encoding="utf-8") as fh:
            json.dump({"orders": [exit_]}, fh)
        with open(self.pf) as fh:
            pf = json.load(fh)
        res = check.check([exit_], cfg(), pf, {}, None, None, utc("2026-11-11 10:16"))
        res.update(checked_at="2026-11-11T04:46:00Z", check_id="c")
        rp = os.path.join(self.d, "risk.json")
        with open(rp, "w", encoding="utf-8") as fh:
            json.dump(res, fh)
        code, out, err = self.run_("approve", "--index", "1", "--reply", "yes", "--orders", self.ords, "--config",
                                   self.cfgp, "--risk", rp, "--state", self.st)
        self.assertEqual(code, 0, err)
        self.assertIn("First cancel every stop this pack placed for M&M today, so none can fire after this sell and "
                      "open a short: cancel_order with variety regular, order_id S9, then order_id S10.", out)
        self.assertIn("A stop prepared at 2026-11-11T10:50:00+05:30 has no result recorded: look in get_orders for "
                      "it, and cancel it too if it is there.", out)
        self.assertIn("Then:" + chr(10) + "Now call place_order with exactly:", out)
        self.assertNotIn("The 3 pm close is not active", out)            # an exit, not an entry


if __name__ == "__main__":
    unittest.main()
