#!/usr/bin/env python3
"""The guard: a Claude Code PreToolUse hook for the kite MCP server's order tools. Standard library only.

It reads the hook's JSON on stdin and EXITS 2 (the call is blocked, the reason on stderr) unless the call is one
this pack allows. Exit 0 only lets the call go on to Claude's own permission card (Allow once / Deny) where one
applies; it is never a yes by itself. Registered by the owner in the project's .claude/settings.json from
settings.example.json (PLAN_INDIA_ZERODHA.md 5.3, R7).

  modify_order   always blocked: nothing in this pack modifies an order.
  place_order    the Kite shape first (regular, NSE, MIS, LIMIT or SL, a whole JSON-number quantity, the tag).
                 While real orders are locked ("dry_run" not false, or b0_proven_on not a real past date, R14):
                 blocked, except the stop of a real position (a stop prepared with ticket.py stop today, for
                 an entry recorded as placed with an order_id). Unlocked:
                   a BUY      the latest record for exactly this order is your yes (approved), within 15 minutes
                              of the risk check it was given on (R44), not sent before, and India time is 09:30
                              to 15:00 on an NSE trading day;
                   a stop     prepared with ticket.py stop today, its entry placed, inside the exit session, and
                              the pair's stops no more than the entry's filled_quantity in a get_orders reply
                              saved under 5 minutes ago (R43; locked or not);
                   an exit    an approved exit ticket, inside the exit session, every stop this pack placed for
                              the stock cancelled through the guard and none prepared without a result (R41), no
                              more than today's MIS position in trader/kite/positions.json saved under 5 minutes ago;
                   3 pm       a line of trader/close-plan.json, unused, written under 5 minutes ago today,
                              15:00 to 15:25 (15:10 for an F&O-list stock), only with close_gate_proven_on set.
  cancel_order   an order_id in an unused line of trader/close-cancels.json (same rules as the plan), or a
                 stop this pack placed today for a symbol with an approved exit ticket.

Every line of the plan or the cancels is used once; the guard marks it. Numbers match by value (3427 is 3427.0,
R39). The files are the project's (CLAUDE_PROJECT_DIR, else the folder above trader-broker/), never the event's
cwd (R40). Fails closed: unreadable input, an unknown tool, a missing file or any other shape exits 2. Known gap
(F14): a hook that times out does not block.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402  (the same folder: fingerprints, the owner's gates, the log, the India clock)

FILE_MAX_AGE_S = 300          # close-plan.json, close-cancels.json and positions.json must be this fresh
TOOLS = {"mcp__kite__place_order": "place", "mcp__kite__modify_order": "modify", "mcp__kite__cancel_order": "cancel"}


class Blocked(Exception):
    pass


def real_now():
    """The real clock, naive UTC; tests replace this function."""
    return ticket.real_now()


def _age_ok(stamp, now):
    """True when an ISO UTC stamp ("2026-11-11T09:31:00Z") is no more than FILE_MAX_AGE_S before now."""
    try:
        t = ticket.datetime.datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    age = (now - t).total_seconds()
    return -60 <= age <= FILE_MAX_AGE_S


def _read(root, rel):
    path = os.path.join(root, rel)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(root, rel, doc):
    ticket.write_json(os.path.join(root, rel), doc)


def shape(ti):
    """The Kite shape every place_order must have (fail closed)."""
    if not isinstance(ti, dict):
        raise Blocked("the tool input is not an object")
    want = {"variety": "regular", "exchange": "NSE", "product": "MIS"}
    for k, v in want.items():
        if ti.get(k) != v:
            raise Blocked("%s must be %r, got %r" % (k, v, ti.get(k)))
    if ti.get("order_type") not in ("LIMIT", "SL"):
        raise Blocked("order_type %r: LIMIT and SL only (no MARKET or SL-M)" % ti.get("order_type"))
    if ti.get("transaction_type") not in ("BUY", "SELL"):
        raise Blocked("transaction_type %r" % ti.get("transaction_type"))
    q = ti.get("quantity")
    if isinstance(q, bool) or not isinstance(q, (int, float)) or q != int(q) or q < 1:
        raise Blocked("quantity %r is not a whole JSON number of shares" % (q,))
    for k in ("price", "trigger_price"):
        if k in ti and (isinstance(ti[k], (bool, str)) or not isinstance(ti[k], (int, float))):
            raise Blocked("%s %r is not a JSON number" % (k, ti[k]))
    if ticket.B0_PROVISIONAL["tag_required"] and ti.get("tag") != ticket.B0_PROVISIONAL["tag"]:
        raise Blocked("tag %r: every order this pack places carries %r" % (ti.get("tag"), ticket.B0_PROVISIONAL["tag"]))
    if set(ti) - set(ticket.KITE_FIELDS):
        raise Blocked("fields this pack never sends: %s" % ", ".join(sorted(set(ti) - set(ticket.KITE_FIELDS))))


def unlocked(cfg, today):
    """R14: real orders only with "dry_run" false and b0_proven_on a real date not after today."""
    if (cfg or {}).get("dry_run", True) is not False:
        return False, "dry run (\"dry_run\" is not false)"
    d, why = ticket.proven_on(cfg, "b0_proven_on", today)
    return (d is not None), (why or "")


def in_session(local, fo):
    """09:15 to Zerodha's square-off for it on an NSE trading day (unknown F&O counts as F&O)."""
    if not ticket.nse_trading_day(local.date())[0]:
        return False
    last = ticket.SQUARE_OFF if fo is False else ticket.SQUARE_OFF_FO
    return ticket.NSE_OPEN <= local.time() < last


def prepared_stop(state, today, fp):
    """The stop_ready record of today for exactly this input whose stop has no result yet and whose entry is placed
    with an order_id, or None."""
    for first, last in ticket.stop_groups(state, today).values():
        if last.get("status") != "stop_ready" or last.get("fingerprint") != fp:
            continue
        e = ticket.latest(state, today, pair=first.get("pair"), leg="entry")
        if e and e.get("status") == "placed" and e.get("order_id"):
            return last, e
    return None, None


def used_key(rec):
    return "%s|%s" % (rec.get("at"), rec.get("fingerprint"))


def check_place(root, ti, cfg, state, today, now):
    shape(ti)
    fp = ticket.fingerprint(ti)
    local = ticket.to_ist(now)
    is_open, why = unlocked(cfg, today)
    stop, entry = prepared_stop(state, today, fp) if ti.get("transaction_type") == "SELL" else (None, None)
    if stop is not None:
        if ti.get("order_type") != "SL":
            raise Blocked("a stop must be an SL order")
        if not in_session(local, stop.get("fo")):
            raise Blocked("outside the session for a stop (09:15 to the square-off)")
        try:   # R43: the stops never cover more than the entry has filled, by Kite's own get_orders
            orders = ticket.kite_orders(os.path.join(root, "trader", "kite", "orders.json"), now)
        except ticket.TicketError as e:
            raise Blocked(str(e))
        filled = ticket.filled_of(orders, entry.get("order_id"))
        if filled is None:
            raise Blocked("the entry's order_id %s is not in the saved get_orders reply" % entry.get("order_id"))
        cover = ticket.covered(state, today, stop.get("pair"))
        if cover > filled:
            raise Blocked("the stops would cover %d shares, but the entry (order_id %s) has filled %d"
                          % (cover, entry.get("order_id"), filled))
        return use_once(root, stop, "the stop of a placed entry (%s)" % stop.get("stop_id"))
    if not is_open:
        raise Blocked("real orders are locked: %s. Only the stop of a real position may go (R14)" % why)
    if ti.get("transaction_type") == "BUY":
        rec = ticket.latest(state, today, fp=fp)
        if not rec or rec.get("status") != "approved" or rec.get("leg") != "entry":
            raise Blocked("no yes for exactly this BUY is waiting (ticket.py approve), or it was already recorded")
        if not ticket.nse_trading_day(local.date())[0]:
            raise Blocked("no NSE session for this pack today")
        if not ticket.ENTRIES_FROM <= local.time() < ticket.ENTRIES_UNTIL:
            raise Blocked("a BUY only from 09:30 to 15:00 India time (it is %s)" % local.strftime("%H:%M"))
        try:   # R44: the 15 minutes run from the risk check the yes was given on
            checked = ticket.datetime.datetime.strptime(str(rec.get("risk_checked_at")), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise Blocked("your yes carries no risk check time; run check.py and approve again")
        if (now - checked).total_seconds() > ticket.RISK_MAX_AGE_MIN * 60:
            raise Blocked("the risk check behind your yes (%s India time) is more than %d minutes old"
                          % (ticket.to_ist(checked).strftime("%H:%M"), ticket.RISK_MAX_AGE_MIN))
        return use_once(root, rec, "your yes at %s" % rec.get("at"))
    # a SELL: the 3 pm close, or an approved exit
    line = close_line(root, "trader/close-plan.json", "tool_input", ti, cfg, today, now)
    if line is not None:
        return "3 pm close line %s" % line
    rec = ticket.latest(state, today, fp=fp)
    if not rec or rec.get("status") != "approved" or rec.get("leg") != "exit":
        raise Blocked("this SELL is not a prepared stop, a 3 pm close line or an approved exit ticket")
    if not in_session(local, rec.get("fo")):
        raise Blocked("outside the session for an exit (09:15 to the square-off)")
    sym = ti.get("tradingsymbol")
    noted = set(guard_doc(root).get("cancelled") or [])
    live, waiting = [], []
    for first, last in ticket.stop_groups(state, today).values():
        if first.get("symbol") != sym:
            continue
        if last.get("status") == "placed" and str(last.get("order_id")) not in noted:
            live.append(str(last.get("order_id")))
        elif last.get("status") == "stop_ready":
            waiting.append(str(first.get("at")))
    if live:   # R41: a stop left at the broker would fire after this sell and open a short
        raise Blocked("cancel this pack's stop for %s first (cancel_order, order_id %s)" % (sym, ", ".join(live)))
    if waiting:
        raise Blocked("a stop for %s prepared at %s has no result recorded: look in get_orders and record it first"
                      % (sym, ", ".join(waiting)))
    pos = _read(root, "trader/kite/positions.json")
    if not isinstance(pos, dict) or not _age_ok(pos.get("saved_at"), now):
        raise Blocked("trader/kite/positions.json is missing or not saved in the last 5 minutes (get_positions)")
    held = net_mis(pos, ti.get("tradingsymbol"))
    if held < ti["quantity"]:
        raise Blocked("selling %s %s with %s held as MIS in positions.json" % (ti["quantity"], ti.get("tradingsymbol"),
                                                                             held))
    return use_once(root, rec, "your yes at %s" % rec.get("at"))


def net_mis(pos, sym):
    """Net NSE MIS quantity of sym in a saved get_positions reply ({"net": [...]} or {"data": {"net": [...]}})."""
    data = pos.get("data") if isinstance(pos.get("data"), dict) else pos
    return sum(int(p.get("quantity") or 0) for p in (data.get("net") or [])
               if isinstance(p, dict) and p.get("tradingsymbol") == sym and p.get("product") == "MIS"
               and p.get("exchange") == "NSE")


def guard_doc(root):
    """trader/state/guard.json: "used" (the yeses let through once) and "cancelled" (stops let through to cancel)."""
    doc = _read(root, "trader/state/guard.json")
    doc = doc if isinstance(doc, dict) else {}
    for k in ("used", "cancelled"):
        if not isinstance(doc.get(k), list):
            doc[k] = []
    return doc


def use_once(root, rec, what):
    """Each yes goes to the broker once: the guard keeps the yeses it let through in trader/state/guard.json."""
    doc = guard_doc(root)
    key = used_key(rec)
    if key in doc["used"]:
        raise Blocked("%s was already sent once; record what happened (ticket.py result)" % what)
    doc["used"].append(key)
    _write(root, "trader/state/guard.json", doc)
    return what


def close_line(root, rel, field, value, cfg, today, now):
    """The 3 pm files (R13, R17): an unused line whose `field` equals value, in a file of today written under 5
    minutes ago, inside the close window for that line, with real orders unlocked and close_gate_proven_on set.
    Marks it used and returns its number; None when no line matches."""
    doc = _read(root, rel)
    if not isinstance(doc, dict):
        return None
    hit = [l for l in (doc.get("lines") or []) if isinstance(l, dict) and l.get(field) == value]
    if not hit:
        return None
    line = hit[0]
    if not unlocked(cfg, today)[0]:
        raise Blocked("real orders are locked (R14)")
    if ticket.proven_on(cfg, "close_gate_proven_on", today)[0] is None:
        raise Blocked("the 3 pm close is not active: close_gate_proven_on is not recorded (R17)")
    if doc.get("date") != today or not _age_ok(doc.get("written_at"), now):
        raise Blocked("%s is not from today or not written in the last 5 minutes" % rel)
    if line.get("used"):
        raise Blocked("%s line %s was already used at %s" % (rel, line.get("line"), line.get("used")))
    local = ticket.to_ist(now)
    last = ticket.datetime.time(15, 10) if line.get("fo") is not False else ticket.SQUARE_OFF
    if not ticket.nse_trading_day(local.date())[0] or not ticket.ENTRIES_UNTIL <= local.time() < last:
        raise Blocked("outside the close window for this line (15:00 to %s India time)" % last.strftime("%H:%M"))
    line["used"] = ticket.ist_iso(now)
    _write(root, rel, doc)
    return line.get("line")


def check_cancel(root, ti, cfg, state, today, now):
    oid = str((ti or {}).get("order_id") or "")
    if not oid or (ti or {}).get("variety", "regular") != "regular":
        raise Blocked("cancel_order needs a regular order_id")
    line = close_line(root, "trader/close-cancels.json", "order_id", oid, cfg, today, now)
    if line is not None:
        return "3 pm cancel line %s" % line
    exits = {r.get("symbol") for r in state.get("orders", []) if ticket.rec_day(r) == today
             and r.get("leg") == "exit" and ticket.latest(state, today, fp=r.get("fingerprint")) is r
             and r.get("status") == "approved"}
    for first, last in ticket.stop_groups(state, today).values():
        if last.get("status") == "placed" and str(last.get("order_id")) == oid and first.get("symbol") in exits:
            doc = guard_doc(root)          # R41: the exit waits for this note
            doc["cancelled"].append(oid)
            _write(root, "trader/state/guard.json", doc)
            return "the stop %s, before an approved exit of %s" % (first.get("stop_id"), first.get("symbol"))
    raise Blocked("order_id %s is not a 3 pm cancel line or a stop to cancel before an approved exit" % oid)


def decide(event, root, now):
    """(True, why) to let the call go on, or (False, reason) to block it."""
    try:
        if not isinstance(event, dict):
            raise Blocked("the hook input is not an object")
        kind = TOOLS.get(event.get("tool_name"))
        if kind is None:
            raise Blocked("unknown tool %r" % event.get("tool_name"))
        if kind == "modify":
            raise Blocked("nothing in this pack modifies an order")
        cfg = _read(root, "trader/config.json")
        if not isinstance(cfg, dict):
            raise Blocked("trader/config.json is missing or not JSON")
        state = _read(root, "trader/state/broker.json") or {"orders": []}
        if not isinstance(state, dict):
            raise Blocked("trader/state/broker.json is not an object")
        today = ticket.to_ist(now).date().isoformat()
        ti = event.get("tool_input")
        if kind == "place":
            return True, check_place(root, ti, cfg, state, today, now)
        return True, check_cancel(root, ti, cfg, state, today, now)
    except Blocked as e:
        return False, str(e)
    except Exception as e:  # anything unexpected fails closed
        return False, "the guard could not check this call (%s: %s)" % (type(e).__name__, e)


def project_root():
    """R40: the project this guard belongs to: CLAUDE_PROJECT_DIR, else the folder above trader-broker/. Never the
    event's cwd: a cd into a copy with its own trader/ files must not change whose gates are judged."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        event = json.loads(sys.stdin.read() or "null")
    except ValueError:
        event = None
    ok, why = decide(event, project_root(), real_now())
    if ok:
        return 0
    sys.stderr.write("trader-broker guard blocked this call: %s\n" % why)
    return 2


if __name__ == "__main__":
    sys.exit(main())
