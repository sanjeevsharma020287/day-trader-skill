#!/usr/bin/env python3
"""The 3 pm close for trader-broker (PLAN_INDIA_ZERODHA.md 5.2). Standard library only. It never talks to the
broker: Claude calls the kite tools, saves their replies with `save`, and this tool decides from those files.

  precheck   the guard registered (R7), an NSE trading day, India time in [15:00, 15:25). Then:
             exit 0 "GO" when the close is active, exit 3 "REPORT ONLY" when it is not (R17), exit 1 otherwise
             (before 15:00, a closed day, the guard missing; after 15:25 it logs "missed").
  save       save a kite reply with the time it was saved: save orders|positions|ltp --reply FILE
  cancels    from trader/kite/orders.json: every open or part-filled BUY and every open stop this pack placed
             today (by order_id in the log); writes trader/close-cancels.json (R13) when active
  plan       from trader/kite/positions.json and orders.json, read again after the cancels: one IOC LIMIT sell
             per position this pack opened today; writes trader/close-plan.json when active. --retry: one more
             time, wider band, only for lines that did not fill (R3)
  result     from orders.json read after the sells: each line filled, partly filled, unfilled or rejected, into
             the broker log as "auto-close"

The close is ACTIVE only when real orders are unlocked ("dry_run": false and b0_proven_on, R14), the owner has
recorded close_gate_proven_on by hand (R17) and the guard is registered (R7). Otherwise cancels and plan write
nothing: they read and report what is open, and name Zerodha's square-off (15:12 for the F&O list, 15:25 for the
rest) and its fee. Every price carries its source and age; a price of unknown age gets the wider band, and no
price is no sell, reported (R16a). A position whose order_id is in the pack's log but whose order has no tag gets
its own line and no sell (R16b). Positions opened by hand are listed and left alone.
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket  # noqa: E402

dt = ticket.datetime
FILE_MAX_AGE_S = 300
FRESH_CLOSE_S = 300             # B0-provisional (R15): a close price this fresh gets the narrow band; older or
                                # unknown age, the wide one (R16a). B0 measures how old Kite's prices are.
FO_LAST = dt.time(15, 10)       # the no-yes sell for an F&O-list stock stops here, 2 minutes before 15:12 (R8)
DONE = {"COMPLETE", "CANCELLED", "REJECTED"}
FEE = "%s + GST an order" % ticket.inr(50, 0)


class CloseError(Exception):
    pass


def real_now():
    """The real clock, naive UTC; tests replace this function."""
    return ticket.real_now()


# ---------------------------------------------------------------- files

def path(root, rel):
    return os.path.join(root, rel)


def read(root, rel, required=False):
    p = path(root, rel)
    if not os.path.exists(p):
        if required:
            raise CloseError("no %s" % rel)
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        raise CloseError("%s is not valid JSON" % rel)


def utc_stamp(now):
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(s):
    try:
        return dt.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def saved(root, what, now, max_age=FILE_MAX_AGE_S):
    """The data of trader/kite/<what>.json if it was saved in the last 5 minutes, else CloseError."""
    doc = read(root, "trader/kite/%s.json" % what)
    if not isinstance(doc, dict):
        raise CloseError("no trader/kite/%s.json: call get_%s and save it (closer.py save %s --reply FILE)"
                         % (what, what if what != "ltp" else "ltp", what))
    t = parse_stamp(doc.get("saved_at"))
    if t is None or not -60 <= (now - t).total_seconds() <= max_age:
        raise CloseError("trader/kite/%s.json was not saved in the last 5 minutes: read it again" % what)
    return doc.get("data"), t


def unwrap(reply):
    """A kite reply as saved: the bare value, or {"data": value} (with or without "status")."""
    if isinstance(reply, dict) and "data" in reply and len(set(reply) - {"data", "status"}) == 0:
        return reply["data"]
    return reply


def order_list(reply):
    v = unwrap(reply)
    return [o for o in v if isinstance(o, dict)] if isinstance(v, list) else []


def net_positions(reply):
    v = unwrap(reply)
    net = v.get("net") if isinstance(v, dict) else None
    return [p for p in net if isinstance(p, dict)] if isinstance(net, list) else []


# ---------------------------------------------------------------- the gates

def active(root, cfg, today):
    """(True, "") when the close may place and cancel; else (False, why)."""
    if (cfg or {}).get("dry_run", True) is not False:
        return False, "this is a dry run (\"dry_run\" is not false)"
    d, why = ticket.proven_on(cfg, "b0_proven_on", today)
    if d is None:
        return False, why + " (R14)"
    d, why = ticket.proven_on(cfg, "close_gate_proven_on", today)
    if d is None:
        return False, why + " (R17)"
    g = ticket.guard_problem(root)
    if g:
        return False, g
    return True, ""


def report_only_line(why):
    return ("REPORT ONLY: the 3 pm close is not active (%s). This run places and cancels nothing; it reports what "
            "is open. Zerodha squares off an open MIS position at 15:12 (F&O list) or 15:25, for %s."
            % (why, FEE))


def pack_records(state, today):
    """{order_id: record} for this pack's placed entries and stops today (the broker's own record, R5)."""
    out = {}
    for first, last in ticket.stop_groups(state, today).values():
        if last.get("status") == "placed" and last.get("order_id"):
            out[str(last["order_id"])] = last
    for rec in state.get("orders", []):
        if ticket.rec_day(rec) == today and rec.get("leg") == "entry" and rec.get("status") == "placed" \
                and rec.get("order_id"):
            out[str(rec["order_id"])] = rec
    return out


# ---------------------------------------------------------------- commands

def cmd_precheck(a, root, cfg, state, now):
    local = ticket.to_ist(now)
    today = local.date().isoformat()
    g = ticket.guard_problem(root)
    if g:
        print("NOTHING TO DO: %s. Without the guard the 3 pm run does nothing." % g)
        return 1
    ok, why = ticket.nse_trading_day(local.date())
    if not ok:
        print("NOTHING TO DO: not a trading day (%s)." % why)
        return 1
    t = local.time()
    if t < ticket.ENTRIES_UNTIL:
        print("NOTHING TO DO: it is %s India time; the close runs from 15:00." % local.strftime("%H:%M"))
        return 1
    if t >= ticket.SQUARE_OFF:
        state.setdefault("orders", []).append({"at": ticket.ist_iso(now), "date": today, "status": "close-missed",
                                               "kind": "auto-close", "plain": "the 3 pm close did not run in time"})
        ticket.write_json(path(root, a.state), state)
        print("MISSED: it is %s India time, after 15:25. Zerodha's square-off applies (15:12 F&O list, 15:25 the "
              "rest, %s); check Kite's tradebook." % (local.strftime("%H:%M"), FEE))
        return 1
    on, why = active(root, cfg, today)
    if not on:
        print(report_only_line(why))
        return 3
    print("GO: %s India time on %s. Next: get_orders, save it, closer.py cancels." % (local.strftime("%H:%M"), today))
    return 0


def cmd_save(a, root, cfg, state, now):
    src = a.reply or path(root, "trader/kite/%s.reply.json" % a.what)
    try:
        with open(src, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise CloseError("cannot read the %s reply at %s (%s)" % (a.what, src, e))
    ticket.write_json(path(root, "trader/kite/%s.json" % a.what), {"saved_at": utc_stamp(now), "data": data})
    print("Saved trader/kite/%s.json at %s India time." % (a.what, ticket.to_ist(now).strftime("%H:%M:%S")))
    return 0


def cmd_cancels(a, root, cfg, state, now):
    local = ticket.to_ist(now)
    today = local.date().isoformat()
    orders, _ = saved(root, "orders", now)
    ours = pack_records(state, today)
    tag = ticket.B0_PROVISIONAL["tag"]
    lines, notes = [], []
    for o in order_list(orders):
        oid = str(o.get("order_id") or "")
        rec = ours.get(oid)
        status = str(o.get("status") or "").upper()
        if rec is None:
            if o.get("tag") == tag and status not in DONE:
                notes.append("%s %s %s (order_id %s) carries the tag but is not in the pack's log: left alone"
                             % (o.get("transaction_type"), o.get("quantity"), o.get("tradingsymbol"), oid))
            continue
        if status in DONE:
            continue
        what = "entry BUY" if rec.get("leg") == "entry" else "stop"
        if o.get("tag") != tag:
            notes.append("%s %s (order_id %s): in the pack's log, tag missing" % (what, o.get("tradingsymbol"), oid))
        lines.append({"line": len(lines) + 1, "order_id": oid, "tradingsymbol": o.get("tradingsymbol"),
                      "what": "%s, %s, %s of %s filled" % (what, status, o.get("filled_quantity"), o.get("quantity")),
                      "fo": rec.get("fo"), "used": None})
    on, why = active(root, cfg, today)
    for n in notes:
        print("NOTE: " + n)
    if not lines:
        print("No open order of this pack to cancel.")
    for l in lines:
        print("%s %d: cancel_order {\"variety\": \"regular\", \"order_id\": \"%s\"}  (%s %s)"
              % ("Line" if on else "Open", l["line"], l["order_id"], l["tradingsymbol"], l["what"]))
    if not on:
        print(report_only_line(why))
        return 3
    if lines:
        ticket.write_json(path(root, "trader/close-cancels.json"),
                          {"date": today, "written_at": utc_stamp(now), "check_id": os.urandom(8).hex(),
                           "lines": lines})
        print("Wrote trader/close-cancels.json. Cancel each line, then get_orders and get_positions again, save "
              "them, and run closer.py plan.")
    return 0


def price_for(root, sym, positions_reply, pos_saved, now):
    """(price, source, age_s or None) by B0_PROVISIONAL["price_sources"], or (None, None, None) (R16a)."""
    for src in ticket.B0_PROVISIONAL["price_sources"]:
        if src == "kite_ltp":
            doc = read(root, "trader/kite/ltp.json")
            t = parse_stamp((doc or {}).get("saved_at"))
            data = unwrap((doc or {}).get("data"))
            q = (data or {}).get("NSE:" + sym) if isinstance(data, dict) else None
            p = ticket._f((q or {}).get("last_price")) if isinstance(q, dict) else None
            if p and p > 0:
                return p, "kite_ltp", (now - t).total_seconds() if t else None
        elif src == "kite_positions":
            for pos in net_positions(positions_reply):
                if pos.get("tradingsymbol") == sym:
                    p = ticket._f(pos.get("last_price"))
                    if p and p > 0:
                        return p, "kite_positions", (now - pos_saved).total_seconds()
        elif src == "yahoo":
            doc = read(root, "trader/prices.json")
            if isinstance(doc, dict):
                p = ticket._f((doc.get("prices") or {}).get(sym))
                d = (doc.get("detail") or {}).get(sym) or {}
                t = None
                for k in (d.get("time"), d.get("fetched"), doc.get("as_of")):
                    try:
                        t = ticket.parse_ist(k) if k else None
                    except ValueError:
                        t = None
                    if t:
                        break
                if p and p > 0:
                    return p, "yahoo", (now - t).total_seconds() if t else None
    return None, None, None


def age_words(age):
    if age is None:
        return "age unknown"
    return ("%d min old" % (age // 60)) if age >= 60 else ("%d s old" % max(age, 0))


def tick_down(price, tick):
    return round(math.floor(price / tick + 1e-9) * tick, 2)


def cmd_plan(a, root, cfg, state, now):
    local = ticket.to_ist(now)
    today = local.date().isoformat()
    orders, _ = saved(root, "orders", now)
    positions, pos_saved = saved(root, "positions", now)
    ours = pack_records(state, today)
    tag = ticket.B0_PROVISIONAL["tag"]
    universe = ticket.read_universe(path(root, a.universe))
    on, why = active(root, cfg, today)
    retry_of = None
    if a.retry:
        old = read(root, "trader/close-plan.json")
        if not isinstance(old, dict) or old.get("date") != today:
            raise CloseError("--retry needs today's trader/close-plan.json")
        if old.get("retry"):
            raise CloseError("there is one retry only; anything still open goes to Zerodha's square-off")
        retry_of = {l.get("tradingsymbol") for l in old.get("lines") or []
                    if l.get("result") in ("unfilled", "partly filled")}
    filled, tagless = {}, set()
    for o in order_list(orders):
        rec = ours.get(str(o.get("order_id") or ""))
        if rec is None or rec.get("leg") != "entry" or o.get("transaction_type") != "BUY":
            continue
        n = int(ticket._f(o.get("filled_quantity")) or 0)
        if n <= 0:
            continue
        if o.get("tag") != tag:
            tagless.add(o.get("tradingsymbol"))
            continue
        filled[o.get("tradingsymbol")] = filled.get(o.get("tradingsymbol"), 0) + n
    lines, notes = [], []
    for p in net_positions(positions):
        sym, net = p.get("tradingsymbol"), int(ticket._f(p.get("quantity")) or 0)
        if p.get("product") != "MIS" or p.get("exchange") != "NSE" or net <= 0:
            continue
        if sym in tagless:
            notes.append("%s: in the pack's log, tag missing: no sell (R16b); Zerodha's square-off applies" % sym)
            continue
        if sym not in filled:
            notes.append("%s: %d held, opened by hand (not by this pack): left alone" % (sym, net))
            continue
        if retry_of is not None and sym not in retry_of:
            continue
        qty = min(net, filled[sym])
        tick, fo = universe.get(sym, (None, None))
        if not tick:
            notes.append("%s: no tick size in %s: no sell; Zerodha's square-off applies" % (sym, a.universe))
            continue
        if fo is not False and local.time() >= FO_LAST:
            notes.append("%s: an F&O-list stock (or unknown) after 15:10: no sell; Zerodha squares it off at 15:12"
                         % sym)
            continue
        price, src, age = price_for(root, sym, positions, pos_saved, now)
        if price is None:
            notes.append("%s: no price from %s: no sell (R16a); Zerodha's square-off applies"
                         % (sym, ", ".join(ticket.B0_PROVISIONAL["price_sources"])))
            continue
        wide = a.retry or age is None or age > FRESH_CLOSE_S
        band = ticket.B0_PROVISIONAL["close_band_wide_pct" if wide else "close_band_pct"]
        limit = tick_down(price * (1 - band / 100.0), tick)
        ti = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
              "quantity": qty, "product": "MIS", "order_type": "LIMIT", "price": limit, "validity": "IOC",
              "tag": tag}
        lines.append({"line": len(lines) + 1, "tradingsymbol": sym, "quantity": qty, "fo": fo, "tick": tick,
                      "price_label": "%s (%s, %s)" % (ticket.inr(price), src, age_words(age)), "band_pct": band,
                      "tool_input": ti, "used": None, "result": None})
    for n in notes:
        print("NOTE: " + n)
    if not lines:
        print("Nothing for the 3 pm close to sell.")
    for l in lines:
        print("%s %d: SELL %d %s, IOC limit %.2f = last price %s minus %s%%%s"
              % ("Line" if on else "Open", l["line"], l["quantity"], l["tradingsymbol"], l["tool_input"]["price"],
                 l["price_label"], ticket._num(l["band_pct"]), " (retry)" if a.retry else ""))
    if not on:
        print(report_only_line(why))
        return 3
    if lines or a.retry:
        ticket.write_json(path(root, "trader/close-plan.json"),
                          {"date": today, "written_at": utc_stamp(now), "check_id": os.urandom(8).hex(),
                           "retry": bool(a.retry), "lines": lines})
        print("Wrote trader/close-plan.json. For each line, place_order with exactly its tool_input; then "
              "get_orders, save it, closer.py result.")
    return 0


def cmd_result(a, root, cfg, state, now):
    today = ticket.to_ist(now).date().isoformat()
    plan = read(root, "trader/close-plan.json", required=True)
    if plan.get("date") != today:
        raise CloseError("trader/close-plan.json is not from today")
    orders, _ = saved(root, "orders", now)
    since = ticket.to_ist(parse_stamp(plan.get("written_at")) or now).strftime("%Y-%m-%d %H:%M:%S")
    tag = ticket.B0_PROVISIONAL["tag"]
    open_left = 0
    for l in plan.get("lines") or []:
        if not l.get("used"):
            l["result"] = "not sent"
            print("Line %d %s: not sent" % (l["line"], l["tradingsymbol"]))
            open_left += 1
            continue
        mine = [o for o in order_list(orders) if o.get("tradingsymbol") == l["tradingsymbol"]
                and o.get("transaction_type") == "SELL" and o.get("tag") == tag
                and str(o.get("validity")).upper() == "IOC" and str(o.get("order_timestamp") or "") >= since]
        o = mine[-1] if mine else {}
        st = str(o.get("status") or "").upper()
        n = int(ticket._f(o.get("filled_quantity")) or 0)
        if not o:
            res = "not found"
        elif st == "REJECTED":
            res = "rejected"
        elif st == "COMPLETE" and n >= l["quantity"]:
            res = "filled"
        elif st in DONE:
            res = "partly filled" if n else "unfilled"
        else:
            res = "not final"
        l["result"] = res
        if res != "filled":
            open_left += 1
        state.setdefault("orders", []).append({
            "at": ticket.ist_iso(now), "date": today, "status": "auto-close", "kind": "auto-close",
            "symbol": l["tradingsymbol"], "transaction_type": "SELL", "quantity": n, "order_id": o.get("order_id"),
            "result": res, "plain": "3 pm close: SELL %d of %d %s, %s%s" % (
                n, l["quantity"], l["tradingsymbol"], res,
                (": " + str(o.get("status_message"))) if res == "rejected" and o.get("status_message") else "")})
        print("Line %d %s: %s (%d of %d)" % (l["line"], l["tradingsymbol"], res, n, l["quantity"]))
    ticket.write_json(path(root, "trader/close-plan.json"), plan)
    ticket.write_json(path(root, a.state), state)
    if open_left:
        print("%d line%s did not fully close. %s" % (
            open_left, "" if open_left == 1 else "s",
            "Zerodha squares off what is still open at 15:12 (F&O list) or 15:25, for %s." % FEE if plan.get("retry")
            else "One retry: get_orders and get_positions again, save them, closer.py plan --retry."))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The 3 pm close (trader-broker). Never places orders itself.")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("precheck", "save", "cancels", "plan", "result"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".", help="the project folder (default: here)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--state", default="trader/state/broker.json")
        if name == "save":
            p.add_argument("what", choices=["orders", "positions", "ltp"])
            p.add_argument("--reply", help="the saved reply (default: trader/kite/<what>.reply.json)")
        if name == "plan":
            p.add_argument("--retry", action="store_true")
            p.add_argument("--universe", default="trader/universe.txt")
    a = ap.parse_args(argv)
    ticket.utf8_stdio()
    if not a.cmd:
        ap.print_help()
        return 1
    root = os.path.abspath(a.root)
    try:
        cfg = read(root, a.config) or {}
        state = read(root, a.state) or {"orders": []}
        fn = {"precheck": cmd_precheck, "save": cmd_save, "cancels": cmd_cancels, "plan": cmd_plan,
              "result": cmd_result}[a.cmd]
        return fn(a, root, cfg, state, real_now())
    except CloseError as e:
        print("closer.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
