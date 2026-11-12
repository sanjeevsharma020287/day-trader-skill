#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker (Zerodha Kite, NSE). Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON Claude would send to the kite MCP server's place_order
            tool. An entry and its stop (a pair from trader-budget) are one
            ticket: one yes covers the pair. An order is READY only if
            trader-risk passed THIS exact order (and its stop) on the real
            clock in the last 15 minutes, inside 09:30 to 15:00 India time,
            and nobody has decided on it yet.
  approve   records your explicit yes for ONE ticket, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any ticket that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with Kite's order_id), denied, or failed; or unsent, when a
            held or expired yes never went out (check Kite first).
  stop      after the entry shows COMPLETE (or part filled) in get_orders:
            the stop's place_order input for the FILLED quantity.
  fill      records a fill in trader/portfolio.json (an MIS position of today).
  flatten   builds a LIMIT sell ticket that closes a symbol's MIS position.
            It still goes through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --order-id 251111000123456
    python3 ticket.py stop --pair M&M-2026-11-11-101600 --filled-qty 32
    python3 ticket.py fill --symbol M&M --side buy --qty 32 --price 3427
    python3 ticket.py flatten --symbol M&M --price 3460.5
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). The clock is
always the real one, in India time (R36): there is no --now, so a yes can
never be given against a pretended time.

Real orders (R14): nothing is ever sent while the config has "dry_run": true.
With "dry_run": false, real orders are still blocked until the owner has
recorded B0 by hand: the key b0_proven_on set to a date like 2026-11-13 (not in the
future). No tool writes b0_proven_on or close_gate_proven_on.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product", "order_type",
               "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity", "tag")
TOOL = "place_order"
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
RISK_MAX_AGE_MIN = 15  # a PASS older than this is stale: prices and the window move on
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (India, no tz files)

# ==== india clock: shared, byte-identical in every tool (tests/test_repo.py checks) ====
import datetime as _ist_dt

IST_OFFSET = _ist_dt.timedelta(hours=5, minutes=30)  # India keeps UTC+5:30 all year: no summer time
NSE_HOLIDAYS = {  # NSE trading holidays 2026 (circular NSE/CMTR/71775; api/holiday-master, read 2026-11-12)
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_SPECIAL_SESSIONS = {  # the exchange opens, but this pack does not trade them (fail closed)
    "2026-02-01": "Union Budget live session (Sunday)",
    "2026-11-08": "Muhurat trading (Sunday)",
}
NSE_CALENDAR_YEARS = (2026,)  # add a year only from NSE's own circular for it
NSE_OPEN = _ist_dt.time(9, 15)        # normal market opens (pre-open 9:00 to 9:15)
NSE_CLOSE = _ist_dt.time(15, 30)      # normal market closes
FO_CLOSE = _ist_dt.time(15, 15)       # F&O-list stocks: continuous trading ends, the closing auction follows
ENTRIES_FROM = _ist_dt.time(9, 30)    # the owner's rule: no order before 9:30
ENTRIES_UNTIL = _ist_dt.time(15, 0)   # the owner's rule: no order at or after 15:00, and every position closed
SQUARE_OFF = _ist_dt.time(15, 25)     # Zerodha's MIS auto square-off, equity
SQUARE_OFF_FO = _ist_dt.time(15, 12)  # ... and for F&O-list stocks (closing auction session)


def to_ist(utc):
    """UTC (naive or aware) -> naive India time."""
    if utc.tzinfo is not None:
        utc = (utc - utc.utcoffset()).replace(tzinfo=None)
    return utc + IST_OFFSET


def ts_to_ist(ts):
    """Unix seconds -> naive India time. Never uses the machine's time zone."""
    return _ist_dt.datetime(1970, 1, 1) + _ist_dt.timedelta(seconds=int(ts)) + IST_OFFSET


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local - IST_OFFSET


def parse_ist(text):
    """ISO time -> naive UTC. A time with no offset is India time; Z or +hh:mm is honoured.
    Raises ValueError on text that is not an ISO time."""
    dt = _ist_dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def nse_trading_day(d):
    """(is_trading_day, reason) for an India date. Fails closed: a year with no NSE calendar,
    a weekend, a holiday or a special session is not a trading day for this pack."""
    if d.year not in NSE_CALENDAR_YEARS:
        return False, "no NSE holiday calendar for %d in this pack yet" % d.year
    key = "%04d-%02d-%02d" % (d.year, d.month, d.day)  # a datetime's isoformat() would miss both tables
    if key in NSE_SPECIAL_SESSIONS:
        return False, "NSE special session (no entries): " + NSE_SPECIAL_SESSIONS[key]
    if d.weekday() >= 5:
        return False, "weekend"
    if key in NSE_HOLIDAYS:
        return False, "NSE holiday: " + NSE_HOLIDAYS[key]
    return True, ""


def inr(x, places=2):
    """Rupees with Indian grouping: 1234567.8 -> "\u20b912,34,567.80"."""
    s = ("%%.%df" % places) % abs(x)
    whole, _, frac = s.partition(".")
    head, groups = whole[:-3], [whole[-3:]]
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    sign = "-" if x < 0 and float(s) != 0 else ""
    return sign + "\u20b9" + ",".join(groups) + ("." + frac if frac else "")


def utf8_stdio():
    """Print the rupee sign on a Windows pipe too (cp1252 cannot encode it)."""
    import sys as _ist_sys
    for stream in (_ist_sys.stdout, _ist_sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

# B0-provisional (R15): every choice that B0's two real orders may still change, in one place.
# Not proven on a real account until the owner records b0_proven_on; see PLAN_INDIA_ZERODHA.md R14/R15.
B0_PROVISIONAL = {
    "tag": "traderpack",              # Kite order tag on every order this pack places (max 20 chars)
    "tag_required": True,             # an order without the tag is refused by trader-risk
    "entry_validity": "DAY",          # entries and stops; TTL is not used until B0 shows how Kite takes it
    "stop_limit_band_pct": 0.5,       # the stop's SL limit sits this far below its trigger
    "close_band_pct": 0.5,            # the 3 pm close's limit, under a fresh price of known age
    "close_band_wide_pct": 1.5,       # ... and under an older one (never for a price of unknown age)
    "price_sources": ("kite_ltp", "kite_positions", "yahoo"),  # first one present wins
    "close_schedule": "15:00",        # when the owner's scheduled task runs the 3 pm close
}


def est_charges(qty, buy, sell):
    """Zerodha intraday equity charges for one round trip, an ESTIMATE (plan F12): brokerage the lower of
    0.03 % or Rs 20 per executed order, STT 0.025 % on the sell, NSE transaction 0.00307 %, SEBI Rs 10 a
    crore, stamp duty 0.003 % on the buy, GST 18 % on brokerage + transaction + SEBI. Zerodha's Rs 50 + GST
    fee for a position it squares off is not in it."""
    b, s = qty * float(buy), qty * float(sell)
    brokerage = min(0.0003 * b, 20.0) + min(0.0003 * s, 20.0)
    txn = 0.0000307 * (b + s)
    sebi = 0.000001 * (b + s)
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + 0.00025 * s + txn + sebi + 0.00003 * b + gst, 2)
# ==== end india clock ====


def real_now():
    """The real clock, naive UTC. Never pinned from the command line (R36); tests replace this function."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-11-11T10:16:00+05:30"."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The India trading day a decision belongs to: its "date", else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- real orders: the owner's gates

def proven_on(cfg, key, today):
    """(date, None) when cfg[key] is a real date not after today (India), else (None, why).
    The owner writes these keys by hand; no tool in this pack writes them."""
    v = (cfg or {}).get(key)
    if v is None or v == "":
        return None, "%s is not recorded" % key
    if not isinstance(v, str) or not DATE.match(v):
        return None, "%s %r is not a date like \"2026-11-13\"" % (key, v)
    try:
        d = datetime.date(int(v[:4]), int(v[5:7]), int(v[8:10]))
    except ValueError:
        return None, "%s %r is not a real date" % (key, v)
    if d.isoformat() > today:
        return None, "%s %s is in the future (today is %s in India)" % (key, v, today)
    return v, None


def mode(cfg, today):
    """("live", line) only when "dry_run" is false AND b0_proven_on is a real date not in the future (R14).
    Otherwise ("dry_run", line): nothing is sent."""
    if (cfg or {}).get("dry_run", True) is not False:
        return "dry_run", "DRY RUN, nothing is sent"
    d, why = proven_on(cfg, "b0_proven_on", today)
    if d is None:
        return "dry_run", ("DRY RUN: \"dry_run\" is false, but %s, so real orders stay blocked until the owner "
                           "records B0 by hand (PLAN_INDIA_ZERODHA.md R14)" % why)
    return "live", "LIVE, REAL MONEY (B0 recorded on %s)" % d


def close_gate_lines(cfg, today):
    """R17: what the user must know at the yes while the 3 pm close is not active."""
    d, _ = proven_on(cfg, "close_gate_proven_on", today)
    if d is not None:
        return []
    return ["The 3 pm close is not active (close_gate_proven_on is not recorded): this pack will not sell this "
            "at 15:00, and a resting BUY can still fill after 15:00. Zerodha squares off an open MIS position "
            "at 15:12 (F&O list) or 15:25, for %s + GST an order. Close it yourself before then." % inr(50, 0)]


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function in trader-risk/check.py: the exact Kite order that was checked."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(v):
    return "%.2f" % float(v)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def tool_input(order):
    """The exact arguments for the kite MCP server's place_order: Kite's field names, numbers as JSON numbers.
    P5 (read from source): the server reads a missing or text quantity as 1 share, so a quantity that is not
    a whole JSON number is refused here, never sent. Our own keys (leg, pair, plan, why...) are never sent."""
    out = {}
    for k in KITE_FIELDS:
        if k in order and order[k] is not None:
            out[k] = order[k]
    q = out.get("quantity")
    if isinstance(q, bool) or not isinstance(q, (int, float)) or q != int(q) or q < 1:
        raise TicketError("the order's quantity %r is not a whole number of shares (a JSON number); not sent"
                          % (q,))
    out["quantity"] = int(q)
    for k in ("price", "trigger_price"):
        if k in out and (isinstance(out[k], (bool, str)) or not isinstance(out[k], (int, float))):
            raise TicketError("the order's %s %r is not a JSON number; not sent" % (k, out[k]))
    return out


def leg_of(order):
    return str(order.get("leg") or ("entry" if str(order.get("transaction_type", "")).upper() == "BUY"
                                   else "exit"))


def stop_of(orders, idx):
    """(index, order) of the stop leg paired with the entry at idx, or (None, None)."""
    pair = orders[idx - 1].get("pair")
    if not pair:
        return None, None
    for j, o in enumerate(orders, 1):
        if j != idx and o.get("pair") == pair and leg_of(o) == "stop":
            return j, o
    return None, None


def entry_of(orders, idx):
    pair = orders[idx - 1].get("pair")
    for j, o in enumerate(orders, 1):
        if j != idx and pair and o.get("pair") == pair and leg_of(o) == "entry":
            return j, o
    return None, None


def plain(order):
    """BUY 32 M&M, limit 3427.00, intraday (MIS), good for today."""
    side = str(order.get("transaction_type", "")).upper()
    sym = order.get("tradingsymbol", "?")
    q = order.get("quantity")
    otype = str(order.get("order_type", "")).upper()
    if otype == "SL":
        how = "stop-loss: trigger %s, limit %s" % (_p(order.get("trigger_price")), _p(order.get("price")))
    elif otype == "LIMIT":
        how = "limit %s" % _p(order.get("price"))
    else:
        how = otype or "no order type"
    good = "good for today" if str(order.get("validity", "")).upper() == "DAY" else str(order.get("validity"))
    return "%s %s %s, %s, intraday (%s), %s" % (side, q, sym, how, order.get("product"), good)


def loss_of(rv, order):
    """(max loss, charges) from trader-risk's verdict, else the plan."""
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    ml = _f((rv or {}).get("max_loss"))
    ch = _f((rv or {}).get("charges_est"))
    if ml is None:
        ml, ch = _f(plan.get("max_loss")), _f(plan.get("charges_est"))
    return ml, ch


def amount_numbers(order, stop=None, rv=None):
    nums = []
    for o in (order, stop or {}):
        for k in ("quantity", "price", "trigger_price"):
            v = _f(o.get(k))
            if v is not None:
                nums.append(v)
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    for v in (_f(plan.get("target")), loss_of(rv, order)[0]):
        if v is not None:
            nums.append(v)
    return nums


def parse_yes(reply, order, batch_symbols=(), stop=None, rv=None):
    """Is this reply an explicit yes for THIS one ticket? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one ticket at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("tradingsymbol", "")).upper()
    tokens = set(re.findall(r"[A-Z0-9][A-Z0-9&\-]{0,19}", raw.upper()))
    others = {str(s).upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this ticket is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order, stop, rv)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this ticket is %s" % (", ".join(_num(n) for n in nums),
                                                                         plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, today, fp=None, pair=None, leg=None):
    """The latest decision recorded today for this fingerprint (or this pair's leg), or None."""
    found = None
    for rec in state.get("orders", []):
        if rec_day(rec) != today:
            continue
        if fp is not None and rec.get("fingerprint") == fp:
            found = rec
        elif pair is not None and rec.get("pair") == pair and rec.get("leg") == leg:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def risk_clock(risk, now):
    """R36 and the 15 minutes: why this risk check cannot be used at `now` (naive UTC, the real clock), or None.
    The check must have been run on the real clock: its date and now_india are the India day and minute of its
    own checked_at (a check pinned with --now or --today is refused)."""
    try:
        checked = datetime.datetime.strptime(str(risk.get("checked_at")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return "the risk check has no checked_at time; run check.py again (without --no-write)"
    local = to_ist(checked)
    if risk.get("date") != local.date().isoformat() or risk.get("now_india") != local.strftime("%Y-%m-%d %H:%M"):
        return ("the risk check was run with a pretended clock (it says %s, but it ran at %s India time); run "
                "check.py again without --now or --today" % (risk.get("now_india"), local.strftime("%Y-%m-%d %H:%M")))
    age = (now - checked).total_seconds()
    if age < -60:
        return "the risk check is timed after now; run check.py again"
    if age > RISK_MAX_AGE_MIN * 60:
        return ("the risk check is %d minutes old (from %s India time; it holds %d); run check.py again"
                % (age // 60, local.strftime("%H:%M"), RISK_MAX_AGE_MIN))
    return None


def window_now(order, rv, risk, now):
    """Why this order cannot go now on the real clock (R36), or None. An entry: an NSE trading day, 09:30 to
    the check's entries_until (never after 15:00). An exit: 09:15 to Zerodha's square-off for it."""
    local = to_ist(now)
    ok, why = nse_trading_day(local.date())
    if not ok:
        return "no NSE session for this pack today (%s)" % why
    t = local.time().replace(second=0, microsecond=0)
    if (rv or {}).get("kind", leg_of(order)) == "entry":
        until = (rv or {}).get("entries_until") or risk.get("entries_until")
        cut = ENTRIES_UNTIL
        if isinstance(until, str) and re.fullmatch(r"\d\d:\d\d", until):
            cut = min(cut, datetime.time(int(until[:2]), int(until[3:])))
        if t < ENTRIES_FROM:
            return "no orders before 09:30 India time (it is %s)" % t.strftime("%H:%M")
        if t >= cut:
            return "no new entries from %s India time (it is %s)" % (cut.strftime("%H:%M"), t.strftime("%H:%M"))
        return None
    last = SQUARE_OFF if order.get("fo") is False else SQUARE_OFF_FO
    if t < NSE_OPEN or t >= last:
        return "outside the session for an exit (09:15 to %s India time; it is %s)" % (last.strftime("%H:%M"),
                                                                                       t.strftime("%H:%M"))
    return None


def status_of(orders, idx, risk, state, today, now):
    """(ready, label, risk_result) for the ticket at idx (an entry with its stop, or an exit)."""
    order = orders[idx - 1]
    leg = leg_of(order)
    if leg == "stop":
        e, _ = entry_of(orders, idx)
        return False, ("the stop of order %s: it goes in after that entry fills (ticket.py stop --pair %s)"
                       % (e or "?", order.get("pair"))), None
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    sj, stop = stop_of(orders, idx)
    if leg == "entry" and stop is not None:
        sv, why = risk_verdict(risk, fingerprint(stop), today)
        if sv is None:
            return False, "NOT READY: its stop (order %d): %s" % (sj, why), rv
        if sv.get("verdict") != "PASS":
            return False, "BLOCKED by trader-risk: its stop (order %d): %s" % (sj, "; ".join(sv.get("reasons", []))), rv
    rec = latest(state, today, fp=fp)
    if rec and rec.get("status") == "unsent":
        rec = None  # recorded as never sent: a fresh PASS needs a new yes
    if rec and rec.get("status") == "approved" and rec.get("risk_check") != risk.get("check_id"):
        return False, ("NOT READY: your yes at %s was on an earlier check. Look at Kite's orders (get_orders) for "
                       "%s, then record ticket.py result --status placed (or failed or denied) if it was sent, or "
                       "ticket.py result --status unsent if it was not" % (rec.get("at"), rec.get("symbol"))), rv
    if rec and rec.get("status") == "approved":
        stale = risk_clock(risk, now) or window_now(order, rv, risk, now)
        if stale:
            return False, ("EXPIRED: your yes at %s no longer holds (%s). Do not place it; record ticket.py result "
                           "--status unsent (or what really happened), run check.py again, then ask for a new yes"
                           % (rec.get("at"), stale)), rv
        return False, "APPROVED by you at %s, waiting for the place_order call" % rec.get("at"), rv
    if rec:
        return False, "DONE: %s at %s" % (rec.get("status"), rec.get("at")), rv
    stale = risk_clock(risk, now) or window_now(order, rv, risk, now)
    if stale:
        return False, "NOT READY: " + stale, rv
    if not risk.get("check_id"):
        return False, "NOT READY: the risk check has no check id; run check.py again (without --no-write)", rv
    return True, "READY: trader-risk PASS%s, waiting for your yes" % (" on the pair" if stop is not None else ""), rv


def placed_entries(state, today):
    """[(pair, the entry's latest record)] for today's paired entries whose latest record is placed (R38: from
    the log, not from orders.json, which flatten or a new plan replaces)."""
    last, order = {}, []
    for rec in state.get("orders", []):
        if rec_day(rec) == today and rec.get("leg") == "entry" and rec.get("pair"):
            if rec["pair"] not in last:
                order.append(rec["pair"])
            last[rec["pair"]] = rec
    return [(p, last[p]) for p in order if last[p].get("status") == "placed"]


def stop_groups(state, today):
    """{stop_id: (its stop_ready record, its latest record)} for every stop prepared today, in order."""
    out = {}
    for rec in state.get("orders", []):
        if rec_day(rec) == today and rec.get("leg") == "stop" and rec.get("stop_id"):
            first = out[rec["stop_id"]][0] if rec["stop_id"] in out else rec
            out[rec["stop_id"]] = (first, rec)
    return out


def covered(state, today, pair):
    """R37: shares under a stop for this pair: stops placed, plus a stop prepared with no result yet (it may be at
    the broker). A denied, failed or unsent stop covers nothing."""
    return sum(int(last.get("quantity") or 0) for first, last in stop_groups(state, today).values()
               if first.get("pair") == pair and last.get("status") in ("placed", "stop_ready"))


def unprotected(state, today):
    """R6, R37: a line for each entry placed today whose stops cover less than its quantity."""
    out = []
    for pair, e in placed_entries(state, today):
        want, have = int(e.get("quantity") or 0), covered(state, today, pair)
        if have >= want:
            continue
        out.append("UNPROTECTED: %s, entry order_id %s (pair %s): stops cover %d of %d; whatever has filled beyond "
                   "that has no stop. When get_orders shows a fill: ticket.py stop --pair %s --filled-qty Q"
                   % (e.get("symbol"), e.get("order_id") or "not recorded", pair, have, want, pair))
    return out


# ---------------------------------------------------------------- rendering

def render(orders, idx, cfg, risk, state, today, now):
    order = orders[idx - 1]
    ready, label, rv = status_of(orders, idx, risk, state, today, now)
    m, mline = mode(cfg, today)
    leg = leg_of(order)
    lines = ["ORDER %d of %d: %s (Zerodha Kite, NSE)" % (idx, len(orders), mline)]
    lines.append("  " + plain(order))
    sj, stop = stop_of(orders, idx)
    if leg == "entry":
        if stop is not None:
            lines.append("  its stop, order %d: %s; it goes in only after this buy fills, for the filled quantity"
                         % (sj, plain(stop)))
        plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
        if _f(plan.get("target")):
            lines.append("  target %s: an alert, not an order" % _p(plan["target"]))
        ml, ch = loss_of(rv, order)
        if ml is not None:
            lines.append("  max loss about %s at the stop's limit%s (a stop can fill worse, or not at all, in a "
                         "fast market)" % (inr(ml), (", including %s estimated charges" % inr(ch)) if ch else ""))
    for x in (rv or {}).get("info", []):
        if x.startswith("last price"):
            lines.append("  " + x)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if leg != "stop":
        lines.append("  %s input:" % TOOL)
        try:
            for l in json.dumps(tool_input(order), indent=2, ensure_ascii=False).splitlines():
                lines.append("    " + l)
        except TicketError as e:
            lines.append("    (not sendable: %s)" % e)
    if ready:
        lines.append("  Reply \"yes\" to %s, or \"no\" to skip it."
                     % ("this pair (the buy, then its stop)" if stop is not None else "this one order"))
        if m == "dry_run":
            lines.append("  Dry run: after the yes, Claude does not call %s. The yes is recorded, nothing is sent."
                         % TOOL)
        else:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
        if leg == "entry":
            lines.extend("  " + x for x in close_gate_lines(cfg, today))
    return "\n".join(lines), ready


def ticket_json(orders, idx, cfg, risk, state, today, now):
    order = orders[idx - 1]
    ready, label, rv = status_of(orders, idx, risk, state, today, now)
    sj, stop = stop_of(orders, idx)
    ml, ch = loss_of(rv, order)
    try:
        ti = tool_input(order) if leg_of(order) != "stop" else None
    except TicketError:
        ti = None
    return {"index": idx, "leg": leg_of(order), "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "mode": mode(cfg, today)[0], "broker": "zerodha", "tool": TOOL,
            "tool_input": ti, "stop_index": sj, "stop": plain(stop) if stop else None,
            "why": order.get("why", ""), "max_loss": ml, "charges_est": ch,
            "close_gate": not close_gate_lines(cfg, today)}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a fill in trader/portfolio.json as an MIS position of `date` (R35: only today's MIS positions
    count as held for a sell). A sell relieves that day's MIS position only."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": budget, "positions": []}
    symbol = symbol.strip().upper()
    q, price = _f(qty), _f(price)
    if q is None or q <= 0 or q != int(q) or price is None or price <= 0:
        raise TicketError("a fill needs a whole number of shares above 0 and a price above 0")
    q = int(q)
    cost = round(q * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if (str(p.get("tradingsymbol") or "").upper() == symbol and str(p.get("product") or "").upper() == "MIS"
                and p.get("date") == date):
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"tradingsymbol": symbol, "exchange": "NSE", "product": "MIS", "date": date, "quantity": 0,
                   "average_price": 0.0}
            pf["positions"].append(pos)
        held = int(pos.get("quantity") or 0)
        pos["average_price"] = round((held * float(pos.get("average_price") or 0) + cost) / (held + q), 4)
        pos["quantity"] = held + q
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        held = int(pos.get("quantity") or 0) if pos else 0
        if held < q:
            raise TicketError("cannot record selling %d %s: today's MIS position holds %d" % (q, symbol, held))
        pos["quantity"] = held - q
        if pos["quantity"] == 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price):
    state = load_state(state_path)
    state.setdefault("orders", []).append({
        "at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.strip().upper(),
        "transaction_type": side.upper(), "quantity": int(float(qty)), "price": float(price),
        "plain": "%s %d %s at %s" % (side.upper(), int(float(qty)), symbol.strip().upper(), _p(price))})
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock():
    now = real_now()
    return now, to_ist(now).date().isoformat()


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock()
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _record(order, idx, today, now, status, **extra):
    rec = {"at": ist_iso(now), "date": today, "index": idx, "fingerprint": fingerprint(order),
           "pair": order.get("pair"), "leg": leg_of(order), "symbol": str(order.get("tradingsymbol", "")).upper(),
           "transaction_type": str(order.get("transaction_type", "")).upper(), "quantity": order.get("quantity"),
           "plain": plain(order), "status": status}
    rec.update({k: v for k, v in extra.items() if v is not None})
    return rec


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [a.index] if a.index else list(range(1, len(orders) + 1))
    for i in picks:
        _pick(orders, i)
    if a.json:
        out = [ticket_json(orders, i, cfg, risk, state, today, now) for i in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i in picks:
        text, ready = render(orders, i, cfg, risk, state, today, now)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    for line in unprotected(state, today):
        print("\n" + line)
    print("\nOne ticket at a time: a yes covers one ticket (an entry and its stop), never the batch. Education "
          "and tooling, not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(orders, a.index, risk, state, today, now)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    sj, stop = stop_of(orders, a.index)
    ok, why = parse_yes(a.reply, order, [o.get("tradingsymbol", "") for o in orders], stop, rv)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    ti = tool_input(order)
    m, mline = mode(cfg, today)
    ml, _ = loss_of(rv, order)
    rec = _record(order, a.index, today, now, "approved" if m == "live" else "dry_run", reply=a.reply,
                  kind=rv.get("kind"), max_loss=ml, risk_check=risk.get("check_id"), mode=m,
                  stop_index=sj, stop_fingerprint=fingerprint(stop) if stop else None,
                  stop_leg=dict(stop) if stop else None)   # R38: the stop this yes covers, kept in the log
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d%s: %s." % (a.index, (" and its stop (order %d)" % sj) if stop else "",
                                                     plain(order)))
    if m == "dry_run":
        print("%s. Do not call %s. This is the input it would get:" % (mline, TOOL))
        print(json.dumps(ti, indent=2, ensure_ascii=False))
        print("Nothing to record after this: a dry run sends nothing.")
    else:
        if leg_of(order) == "exit":
            mine = [(f, l) for f, l in stop_groups(state, today).values() if f.get("symbol") == rec["symbol"]]
            placed = [l.get("order_id") for f, l in mine if l.get("status") == "placed"]
            waiting = [f.get("at") for f, l in mine if l.get("status") == "stop_ready"]
            if placed:   # R37: every one, or the one left fires after the sell and opens a short
                print("First cancel every stop this pack placed for %s today, so none can fire after this sell and "
                      "open a short: cancel_order with variety regular, order_id %s."
                      % (rec["symbol"], ", then order_id ".join(str(x) for x in placed)))
            if waiting:
                print("A stop prepared at %s has no result recorded: look in get_orders for it, and cancel it too "
                      "if it is there." % ", ".join(waiting))
            if placed or waiting:
                print("Then:")
        print("Now call %s with exactly:" % TOOL)
        print(json.dumps(ti, indent=2, ensure_ascii=False))
        print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status placed "
              "--order-id <Kite order_id> (or denied or failed). Never retry a failed call: read get_orders "
              "first (its status_message says why), because a call that timed out may still have placed it."
              % a.index)
        if stop is not None:
            print("When get_orders shows it COMPLETE (or part filled): ticket.py stop --pair %s --filled-qty Q. "
                  "Until its stops cover the filled quantity the position is UNPROTECTED." % order.get("pair"))
    if leg_of(order) == "entry":
        for x in close_gate_lines(cfg, today):
            print(x)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    state.setdefault("orders", []).append(_record(order, a.index, today, now, "declined", reply=a.reply or "no"))
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def _load_state_only(a):
    """For the commands that must work after orders.json was replaced (R38): config, risk check, log, clock."""
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    now, today = _clock()
    return cfg, risk, load_state(a.state), today, now


def cmd_result(a):
    if (a.index is None) == (a.pair is None):
        raise TicketError("say which: --index N (an entry or exit in orders.json) or --pair P (its stop)")
    if a.pair is not None:          # a stop: from the log alone (R38)
        cfg, risk, state, today, now = _load_state_only(a)
        rec = latest(state, today, pair=a.pair, leg="stop")
        if not rec or rec.get("status") != "stop_ready":
            raise TicketError("pair %s has no stop prepared (ticket.py stop) without a result today" % a.pair)
        what = "the stop of pair %s" % a.pair
    else:
        orders, cfg, risk, state, today, now = _load(a)
        order = _pick(orders, a.index)
        if leg_of(order) == "stop":
            raise TicketError("order %d is a stop: record its result with --pair %s" % (a.index, order.get("pair")))
        rec = latest(state, today, fp=fingerprint(order))
        if not rec or rec.get("status") != "approved":
            raise TicketError("order %d has no live yes recorded today; a result can only follow it" % a.index)
        what = "order %d" % a.index
    if a.status == "placed" and not a.order_id:
        raise TicketError("placed needs --order-id (Kite's order_id from the place_order reply or get_orders)")
    new = dict(rec, at=ist_iso(now), status=a.status)
    for k in ("reply", "order_id", "note"):
        new.pop(k, None)
    if a.order_id:
        new["order_id"] = a.order_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded %s as %s." % (what, a.status))
    if a.status == "unsent":
        print("Nothing was sent. Run check.py again; a fresh PASS makes it READY for a new yes.")
    if a.status == "failed":
        print("Do not retry. Read get_orders: its status_message says why Kite refused it (the kite MCP server "
              "only says \"Failed to place order\").")
    if a.status == "placed" and a.filled_qty and a.filled_price:
        side = "buy" if new.get("transaction_type") == "BUY" else "sell"
        apply_fill(a.portfolio, cfg, new["symbol"], side, a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, new["symbol"], side, a.filled_qty, a.filled_price)
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (new["symbol"], "buy" if new.get("transaction_type") == "BUY" else "sell"))
    for line in unprotected(load_state(a.state), today):
        print(line)
    return 0


def cmd_stop(a):
    """The stop the yes covered (R38: from the log), for a filled quantity the stops do not cover yet (R37)."""
    cfg, risk, state, today, now = _load_state_only(a)
    e = latest(state, today, pair=a.pair, leg="entry")
    if not e or e.get("status") != "placed":
        raise TicketError("pair %s has no entry recorded as placed today; record the entry first (ticket.py result "
                          "--index N --status placed --order-id ...)" % a.pair)
    stop = e.get("stop_leg")
    if not isinstance(stop, dict):
        raise TicketError("the yes for pair %s recorded no stop leg" % a.pair)
    # No dry-run refusal here: a placed entry is a real position (only a live yes can lead to "placed"), and
    # its stop must still be offered if the config was switched back to dry run since.
    groups = [(f, l) for f, l in stop_groups(state, today).values() if f.get("pair") == a.pair]
    waiting = [f for f, l in groups if l.get("status") == "stop_ready"]
    if waiting:
        raise TicketError("the stop prepared at %s for pair %s has no result yet: look in get_orders and record it "
                          "first (ticket.py result --pair %s --status placed|denied|failed|unsent)"
                          % (waiting[-1].get("at"), a.pair, a.pair))
    want, have = int(e.get("quantity") or 0), covered(state, today, a.pair)
    q = _f(a.filled_qty)
    if q is None or q != int(q) or not 1 <= q <= want - have:
        if have >= want:
            raise TicketError("stops already cover all %d shares of pair %s" % (want, a.pair))
        raise TicketError("--filled-qty wants the newly filled quantity from get_orders that no stop covers yet: "
                          "stops cover %d of %d, so 1 to %d" % (have, want, want - have))
    why = window_now(stop, {"kind": "exit"}, risk or {}, now)
    if why:
        raise TicketError("the stop cannot go in now: %s" % why)
    s = dict(stop, quantity=int(q))
    ti = tool_input(s)
    sid = "%s/stop-%d" % (a.pair, len(groups) + 1)
    state.setdefault("orders", []).append(_record(s, e.get("stop_index"), today, now, "stop_ready", stop_id=sid,
                                                  entry_order_id=e.get("order_id")))
    write_json(a.state, state)
    print("Stop %s, for %d shares: %s." % (sid, int(q), plain(s)))
    print("Call %s with exactly:" % TOOL)
    print(json.dumps(ti, indent=2, ensure_ascii=False))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --pair %s --status placed --order-id "
          "<Kite order_id>. Stops then cover %d of %d; if more fills later, run ticket.py stop again for it."
          % (a.pair, have + int(q), want))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock()
    date = a.date or today
    if not DATE.match(date):
        raise TicketError("--date wants a date like 2026-11-11")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price)
    print("Recorded %s %s %s at %s on %s in %s (an MIS position of that day). Cash now %s."
          % (a.side, a.qty, a.symbol.upper(), _p(a.price), date, a.portfolio, inr(pf["cash"])))
    return 0


def read_universe(path):
    """trader-scanner's universe.txt (SYMBOL|Name|tick|FO) -> {SYMBOL: (tick, fo)}."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            parts = ln.rstrip("\n").split("|")
            if len(parts) >= 4 and parts[0].strip() and not parts[0].startswith("#"):
                t = _f(parts[2])
                if t and t > 0:
                    out[parts[0].strip().upper()] = (t, parts[3].strip() == "FO")
    return out


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    now, today = _clock()
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 32")
        source = "your --qty"
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(int(_f(p.get("quantity")) or 0) for p in pf.get("positions", [])
                  if str(p.get("tradingsymbol") or "").upper() == sym and str(p.get("product") or "").upper() == "MIS"
                  and p.get("date") == today)
        source = a.portfolio
        if qty <= 0:
            raise TicketError("nothing to flatten: no MIS position of %s today in %s. Check Kite (get_positions) "
                              "and pass --qty if you do hold some" % (sym, a.portfolio))
    tick, fo = read_universe(a.universe).get(sym, (None, None))
    if a.tick is not None:
        tick = _f(a.tick)
    if not tick or tick <= 0:
        raise TicketError("no tick size for %s: pass --tick (from trader-scanner's universe.txt or Kite)" % sym)
    price = _f(a.price)
    if price is None or price <= 0:
        raise TicketError("--price wants your limit, like --price 3460.5 (MARKET orders are not used)")
    n = price / tick
    if abs(n - round(n)) > 1e-6:
        raise TicketError("--price %s is off the %s tick" % (_p(price), _num(tick)))
    order = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
             "quantity": int(qty), "product": "MIS", "order_type": "LIMIT", "price": round(price, 2),
             "validity": B0_PROVISIONAL["entry_validity"], "tag": B0_PROVISIONAL["tag"], "leg": "exit",
             "tick": tick, "fo": fo,
             "why": "closes the %s day trade by hand before 15:00 (quantity from %s)" % (sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "dry_run": mode(cfg, today)[0] == "dry_run",
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s." % (sym, plain(order)))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The quantity comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell of today's MIS position is never blocked by the day limits.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. ticket.py approve: if this pack placed a stop for %s today, cancel it first (the approve step "
          "names its order_id), then place_order with the ticket." % sym)
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock()
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" order_id " + str(r["order_id"])) if r.get("order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined",
                                                                                    "dry_run") else ""
        placed_loss = r.get("max_loss") is not None and r["status"] == "placed"
        loss = (" max loss %s" % inr(r["max_loss"])) if placed_loss else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        if r.get("status") == "unsent":
            extra += " (never sent; the yes no longer counts)"
        print("%s  %s %-10s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one ticket")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, help="an entry or exit in orders.json")
    p.add_argument("--pair", help="the pair id of a stop prepared with ticket.py stop")
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed", "unsent"])
    p.add_argument("--order-id", help="Kite's order_id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("stop", help="the stop's place_order input for the filled quantity")
    common(p)
    p.add_argument("--pair", required=True, help="the entry's pair id (show and the UNPROTECTED line give it)")
    p.add_argument("--filled-qty", required=True)
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a LIMIT sell ticket that closes a symbol's MIS position")
    p.add_argument("--symbol", required=True)
    p.add_argument("--price", required=True, help="your limit, on the stock's tick")
    p.add_argument("--qty", help="shares you hold, from get_positions (default: today's MIS position in the portfolio)")
    p.add_argument("--tick", help="the stock's tick (default: trader/universe.txt)")
    p.add_argument("--universe", default="trader/universe.txt")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdio()
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "stop": cmd_stop, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
