import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
guard = load("trader-broker", "guard")
closer = load("trader-broker", "closer")

TODAY = "2026-11-11"                                   # a Wednesday, an NSE trading day
PAIR = "M&M-2026-11-11-101600"
TAG = "traderpack"


def utc(india):
    return datetime.datetime.strptime(india, "%Y-%m-%d %H:%M") - datetime.timedelta(hours=5, minutes=30)


def stamp(india):
    return utc(india).strftime("%Y-%m-%dT%H:%M:%SZ")


def config(**over):
    with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
        c = json.load(fh)
    c.update(over)
    return c


UNLOCKED = dict(dry_run=False, b0_proven_on="2026-11-10")
ACTIVE = dict(UNLOCKED, close_gate_proven_on="2026-11-10")


def kite_order(**over):
    o = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "BUY", "quantity": 32,
         "product": "MIS", "order_type": "LIMIT", "price": 3427.0, "validity": "DAY", "tag": TAG}
    o.update(over)
    return o


STOP = kite_order(transaction_type="SELL", order_type="SL", price=3367.4, trigger_price=3384.4)


class Project(unittest.TestCase):
    """A temporary project folder: the guard registered, a config, a broker log."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, ".claude"))
        shutil.copy(os.path.join(ROOT, "trader-broker", "settings.example.json"),
                    os.path.join(self.root, ".claude", "settings.json"))
        self.cfg(**ACTIVE)
        self.state({"orders": []})

    def tearDown(self):
        shutil.rmtree(self.root)

    def put(self, rel, doc):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            if isinstance(doc, str):
                fh.write(doc)
            else:
                json.dump(doc, fh, ensure_ascii=False)

    def get(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return json.load(fh)

    def cfg(self, **c):
        self.put("trader/config.json", config(**c))

    def state(self, doc):
        self.put("trader/state/broker.json", doc)

    def rec(self, order, status, at="2026-11-11T10:17:00+05:30", **extra):
        r = {"at": at, "date": TODAY, "fingerprint": ticket.fingerprint(order), "pair": PAIR,
             "leg": extra.pop("leg", "entry"), "symbol": order["tradingsymbol"],
             "transaction_type": order["transaction_type"], "quantity": order["quantity"], "status": status,
             "fo": extra.pop("fo", True)}
        if status == "approved":                       # R44: approve copies the risk check's time (10:16 India)
            r["risk_checked_at"] = extra.pop("risk_checked_at", "2026-11-11T04:46:00Z")
        r.update(extra)
        return r

    def kite(self, filled=32, order_id="E1", at="2026-11-11 10:19"):
        """R43: get_orders as saved by closer.py save orders."""
        self.put("trader/kite/orders.json", {"saved_at": stamp(at), "data": [
            {"order_id": order_id, "tradingsymbol": "M&M", "transaction_type": "BUY", "quantity": 32,
             "filled_quantity": filled, "status": "COMPLETE" if filled == 32 else "OPEN", "tag": TAG}]})


# ---------------------------------------------------------------- the guard

class Guard(Project):
    def setUp(self):
        super().setUp()
        self.kite()
    def go(self, tool, ti, at="2026-11-11 10:20"):
        return guard.decide({"tool_name": "mcp__kite__" + tool, "tool_input": ti}, self.root, utc(at))

    def blocked(self, words, *args, **kw):
        ok, why = self.go(*args, **kw)
        self.assertFalse(ok, why)
        self.assertIn(words, why)

    def test_modify_unknown_and_bad_input_always_block(self):
        self.blocked("nothing in this pack modifies an order", "modify_order", {"order_id": "1"})
        self.blocked("unknown tool", "place_gtt_order", {})
        self.assertFalse(guard.decide(None, self.root, utc("2026-11-11 10:20"))[0])
        os.remove(os.path.join(self.root, "trader", "config.json"))
        self.blocked("trader/config.json is missing", "place_order", kite_order())

    def test_the_kite_shape(self):
        self.state({"orders": [self.rec(kite_order(), "approved")]})
        for over, words in (({"order_type": "MARKET"}, "LIMIT and SL only"), ({"product": "CNC"}, "product must be"),
                            ({"exchange": "BSE"}, "exchange must be"), ({"variety": "amo"}, "variety must be"),
                            ({"quantity": "32"}, "not a whole JSON number"), ({"quantity": 32.5}, "not a whole"),
                            ({"price": "3427"}, "price '3427' is not a JSON number"), ({"tag": None}, "tag None"),
                            ({"pair": PAIR}, "fields this pack never sends: pair")):
            self.blocked(words, "place_order", kite_order(**over))

    def test_locked_blocks_everything_but_the_stop_of_a_real_position(self):
        # R14 with Fable's B.6 ruling: dry run, or no b0_proven_on.
        for c in ({}, dict(dry_run=False), dict(dry_run=False, b0_proven_on="2026-11-12"), dict(dry_run="false"),
                  dict(dry_run=True, b0_proven_on="2026-11-10"), dict(dry_run="false", b0_proven_on="2026-11-10")):
            self.cfg(**c)
            self.state({"orders": [self.rec(kite_order(), "approved")]})
            self.blocked("real orders are locked", "place_order", kite_order())
        stop20 = dict(STOP, quantity=20)
        self.state({"orders": [self.rec(kite_order(), "approved"), self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(stop20, "stop_ready", leg="stop", stop_id=PAIR + "/stop-1")]})
        ok, why = self.go("place_order", stop20)
        self.assertTrue(ok, why)
        self.assertIn("the stop of a placed entry (%s/stop-1)" % PAIR, why)
        self.blocked("was already sent once", "place_order", stop20)                  # once
        self.blocked("real orders are locked", "place_order", dict(stop20, quantity=21))  # not exactly it
        self.blocked("real orders are locked", "place_order", kite_order(transaction_type="SELL", quantity=20))

    def test_a_stop_needs_its_entry_placed_with_an_order_id(self):
        stop20 = dict(STOP, quantity=20)
        for entry in (self.rec(kite_order(), "approved"), self.rec(kite_order(), "placed")):
            os.makedirs(os.path.join(self.root, "trader", "state"), exist_ok=True)
            self.state({"orders": [entry, self.rec(stop20, "stop_ready", leg="stop", stop_id="s1")]})
            self.blocked("not a prepared stop", "place_order", stop20)
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(stop20, "stop_ready", leg="stop", stop_id="s1")]})
        self.blocked("outside the session for a stop", "place_order", stop20, at="2026-11-11 15:12")
        self.blocked("outside the session for a stop", "place_order", stop20, at="2026-11-11 09:14")

    def test_an_approved_buy_inside_the_window_once(self):
        self.state({"orders": [self.rec(kite_order(), "approved")]})
        self.blocked("a BUY only from 09:30 to 15:00", "place_order", kite_order(), at="2026-11-11 09:29")
        self.blocked("the risk check behind your yes (10:16 India time) is more than 15 minutes old", "place_order",
                     kite_order(), at="2026-11-11 10:32")                        # R44: from the check, not the yes
        ok, why = self.go("place_order", kite_order())
        self.assertTrue(ok, why)
        self.blocked("was already sent once", "place_order", kite_order())
        self.state({"orders": [self.rec(kite_order(), "approved", at="2026-11-11T14:55:00+05:30")]})
        self.blocked("a BUY only from 09:30 to 15:00 India time (it is 15:00)", "place_order", kite_order(),
                     at="2026-11-11 15:00")
        self.blocked("no yes for exactly this BUY", "place_order", kite_order(quantity=31))
        self.state({"orders": [self.rec(kite_order(), "approved"), self.rec(kite_order(), "placed", order_id="E1")]})
        self.blocked("no yes for exactly this BUY", "place_order", kite_order())
        self.state({"orders": [self.rec(kite_order(), "approved", date="2026-11-14",
                                        at="2026-11-14T10:17:00+05:30")]})
        self.blocked("no NSE session", "place_order", kite_order(), at="2026-11-14 10:20")

    def positions(self, qty=32, at="2026-11-11 15:30"):
        self.put("trader/kite/positions.json", {"saved_at": stamp(at), "data": {"net": [
            {"tradingsymbol": "M&M", "exchange": "NSE", "product": "MIS", "quantity": qty, "last_price": 3460.0}]}})

    def test_an_approved_exit_against_fresh_positions(self):
        sell = kite_order(transaction_type="SELL", price=3460.5)
        self.state({"orders": [self.rec(sell, "approved", leg="exit", pair=None, fo=False)]})
        self.blocked("positions.json is missing", "place_order", sell, at="2026-11-11 15:20")
        self.positions(at="2026-11-11 15:14")
        self.blocked("not saved in the last 5 minutes", "place_order", sell, at="2026-11-11 15:20")
        self.positions(qty=31, at="2026-11-11 15:19")
        self.blocked("selling 32 M&M with 31 held", "place_order", sell, at="2026-11-11 15:20")
        self.positions(at="2026-11-11 15:24")
        self.blocked("outside the session for an exit", "place_order", sell, at="2026-11-11 15:25")
        ok, why = self.go("place_order", sell, at="2026-11-11 15:24")                 # fo false: until 15:25
        self.assertTrue(ok, why)

    def plan(self, written="2026-11-11 15:01", fo=False, date=TODAY):
        ti = kite_order(transaction_type="SELL", price=3411.3, validity="IOC")
        self.put("trader/close-plan.json", {"date": date, "written_at": stamp(written), "check_id": "c",
                                            "retry": False, "lines": [{"line": 1, "tradingsymbol": "M&M",
                                                                       "quantity": 32, "fo": fo, "tool_input": ti,
                                                                       "used": None}]})
        return ti

    def test_a_3pm_line_once_inside_its_window(self):
        ti = self.plan(written="2026-11-11 14:58")
        self.blocked("outside the close window", "place_order", ti, at="2026-11-11 14:59")
        ti = self.plan()
        ok, why = self.go("place_order", ti, at="2026-11-11 15:02")
        self.assertEqual((ok, why), (True, "3 pm close line 1"))
        self.assertEqual(self.get("trader/close-plan.json")["lines"][0]["used"], "2026-11-11T15:02:00+05:30")
        self.blocked("line 1 was already used", "place_order", ti, at="2026-11-11 15:03")
        ti = self.plan(written="2026-11-11 15:08", fo=True)
        self.blocked("outside the close window for this line (15:00 to 15:10", "place_order", ti,
                     at="2026-11-11 15:10")
        ti = self.plan(written="2026-11-11 15:08", fo=None)                              # unknown counts as F&O
        self.blocked("15:00 to 15:10", "place_order", ti, at="2026-11-11 15:10")
        ti = self.plan(written="2026-11-11 15:08")
        self.assertTrue(self.go("place_order", ti, at="2026-11-11 15:10")[0])
        ti = self.plan(written="2026-11-11 15:01")
        self.blocked("not written in the last 5 minutes", "place_order", ti, at="2026-11-11 15:07")
        ti = self.plan(date="2026-11-10")
        self.blocked("is not from today", "place_order", ti, at="2026-11-11 15:02")
        ti = self.plan()
        self.blocked("not a prepared stop, a 3 pm close line", "place_order", dict(ti, price=3400.0),
                     at="2026-11-11 15:02")

    def test_the_3pm_gates(self):
        ti = self.plan()
        self.cfg(**UNLOCKED)
        self.blocked("the 3 pm close is not active: close_gate_proven_on is not recorded (R17)", "place_order", ti,
                     at="2026-11-11 15:02")
        self.cfg(**dict(ACTIVE, close_gate_proven_on="2026-11-12"))
        self.blocked("(R17)", "place_order", ti, at="2026-11-11 15:02")
        self.cfg(close_gate_proven_on="2026-11-10")
        self.blocked("real orders are locked", "place_order", ti, at="2026-11-11 15:02")

    def test_cancels(self):
        self.put("trader/close-cancels.json", {"date": TODAY, "written_at": stamp("2026-11-11 15:01"),
                                               "lines": [{"line": 1, "order_id": "E9", "fo": True, "used": None}]})
        ok, why = self.go("cancel_order", {"variety": "regular", "order_id": "E9"}, at="2026-11-11 15:02")
        self.assertEqual((ok, why), (True, "3 pm cancel line 1"))
        self.blocked("already used", "cancel_order", {"variety": "regular", "order_id": "E9"}, at="2026-11-11 15:03")
        self.blocked("is not a 3 pm cancel line", "cancel_order", {"variety": "regular", "order_id": "X1"})
        self.blocked("needs a regular order_id", "cancel_order", {"variety": "regular"})
        p = self.get("trader/close-cancels.json")
        p["lines"][0]["used"] = None
        self.put("trader/close-cancels.json", p)
        self.cfg(**dict(ACTIVE, dry_run=True))                                          # R14: locked
        self.blocked("real orders are locked (R14)", "cancel_order", {"variety": "regular", "order_id": "E9"},
                     at="2026-11-11 15:02")
        self.cfg(**ACTIVE)
        sell = kite_order(transaction_type="SELL", price=3460.5)
        s = self.rec(STOP, "placed", leg="stop", stop_id="s1", order_id="S7")
        self.state({"orders": [s, self.rec(sell, "approved", leg="exit", pair=None)]})
        ok, why = self.go("cancel_order", {"variety": "regular", "order_id": "S7"})
        self.assertTrue(ok, why)
        self.state({"orders": [s]})                                                    # no exit approved
        self.blocked("is not a 3 pm cancel line", "cancel_order", {"variety": "regular", "order_id": "S7"})

    def test_the_15_minutes_run_from_the_risk_check(self):
        # R44: a yes at 10:30 on a 10:16 check is good until 10:31, not 10:45; a yes without the time is refused.
        self.state({"orders": [self.rec(kite_order(), "approved", at="2026-11-11T10:30:00+05:30")]})
        ok, why = self.go("place_order", kite_order(), at="2026-11-11 10:31")
        self.assertTrue(ok, why)
        rec = self.rec(kite_order(), "approved")
        del rec["risk_checked_at"]
        self.state({"orders": [rec]})
        self.blocked("your yes carries no risk check time", "place_order", kite_order())

    def test_numbers_match_by_value(self):
        # R39: orders.json says 3427.0; the hook's JSON may say 3427 (and the reverse).
        self.state({"orders": [self.rec(kite_order(price=3427.0), "approved")]})
        ok, why = self.go("place_order", kite_order(price=3427))
        self.assertTrue(ok, why)
        self.state({"orders": [self.rec(kite_order(price=3427), "approved", at="2026-11-11T10:18:00+05:30")]})
        ok, why = self.go("place_order", kite_order(price=3427.0, quantity=32.0))
        self.assertTrue(ok, why)
        stop = dict(STOP, quantity=20, price=3367.0, trigger_price=3384.0)
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(stop, "stop_ready", leg="stop", stop_id="s1")]})
        ok, why = self.go("place_order", dict(stop, price=3367, trigger_price=3384))
        self.assertTrue(ok, why)
        self.assertEqual(ticket.fingerprint(kite_order(price=3427)), ticket.fingerprint(kite_order(price=3427.0)))
        self.assertNotEqual(ticket.fingerprint(kite_order(price=3427)),
                            ticket.fingerprint(kite_order(price="3427")))                # text stays text

    def test_a_stop_never_covers_more_than_kite_has_filled(self):
        # R43, locked or not: the stops for the pair against the entry's filled_quantity in a fresh get_orders.
        stop20 = dict(STOP, quantity=20)
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(stop20, "stop_ready", leg="stop", stop_id="s1")]})
        self.kite(filled=10)
        self.blocked("the stops would cover 20 shares, but the entry (order_id E1) has filled 10", "place_order",
                     stop20)
        self.kite(filled=32, at="2026-11-11 10:14")
        self.blocked("was not saved in the last 5 minutes", "place_order", stop20)
        self.kite(filled=32, order_id="OTHER")
        self.blocked("the entry's order_id E1 is not in the saved get_orders reply", "place_order", stop20)
        self.cfg(**UNLOCKED)
        self.kite(filled=10)
        self.blocked("the stops would cover 20 shares", "place_order", stop20)
        self.kite(filled=20)
        self.assertTrue(self.go("place_order", stop20)[0])

    def test_an_exit_waits_for_the_stops_to_be_cancelled(self):
        # R41: the exit is blocked while a stop this pack placed for the symbol has no cancel through the guard.
        sell = kite_order(transaction_type="SELL", price=3460.5)
        s7 = self.rec(STOP, "placed", leg="stop", stop_id="s1", order_id="S7")
        self.state({"orders": [s7, self.rec(sell, "approved", leg="exit", pair=None, fo=False)]})
        self.put("trader/kite/positions.json", {"saved_at": stamp("2026-11-11 10:19"), "data": {"net": [
            {"tradingsymbol": "M&M", "exchange": "NSE", "product": "MIS", "quantity": 32}]}})
        self.blocked("cancel this pack's stop for M&M first (cancel_order, order_id S7)", "place_order", sell)
        ok, why = self.go("cancel_order", {"variety": "regular", "order_id": "S7"})
        self.assertTrue(ok, why)
        self.assertEqual(self.get("trader/state/guard.json")["cancelled"], ["S7"])
        ok, why = self.go("place_order", sell)
        self.assertTrue(ok, why)
        waiting = self.rec(dict(STOP, quantity=5), "stop_ready", leg="stop", stop_id="s2",
                           at="2026-11-11T10:18:00+05:30")
        self.state({"orders": [s7, waiting, self.rec(sell, "approved", leg="exit", pair=None, fo=False,
                                                     at="2026-11-11T10:19:00+05:30")]})
        self.blocked("a stop for M&M prepared at 2026-11-11T10:18:00+05:30 has no result recorded", "place_order",
                     sell)

    def test_the_root_is_the_project_not_the_events_cwd(self):
        # R40: an event whose cwd is an unlocked copy with a yes waiting is judged by the project's own files.
        copy = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(self.root, "trader"), os.path.join(copy, "trader"))
            with open(os.path.join(copy, "trader", "config.json"), "w") as fh:
                json.dump(config(**UNLOCKED), fh)
            with open(os.path.join(copy, "trader", "state", "broker.json"), "w") as fh:
                json.dump({"orders": [self.rec(kite_order(), "approved")]}, fh)
            self.cfg()                                                                  # the project: dry run
            env, now, stdin = os.environ.get("CLAUDE_PROJECT_DIR"), guard.real_now, sys.stdin
            os.environ["CLAUDE_PROJECT_DIR"] = self.root
            guard.real_now = lambda: utc("2026-11-11 10:20")
            sys.stdin = io.StringIO(json.dumps({"tool_name": "mcp__kite__place_order", "tool_input": kite_order(),
                                                "cwd": copy}))
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    code = guard.main()
            finally:
                sys.stdin, guard.real_now = stdin, now
                if env is None:
                    del os.environ["CLAUDE_PROJECT_DIR"]
                else:
                    os.environ["CLAUDE_PROJECT_DIR"] = env
            self.assertEqual(code, 2)
            self.assertIn("real orders are locked", err.getvalue())
        finally:
            shutil.rmtree(copy)
        env, here = os.environ.pop("CLAUDE_PROJECT_DIR", None), os.getcwd()
        os.chdir(self.root)                                                             # not where it is run from
        try:
            # the folder above trader-broker
            self.assertEqual(os.path.normcase(guard.project_root()), os.path.normcase(ROOT))
        finally:
            os.chdir(here)
            if env is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = env

    def test_the_hook_process_exits_2(self):
        # The hook as Claude Code runs it: JSON on stdin, exit 2 and the reason on stderr.
        p = subprocess.run([sys.executable, os.path.join(ROOT, "trader-broker", "guard.py")],
                           input=json.dumps({"tool_name": "mcp__kite__modify_order", "tool_input": {},
                                             "cwd": self.root}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("trader-broker guard blocked this call: nothing in this pack modifies an order", p.stderr)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "trader-broker", "guard.py")], input="not json",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)


# ---------------------------------------------------------------- the 3 pm close

class Close(Project):
    def setUp(self):
        super().setUp()
        self._now = closer.real_now
        self.put("trader/universe.txt", "M&M|Mahindra|0.1|NFO\nSBIN|SBI|0.05|FO\nINFY|Infosys|0.1|FO\n"
                                        "TCS|TCS|0.1|FO\n")
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(STOP, "placed", leg="stop", stop_id="s1", order_id="S1"),
                               self.rec(kite_order(tradingsymbol="INFY", quantity=5), "placed", order_id="I1",
                                        pair="INFY-p")]})

    def tearDown(self):
        closer.real_now = self._now
        super().tearDown()

    def run_(self, *argv, at="2026-11-11 15:01"):
        t = utc(at)
        closer.real_now = lambda: t
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = closer.main(list(argv) + ["--root", self.root])
        return code, out.getvalue() + err.getvalue()

    def save(self, what, data, at="2026-11-11 15:01"):
        self.put("trader/kite/%s.json" % what, {"saved_at": stamp(at), "data": data})

    def test_precheck(self):
        self.assertEqual(self.run_("precheck")[0], 0)
        self.assertIn("NOTHING TO DO: it is 14:59", self.run_("precheck", at="2026-11-11 14:59")[1])
        self.assertIn("not a trading day (weekend)", self.run_("precheck", at="2026-11-14 15:01")[1])
        code, out = self.run_("precheck", at="2026-11-11 15:25")
        self.assertEqual(code, 1)
        self.assertIn("MISSED: it is 15:25 India time, after 15:25. Zerodha's square-off applies", out)
        self.assertEqual(self.get("trader/state/broker.json")["orders"][-1]["status"], "close-missed")
        self.cfg(**UNLOCKED)
        code, out = self.run_("precheck")
        self.assertEqual(code, 3)
        self.assertIn("REPORT ONLY: the 3 pm close is not active (close_gate_proven_on is not recorded (R17)). This "
                      "run places and cancels nothing; it reports what is open. Zerodha squares off an open MIS "
                      "position at 15:12 (F&O list) or 15:25, for ₹50 + GST an order.", out)
        for c in ({}, dict(dry_run=False, close_gate_proven_on="2026-11-10"), dict(ACTIVE, dry_run=True)):
            self.cfg(**c)
            self.assertEqual(self.run_("precheck")[0], 3, c)
        self.cfg(**ACTIVE)
        os.remove(os.path.join(self.root, ".claude", "settings.json"))
        code, out = self.run_("precheck")
        self.assertEqual(code, 1)
        self.assertIn("Without the guard the 3 pm run does nothing", out)

    def test_save(self):
        self.put("trader/kite/orders.reply.json", [{"order_id": "1"}])
        self.assertEqual(self.run_("save", "orders")[0], 0)
        self.assertEqual(self.get("trader/kite/orders.json"),
                         {"saved_at": "2026-11-11T09:31:00Z", "data": [{"order_id": "1"}]})

    ORDERS = [
        {"order_id": "E1", "tradingsymbol": "M&M", "transaction_type": "BUY", "status": "OPEN", "quantity": 32,
         "filled_quantity": 20, "tag": TAG},
        {"order_id": "S1", "tradingsymbol": "M&M", "transaction_type": "SELL", "status": "TRIGGER PENDING",
         "quantity": 20, "filled_quantity": 0, "tag": TAG},
        {"order_id": "I1", "tradingsymbol": "INFY", "transaction_type": "BUY", "status": "OPEN", "quantity": 5,
         "filled_quantity": 0, "tag": None},
        {"order_id": "Z1", "tradingsymbol": "TCS", "transaction_type": "BUY", "status": "OPEN", "quantity": 1,
         "filled_quantity": 0, "tag": TAG},
        {"order_id": "H1", "tradingsymbol": "SBIN", "transaction_type": "BUY", "status": "OPEN", "quantity": 9,
         "filled_quantity": 0, "tag": None},
    ]

    def test_cancels_this_packs_open_orders_only(self):
        self.save("orders", {"status": "success", "data": self.ORDERS})
        code, out = self.run_("cancels")
        self.assertEqual(code, 0, out)
        lines = self.get("trader/close-cancels.json")["lines"]
        self.assertEqual([l["order_id"] for l in lines], ["E1", "S1", "I1"])
        self.assertIn("NOTE: entry BUY INFY (order_id I1): in the pack's log, tag missing", out)
        self.assertIn("NOTE: BUY 1 TCS (order_id Z1) carries the tag but is not in the pack's log: left alone", out)
        self.assertNotIn("H1", out)                                                    # someone else's order
        self.cfg(**UNLOCKED)
        os.remove(os.path.join(self.root, "trader", "close-cancels.json"))
        code, out = self.run_("cancels")
        self.assertEqual(code, 3)
        self.assertIn("Open 1: cancel_order", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "trader", "close-cancels.json")))   # R17
        self.save("orders", self.ORDERS, at="2026-11-11 14:55")
        self.assertIn("was not saved in the last 5 minutes", self.run_("cancels")[1])
        self.save("orders", [dict(self.ORDERS[0], status="COMPLETE", filled_quantity=32)])
        self.assertIn("No open order of this pack to cancel.", self.run_("cancels")[1])

    def positions(self, rows, at="2026-11-11 15:01"):
        self.save("positions", {"net": rows}, at=at)

    def pos(self, sym, qty, last=None, product="MIS"):
        p = {"tradingsymbol": sym, "exchange": "NSE", "product": product, "quantity": qty}
        if last:
            p["last_price"] = last
        return p

    def filled_orders(self):
        return [dict(self.ORDERS[0], status="CANCELLED", filled_quantity=20),
                dict(self.ORDERS[2], status="COMPLETE", filled_quantity=5)]

    def test_plan_sells_only_what_this_pack_opened(self):
        self.save("orders", self.filled_orders())
        self.save("ltp", {"NSE:M&M": {"last_price": 3428.5}}, at="2026-11-11 15:00")
        self.positions([self.pos("M&M", 32, 3429.0), self.pos("SBIN", 10, 950.0), self.pos("INFY", 5, 1600.0),
                        self.pos("TCS", 3, 4000.0, product="CNC")])
        code, out = self.run_("plan")
        self.assertEqual(code, 0, out)
        plan = self.get("trader/close-plan.json")
        self.assertEqual(len(plan["lines"]), 1)
        line = plan["lines"][0]
        # min(32 held, 20 filled by this pack); kite_ltp first, 60 s old: the 0.5 % band, down to the 0.1 tick
        self.assertEqual(line["tool_input"], {"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M",
                                              "transaction_type": "SELL", "quantity": 20, "product": "MIS",
                                              "order_type": "LIMIT", "price": 3411.3, "validity": "IOC", "tag": TAG})
        self.assertEqual(line["price_label"], "₹3,428.50 (kite_ltp, 1 min old)")
        self.assertIn("NOTE: SBIN: 10 held, opened by hand (not by this pack): left alone", out)
        self.assertIn("NOTE: INFY: in the pack's log, tag missing: no sell (R16b)", out)
        self.assertNotIn("TCS", out)
        # the guard takes exactly that line
        ok, why = guard.decide({"tool_name": "mcp__kite__place_order", "tool_input": line["tool_input"]}, self.root,
                               utc("2026-11-11 15:02"))
        self.assertEqual((ok, why), (True, "3 pm close line 1"))

    def test_prices_carry_source_and_age(self):
        self.save("orders", self.filled_orders()[:1])
        self.positions([self.pos("M&M", 20, 3429.0)])
        out = self.run_("plan")[1]
        self.assertIn("₹3,429.00 (kite_positions, 0 s old)", out)          # no ltp: positions' last price
        self.assertEqual(self.get("trader/close-plan.json")["lines"][0]["band_pct"], 0.5)
        self.positions([self.pos("M&M", 20)])
        self.put("trader/prices.json", {"prices": {"M&M": 3430.0}})
        out = self.run_("plan")[1]
        self.assertIn("₹3,430.00 (yahoo, age unknown)", out)                # R16a: unknown age, wide band
        line = self.get("trader/close-plan.json")["lines"][0]
        self.assertEqual((line["band_pct"], line["tool_input"]["price"]), (1.5, 3378.5))
        self.put("trader/prices.json", {"prices": {"M&M": 3430.0},
                                        "detail": {"M&M": {"time": stamp("2026-11-11 14:50")}}})
        self.assertIn("(yahoo, 11 min old)", self.run_("plan")[1])
        self.assertEqual(self.get("trader/close-plan.json")["lines"][0]["band_pct"], 1.5)   # older than 5 minutes
        os.remove(os.path.join(self.root, "trader", "prices.json"))
        out = self.run_("plan")[1]
        self.assertIn("NOTE: M&M: no price from kite_ltp, kite_positions, yahoo: no sell (R16a)", out)

    def test_fo_stops_at_1510_and_no_tick_is_no_sell(self):
        self.save("orders", [dict(self.ORDERS[0], tradingsymbol="SBIN", status="CANCELLED", filled_quantity=9)],
                  at="2026-11-11 15:10")
        self.state({"orders": [self.rec(kite_order(tradingsymbol="SBIN", quantity=9), "placed", order_id="E1")]})
        self.positions([self.pos("SBIN", 9, 950.0)], at="2026-11-11 15:10")
        out = self.run_("plan", at="2026-11-11 15:10")[1]
        self.assertIn("NOTE: SBIN: an F&O-list stock (or unknown) after 15:10: no sell", out)
        self.put("trader/universe.txt", "")
        out = self.run_("plan", at="2026-11-11 15:10")[1]
        self.assertIn("NOTE: SBIN: no tick size", out)

    def test_report_only_writes_no_plan(self):
        self.cfg(**UNLOCKED)
        self.save("orders", self.filled_orders()[:1])
        self.positions([self.pos("M&M", 20, 3429.0)])
        code, out = self.run_("plan")
        self.assertEqual(code, 3)
        self.assertIn("Open 1: SELL 20 M&M, IOC limit", out)
        self.assertIn("REPORT ONLY", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "trader", "close-plan.json")))
        self.cfg(**ACTIVE)
        os.remove(os.path.join(self.root, ".claude", "settings.json"))                # R7
        code, out = self.run_("plan")
        self.assertEqual(code, 3)
        self.assertIn("REPORT ONLY: the 3 pm close is not active (there is no", out)

    def test_result_then_one_retry(self):
        self.save("orders", self.filled_orders()[:1])
        self.positions([self.pos("M&M", 20, 3429.0)])
        self.run_("plan")
        plan = self.get("trader/close-plan.json")
        plan["lines"][0]["used"] = "2026-11-11T15:02:00+05:30"
        self.put("trader/close-plan.json", plan)
        sell = {"order_id": "C1", "tradingsymbol": "M&M", "transaction_type": "SELL", "validity": "IOC", "tag": TAG,
                "status": "CANCELLED", "filled_quantity": 0, "order_timestamp": "2026-11-11 15:02:05"}
        old = dict(sell, order_id="OLD", status="COMPLETE", filled_quantity=20, order_timestamp="2026-11-11 14:00:00")
        self.save("orders", self.filled_orders()[:1] + [sell, old], at="2026-11-11 15:03")
        code, out = self.run_("result", at="2026-11-11 15:03")
        self.assertIn("Line 1 M&M: unfilled (0 of 20)", out)
        self.assertIn("One retry for 1 line (M&M): get_orders and get_positions again", out)
        recs = [r for r in self.get("trader/state/broker.json")["orders"] if r.get("kind") == "auto-close"]
        self.assertEqual([(r["status"], r["result"], r["quantity"]) for r in recs], [("auto-close", "unfilled", 0)])
        self.positions([self.pos("M&M", 20, 3429.0)], at="2026-11-11 15:03")
        code, out = self.run_("plan", "--retry", at="2026-11-11 15:03")
        line = self.get("trader/close-plan.json")["lines"][0]
        self.assertEqual((line["band_pct"], line["used"]), (1.5, None))
        self.assertTrue(self.get("trader/close-plan.json")["retry"])
        code, out = self.run_("plan", "--retry", at="2026-11-11 15:03")
        self.assertIn("there is one retry only", out)
        line["used"] = "x"
        p = self.get("trader/close-plan.json")
        p["lines"][0]["used"] = "x"
        self.put("trader/close-plan.json", p)
        self.save("orders", self.filled_orders()[:1] + [dict(sell, order_id="C2", status="REJECTED",
                                                             status_message="RMS: blocked",
                                                             order_timestamp="2026-11-11 15:04:00")],
                  at="2026-11-11 15:04")
        out = self.run_("result", at="2026-11-11 15:04")[1]
        self.assertIn("Line 1 M&M: rejected (0 of 20)", out)
        self.assertIn("Zerodha squares off what is still open", out)
        self.assertIn("3 pm close: SELL 0 of 20 M&M, rejected: RMS: blocked",
                      self.get("trader/state/broker.json")["orders"][-1]["plain"])

    def test_a_retry_is_only_for_what_did_not_fill(self):
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(kite_order(tradingsymbol="TCS", quantity=3), "placed", order_id="T1",
                                        pair="TCS-p")]})
        buys = [dict(self.ORDERS[0], status="COMPLETE", filled_quantity=32),
                dict(self.ORDERS[0], order_id="T1", tradingsymbol="TCS", status="COMPLETE", quantity=3,
                     filled_quantity=3)]
        self.save("orders", buys)
        self.positions([self.pos("M&M", 32, 3429.0), self.pos("TCS", 3, 4000.0)])
        self.run_("plan")
        p = self.get("trader/close-plan.json")
        for l in p["lines"]:
            l["used"] = "x"
        self.put("trader/close-plan.json", p)
        ioc = {"transaction_type": "SELL", "validity": "IOC", "tag": TAG, "order_timestamp": "2026-11-11 15:02:00"}
        self.save("orders", buys + [dict(ioc, order_id="C1", tradingsymbol="M&M", status="COMPLETE",
                                         filled_quantity=32),
                                    dict(ioc, order_id="C2", tradingsymbol="TCS", status="CANCELLED",
                                         filled_quantity=0)], at="2026-11-11 15:02")
        self.run_("result", at="2026-11-11 15:02")
        self.positions([self.pos("TCS", 3, 4000.0)], at="2026-11-11 15:02")
        self.run_("plan", "--retry", at="2026-11-11 15:02")
        self.assertEqual([l["tradingsymbol"] for l in self.get("trader/close-plan.json")["lines"]], ["TCS"])
        self.positions([self.pos("M&M", 32, 3429.0), self.pos("TCS", 3, 4000.0)], at="2026-11-11 15:02")
        p = self.get("trader/close-plan.json")
        p["retry"] = False
        for l in p["lines"]:
            l["result"] = "unfilled"
        p["lines"].append({"line": 2, "tradingsymbol": "M&M", "result": "filled"})
        self.put("trader/close-plan.json", p)
        self.run_("plan", "--retry", at="2026-11-11 15:02")
        self.assertEqual([l["tradingsymbol"] for l in self.get("trader/close-plan.json")["lines"]], ["TCS"])

    def test_plan_waits_for_the_cancels(self):
        # R42: orders read after the last cancel, and no sell while this pack has an open order for the stock.
        self.put("trader/close-cancels.json", {"date": TODAY, "written_at": stamp("2026-11-11 15:00"), "lines": [
            {"line": 1, "order_id": "S1", "used": "2026-11-11T15:01:30+05:30"}]})
        self.save("orders", self.filled_orders()[:1], at="2026-11-11 15:01")
        self.positions([self.pos("M&M", 20, 3429.0)])
        code, out = self.run_("plan", at="2026-11-11 15:02")
        self.assertEqual(code, 1)
        self.assertIn("was saved before the last cancel", out)
        still = dict(self.ORDERS[1])                                                   # the stop, still open
        self.save("orders", self.filled_orders()[:1] + [still], at="2026-11-11 15:02")
        self.positions([self.pos("M&M", 20, 3429.0)], at="2026-11-11 15:02")
        code, out = self.run_("plan", at="2026-11-11 15:02")
        self.assertIn("NOTE: M&M: this pack's order_id S1 is still open at Kite: no sell", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "trader", "close-plan.json")))
        self.save("orders", self.filled_orders()[:1] + [dict(still, status="CANCELLED")], at="2026-11-11 15:02")
        self.run_("plan", at="2026-11-11 15:02")
        self.assertEqual(len(self.get("trader/close-plan.json")["lines"]), 1)
        c = self.get("trader/close-cancels.json")
        c["lines"][0]["used"] = "soon"
        self.put("trader/close-cancels.json", c)
        code, out = self.run_("plan", at="2026-11-11 15:02")
        self.assertEqual(code, 1)
        self.assertIn("trader/close-cancels.json line 1: 'soon' is not a time", out)

    def test_old_prices_are_not_used(self):
        # R45: an LTP file over 5 minutes old is skipped; a Yahoo price from another day is no price.
        self.save("orders", self.filled_orders()[:1])
        self.save("ltp", {"NSE:M&M": {"last_price": 3000.0}}, at="2026-11-11 14:55")
        self.positions([self.pos("M&M", 20, 3429.0)])
        self.assertIn("(kite_positions, 0 s old)", self.run_("plan")[1])
        self.positions([self.pos("M&M", 20)])
        self.put("trader/prices.json", {"prices": {"M&M": 3430.0},
                                        "detail": {"M&M": {"time": stamp("2026-11-10 15:29")}}})
        self.assertIn("NOTE: M&M: no price from", self.run_("plan")[1])

    def test_a_line_not_sent_is_retried_and_a_rejected_one_is_not(self):
        # R46
        self.state({"orders": [self.rec(kite_order(), "placed", order_id="E1"),
                               self.rec(kite_order(tradingsymbol="TCS", quantity=3), "placed", order_id="T1",
                                        pair="TCS-p")]})
        buys = [dict(self.ORDERS[0], status="COMPLETE", filled_quantity=32),
                dict(self.ORDERS[0], order_id="T1", tradingsymbol="TCS", status="COMPLETE", quantity=3,
                     filled_quantity=3)]
        self.save("orders", buys)
        self.positions([self.pos("M&M", 32, 3429.0), self.pos("TCS", 3, 4000.0)])
        self.run_("plan")
        p = self.get("trader/close-plan.json")
        p["lines"][1]["used"] = "x"                                                    # TCS sent, M&M not
        self.put("trader/close-plan.json", p)
        self.save("orders", buys + [{"order_id": "C2", "tradingsymbol": "TCS", "transaction_type": "SELL",
                                     "validity": "IOC", "tag": TAG, "status": "REJECTED", "filled_quantity": 0,
                                     "order_timestamp": "2026-11-11 15:02:00"}], at="2026-11-11 15:02")
        out = self.run_("result", at="2026-11-11 15:02")[1]
        self.assertIn("One retry for 1 line (M&M)", out)
        self.assertIn("Not retried (TCS): Zerodha squares off", out)
        self.positions([self.pos("M&M", 32, 3429.0), self.pos("TCS", 3, 4000.0)], at="2026-11-11 15:02")
        self.run_("plan", "--retry", at="2026-11-11 15:02")
        self.assertEqual([l["tradingsymbol"] for l in self.get("trader/close-plan.json")["lines"]], ["M&M"])

    def test_result_filled_and_not_sent(self):
        self.save("orders", self.filled_orders()[:1])
        self.positions([self.pos("M&M", 20, 3429.0)])
        self.run_("plan")
        out = self.run_("result", at="2026-11-11 15:02")[1]
        self.assertIn("Line 1 M&M: not sent", out)
        p = self.get("trader/close-plan.json")
        p["lines"][0]["used"] = "x"
        self.put("trader/close-plan.json", p)
        self.save("orders", [{"order_id": "C1", "tradingsymbol": "M&M", "transaction_type": "SELL", "tag": TAG,
                              "validity": "IOC", "status": "COMPLETE", "filled_quantity": 20,
                              "order_timestamp": "2026-11-11 15:02:01"}], at="2026-11-11 15:02")
        out = self.run_("result", at="2026-11-11 15:02")[1]
        self.assertIn("Line 1 M&M: filled (20 of 20)", out)
        self.assertNotIn("did not fully close", out)


class SquareOffLine(unittest.TestCase):
    # 5.4: the next morning the log says who closed a day with no auto-close row.
    def test_lines(self):
        st = {"orders": [{"date": "2026-11-10", "leg": "entry", "status": "placed"},
                         {"date": "2026-11-09", "leg": "entry", "status": "placed"},
                         {"date": "2026-11-06", "leg": "entry", "status": "placed"},
                         {"date": "2026-11-06", "kind": "auto-close", "status": "auto-close"},
                         {"date": TODAY, "leg": "entry", "status": "placed"}]}
        morning = datetime.datetime(2026, 11, 11, 9, 0)
        lines = ticket.square_off_lines(st, TODAY, morning)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("2026-11-10: no auto-close row from this pack. Anything still open was "
                                            "closed by Zerodha's square-off (not by this pack"), lines[0])
        self.assertEqual([l[:10] for l in ticket.square_off_lines(st, TODAY, morning, True)],
                         ["2026-11-09", "2026-11-10"])
        d = tempfile.mkdtemp()
        try:
            sp = os.path.join(d, "broker.json")
            with open(sp, "w") as fh:
                json.dump(st, fh)
            real = ticket.real_now
            ticket.real_now = lambda: utc("2026-11-12 09:00")
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    code = ticket.main(["log", "--state", sp])
            finally:
                ticket.real_now = real
            self.assertEqual(code, 1)                                                   # nothing today yet
            self.assertIn("No decisions recorded today." + chr(10) + "2026-11-11: no auto-close row from this pack.",
                          out.getvalue())
        finally:
            shutil.rmtree(d)
        evening = datetime.datetime(2026, 11, 11, 15, 30)
        self.assertEqual([l[:10] for l in ticket.square_off_lines(st, TODAY, evening)], ["2026-11-10", TODAY])


if __name__ == "__main__":
    unittest.main()
