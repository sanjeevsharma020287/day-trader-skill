#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json, Kite place_order inputs from
trader-budget) and checks every one against your rules (trader/config.json),
your account (trader/portfolio.json), prices (trader/prices.json) and what was
placed today (trader/state/broker.json). Each order gets PASS or BLOCK with the
reasons. Orders are checked in file order as if each PASS had filled, so an
entry and its stop leg are checked together, and buys that are fine one by one
but break a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-11 10:16"        # pin the clock (India time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

The owner's rules, enforced here in code and not switchable:
  - Indian market only: NSE, through Zerodha (Kite). Intraday (product MIS).
  - No order before 9:30 AM or at or after 3:00 PM India time, on an NSE
    trading day (the NSE calendar is built in; special sessions such as
    Muhurat or a Budget Sunday are not trading days for this pack; a year
    with no NSE calendar is treated as closed). "no_entries_before" and
    "no_entries_after" in the config may only tighten the window; an earlier
    start, a later end or a null is a config BLOCK naming the owner's rule.

Entries and exits. An ENTRY opens or adds to a position (a BUY). An EXIT only
reduces what you hold (a SELL of shares held, such as the stop leg of an
entry once the entry has filled). Getting out must always be possible: an
exit is checked for format, asset and short sales only; every other rule
turns into a warning for it.

What blocks an order (every order):

  config        a key from the old US pack (max_loss_per_trade_usd and the other
                *_usd keys, pdt_guard, sec_user_agent, paper, fractional,
                extended_hours), "broker" other than zerodha, "allow_short": true
                (this pack is long only), "allow_options": true, a looser or switched-off entry window
                or flat_by later than 15:00: each named with what replaces it
  format        variety other than regular (amo, co, iceberg, auction), exchange
                other than NSE, product other than MIS, order_type MARKET or
                SL-M (Kite's market orders need market protection that the kite
                MCP server does not send: LIMIT and SL only), validity other
                than DAY (TTL and IOC are not for entries), a quantity that is
                not a whole number above 0, a price or trigger off the stock's
                tick, an SL without a trigger or with its limit above the
                trigger, the tag missing
  asset         a symbol that is not an NSE equity symbol
  short         a SELL of more than you hold: an MIS sell with no position is a
                short sale, and this pack is long only

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  price         no last price for the symbol, or one of unknown age or older
                than 15 minutes (each price carries its source and age)
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin while
                "allow_margin" is false (the default: MIS leverage is not used)
  duplicate     the same symbol and side twice in this batch, or already placed
                today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop leg (an SL SELL of the same quantity, paired with the
                entry) while "require_stop": true; a stop on the wrong side
  loss          quantity x (entry limit - the stop leg's limit price) plus the
                estimated charges, over max_loss_per_trade_inr
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses of today's entries plus this one over
                max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        outside 09:30 to 15:00 India time (or your tighter window),
                or not an NSE trading day

Charges (an estimate, labelled as one): Zerodha intraday equity, per the
charges page: brokerage the lower of 0.03 % or Rs 20 per executed order, STT
0.025 % on the sell, NSE transaction charges 0.00307 %, SEBI Rs 10 a crore,
stamp duty 0.003 % on the buy, GST 18 % on brokerage and those charges. The
Rs 50 + GST fee for a position Zerodha squares off is not in it.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 200000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 2000, "max_daily_loss_inr": 6000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "15:00", "flat_by": "15:00",
    "dry_run": True, "broker": "zerodha", "blocked": [], "allow_margin": False,
    "allow_short": False,
}
OLD_KEYS = {
    "max_loss_per_trade_usd": "max_loss_per_trade_inr", "max_daily_loss_usd": "max_daily_loss_inr",
    "max_order_usd": "max_order_inr", "min_order_usd": "min_order_inr", "paper": "dry_run",
    "pdt_guard": "nothing (India has no pattern-day-trader rule)", "sec_user_agent": "nothing (no SEC in this pack)",
    "fractional": "nothing (Kite takes whole shares only)", "extended_hours": "nothing (NSE has no extended session)",
}
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product", "order_type",
               "price", "trigger_price", "validity", "validity_ttl", "disclosed_quantity", "tag")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{0,19}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = 0.005
FRESH_MINUTES = 15
NUMBER_KEYS = ("budget", "cash_buffer_pct", "max_position_pct", "max_order_inr", "min_order_inr",
               "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "max_orders_per_day",
               "min_reward_risk")


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time and the calendar

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
    "close_band_wide_pct": 1.5,       # ... and under an older one, or one of unknown age (R16a)
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


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    try:
        return parse_ist(text)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-11-11 10:16\" (India) or ...Z (UTC)")


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def hhmm(value, key):
    m = HHMM.match(str(value).strip()) if value not in (None, "", False) else None
    if not m:
        raise RiskError("%s must be an India time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def config_problems(c):
    """Reasons the config itself is refused (every order is then BLOCKed with them)."""
    out = []
    for k, new in sorted(OLD_KEYS.items()):
        if k in c:
            out.append("config: %r is from the old US pack; use %s" % (k, new))
    if str(c.get("broker") or "zerodha").lower() != "zerodha":
        out.append("config: broker %r: this pack trades on NSE through Zerodha only" % c.get("broker"))
    if c.get("allow_options"):
        out.append("config: \"allow_options\": true: options stay off (this pack trades NSE equity only)")
    if c.get("allow_short"):
        out.append("config: \"allow_short\": true: this pack is long only (an MIS sell with no position is a "
                   "short sale)")
    for key in bad_numbers(c):
        out.append("config: %s must be a number (or null), got %r: a limit that is not a number is not "
                   "switched off" % (key, c[key]))
    for key, default, rule in (("no_entries_before", "09:30", "no order before 9:30"),
                               ("no_entries_after", "15:00", "no order from 3:00 PM"),
                               ("flat_by", "15:00", "every position closed by 3:00 PM")):
        if key in c and c[key] in (None, "", False):
            out.append("config: %s is switched off: the owner's rule is %s, and it cannot be switched off"
                       % (key, rule))
            continue
        try:
            t = hhmm(c.get(key, default), key)
        except RiskError as e:
            out.append("config: %s" % e)
            continue
        if key == "no_entries_before" and t < ENTRIES_FROM:
            out.append("config: no_entries_before %s is earlier than 09:30: the owner's rule is %s (it can "
                       "only be made later)" % (t.strftime("%H:%M"), rule))
        if key != "no_entries_before" and t > ENTRIES_UNTIL:
            out.append("config: %s %s is later than 15:00: the owner's rule is %s (it can only be made earlier)"
                       % (key, t.strftime("%H:%M"), rule))
    return out


def bad_numbers(c):
    """The NUMBER_KEYS present with a value that is not a plain number: "2,000", "2000", true, NaN.
    null and 0 keep their meaning (no limit)."""
    out = []
    for key in NUMBER_KEYS:
        v = c.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            out.append(key)
    return out


def format_reasons(o, tag_want):
    """The format reasons of one Kite order on its own (also used to judge an entry's stop leg)."""
    reasons = []
    side = str(o.get("transaction_type", "")).strip().upper()
    otype = str(o.get("order_type", "")).strip().upper()
    qty = o.get("quantity")
    q = _f(qty)
    tick = _f(o.get("tick")) or 0.01
    price, trig = _f(o.get("price")), _f(o.get("trigger_price"))
    if str(o.get("variety", "")).lower() != "regular":
        reasons.append("format: variety %r: only regular orders (no AMO, cover, iceberg or auction)"
                       % o.get("variety"))
    if str(o.get("exchange", "")).upper() != "NSE":
        reasons.append("format: exchange %r: this pack trades on NSE only" % o.get("exchange"))
    if str(o.get("product", "")).upper() != "MIS":
        reasons.append("format: product %r: intraday (MIS) only" % o.get("product"))
    if side not in ("BUY", "SELL"):
        reasons.append("format: transaction_type must be BUY or SELL, got %r" % o.get("transaction_type"))
    if otype not in ("LIMIT", "SL"):
        reasons.append("format: order_type %r: LIMIT and SL only (MARKET and SL-M need market protection "
                       "the kite MCP server does not send)" % o.get("order_type"))
    if str(o.get("validity", "")).upper() != B0_PROVISIONAL["entry_validity"]:
        reasons.append("format: validity %r: DAY only (IOC is the 3 pm close's, TTL waits for B0)"
                       % o.get("validity"))
    if q is None or q <= 0 or q != int(q) or isinstance(qty, bool):
        reasons.append("format: quantity must be a whole number above 0, got %r" % (qty,))
    texts = [k for k in ("quantity", "price", "trigger_price") if isinstance(o.get(k), str)]
    if texts:   # P5, read from source: the kite MCP server reads a text quantity as 1 share, a text price as none
        reasons.append("format: %s must be a JSON number, not text (the kite MCP server would read text "
                       "quantity as 1 share)" % ", ".join(texts))
    if price is None or price <= 0:
        reasons.append("format: a %s order needs a price above 0" % (otype or "LIMIT"))
    elif not on_tick(price, tick):
        reasons.append("format: price %s is off the %s tick" % (_p(price), _num(tick)))
    if otype == "SL":
        if trig is None or trig <= 0:
            reasons.append("format: an SL order needs a trigger_price")
        else:
            if not on_tick(trig, tick):
                reasons.append("format: trigger_price %s is off the %s tick" % (_p(trig), _num(tick)))
            if price is not None and side == "SELL" and price > trig + PAISA:
                reasons.append("format: an SL sell's limit %s is above its trigger %s" % (_p(price), _p(trig)))
    if o.get("disclosed_quantity") or o.get("validity_ttl") or o.get("iceberg_legs"):
        reasons.append("format: disclosed quantity, TTL and iceberg fields are not used by this pack")
    if B0_PROVISIONAL["tag_required"] and o.get("tag") != tag_want:
        reasons.append("format: tag %r: every order this pack places carries the tag %r" % (o.get("tag"), tag_want))
    return reasons


def window(now_ist, c):
    """(cut-off "HH:MM" or None on a closed day, [reasons an entry cannot be placed now])."""
    d, t = now_ist.date(), now_ist.time()
    ok, why = nse_trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return None, ["window: no NSE session for this pack today (%s, %s India time)" % (why, stamp)]
    try:
        before = max(ENTRIES_FROM, hhmm(c.get("no_entries_before", "09:30"), "no_entries_before"))
        after = hhmm(c.get("no_entries_after", "15:00"), "no_entries_after")
    except RiskError:
        before, after = ENTRIES_FROM, ENTRIES_UNTIL
    cut = min(ENTRIES_UNTIL, (datetime.datetime.combine(d, after) + datetime.timedelta(minutes=1)).time())
    if t < before:
        return cut.strftime("%H:%M"), ["window: no orders before %s India time (it is %s)"
                                       % (before.strftime("%H:%M"), t.strftime("%H:%M"))]
    if t >= cut:
        return cut.strftime("%H:%M"), ["window: no new entries from %s India time (it is %s)%s"
                                       % (cut.strftime("%H:%M"), t.strftime("%H:%M"),
                                          ": the owner's rule" if cut == ENTRIES_UNTIL else "")]
    return cut.strftime("%H:%M"), []


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact Kite order that was checked."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    for k, v in core.items():   # R39: numbers by value (3427 and 3427.0 are the same order); text stays text
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            core[k] = float(v)
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _p(x):
    return "%.2f" % float(x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def on_tick(price, tick):
    """True when price is a whole number of ticks (to a hundredth of a paisa)."""
    n = price / tick
    return abs(n - round(n)) < 1e-6


def account(cfg, portfolio, prices, today=None):
    """Cash, what counts as held for a sell, and what each position is worth.
    R35: only an MIS position from today (product "MIS", date today) is held for a sell. A CNC holding, an
    older position or one with no product is valued in the account but never makes an MIS sell an exit:
    at Zerodha that sell would be an intraday short."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: nothing to check the orders "
                            "against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("symbol") or p.get("tradingsymbol") or p.get("ticker") or "").upper()
        if not t:
            continue
        n = _f(p.get("quantity", p.get("shares", p.get("qty")))) or 0
        if str(p.get("product") or "").upper() == "MIS" and today and str(p.get("date") or "") == today:
            shares[t] = shares.get(t, 0) + n
        elif n:
            warnings.append("%s: %s shares (product %s, from %s) are not held for an intraday sell; only today's "
                            "MIS positions are" % (t, _num(n), p.get("product") or "not recorded",
                                                   p.get("date") or "an unrecorded day"))
        px = (prices.get(t) or {}).get("price")
        avg = _f(p.get("average_price"))
        values[t] = values.get(t, 0.0) + n * (px or avg or 0)
        if not px:
            warnings.append("no price for %s: valued at %s" % (t, "its average price" if avg else "zero"))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def placed_today(state, today):
    return [r for r in (state or {}).get("orders", []) if r.get("status") == "placed" and rec_day(r) == today]


def load_prices(pdoc):
    """prices.json -> {SYM: {"price", "source", "at" (naive UTC or None)}}. The time is the quote's own
    (detail.time), else when it was fetched (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}
    prices = pdoc.get("prices", {})
    if not isinstance(prices, dict):
        return {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    out = {}
    for s, p in prices.items():
        v = _f(p)
        if v is None or v <= 0:
            continue
        d = detail.get(s) or detail.get(str(s).upper()) or {}
        out[str(s).upper()] = {"price": v, "source": d.get("source") or pdoc.get("source") or "unknown",
                               "at": parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of}
    return out


def price_label(pr, now_utc):
    """"₹3,427.00 (yahoo, 1 min old)", "... (kite_ltp, age unknown)"."""
    if pr["at"] is None:
        age = "age unknown"
    else:
        s = int((now_utc - pr["at"]).total_seconds())
        age = ("%d min old" % (s // 60)) if s >= 60 else ("%d s old" % max(s, 0))
    return "%s (%s, %s)" % (inr(pr["price"]), pr["source"], age)


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    prices  {SYM: {"price", "source", "at"}} from load_prices
    now     naive UTC (default: the real clock); its India date is "today" """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = prices or {}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    cfg_bad = config_problems(dict(cfg or {}))
    for key in bad_numbers(c):
        c[key] = DEFAULTS[key]      # only so the arithmetic runs: cfg_bad already BLOCKs every order
    try:
        acct, top_warn = account(c, portfolio, prices, today)
    except RiskError:
        if not cfg_bad:
            raise
        acct, top_warn = {"cash": 0.0, "shares": {}, "values": {}}, []   # a bad "budget": BLOCKed above
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    entries_today = [r for r in done_today if r.get("kind") == "entry"]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s) has no max loss in the log; not counted in the daily loss"
                            % r.get("symbol"))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("transaction_type", r.get("side", ""))).upper())
                   for r in done_today if r.get("kind") == "entry"}
    cut, win_reasons = window(now_ist, c)
    if not c.get("dry_run", True):
        top_warn.append("LIVE (\"dry_run\": false): these orders would use real money once the owner has "
                        "recorded B0 (b0_proven_on)")
    top_warn.extend(cfg_bad)
    tag_want = B0_PROVISIONAL["tag"]
    stops = {}
    for j, o in enumerate(orders, 1):
        if str(o.get("leg") or "") == "stop" and o.get("pair"):
            stops[str(o["pair"])] = (j, o)
    seen, results, blocked_pairs = set(), [], {}
    batch_entries, batch_loss = 0, 0.0

    for i, o in enumerate(orders, 1):
        reasons, warns, info, later = list(cfg_bad), [], [], []
        sym = str(o.get("tradingsymbol", "")).strip().upper()
        side = str(o.get("transaction_type", "")).strip().upper()
        otype = str(o.get("order_type", "")).strip().upper()
        qty = o.get("quantity")
        q = _f(qty)
        price, trig = _f(o.get("price")), _f(o.get("trigger_price"))
        leg = str(o.get("leg") or "")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}

        # format
        reasons.extend(format_reasons(o, tag_want))

        # asset
        if sym and not SYMBOL.match(sym):
            reasons.append("asset: %r is not an NSE equity symbol" % sym)
        elif not sym:
            reasons.append("format: no tradingsymbol")

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        kind = "exit" if (side == "SELL" and q is not None and held > 1e-9 and q <= held + 1e-9) else "entry"
        if side == "SELL" and kind == "entry" and leg == "stop" and str(o.get("pair") or "") in blocked_pairs:
            reasons.append("short: the entry this stop protects (#%d) was blocked, so there is nothing for it to "
                           "sell; alone it would be a short sale" % blocked_pairs[str(o.get("pair"))])
        elif side == "SELL" and kind == "entry":
            reasons.append("short: selling %s %s with %s held; an MIS sell with no position is a short sale and "
                           "this pack is long only. If you bought it, record the fill first (ticket.py fill)"
                           % (_num(q or 0), sym, _num(held)))
        value = q * price if (q and price) else None

        max_loss = risk_ps = rr = stop = target = charges = last_px = None
        if kind == "entry" and side == "BUY":
            pr = prices.get(sym)
            if pr is None:
                later.append("price: no last price for %s; get one first (trader-alerts: alerts.py quote %s "
                             "--write, or Kite's LTP)" % (sym, sym))
            elif pr["at"] is None:
                later.append("price: the last price %s has no time: age unknown, not used for an entry"
                             % price_label(pr, now_utc))
            else:
                age = (now_utc - pr["at"]).total_seconds()
                if not -60 <= age <= FRESH_MINUTES * 60:
                    later.append("price: the last price %s is older than %d minutes" % (price_label(pr, now_utc),
                                                                                       FRESH_MINUTES))
                else:
                    last_px = pr["price"]
                    info.append("last price %s" % price_label(pr, now_utc))
                    if price and pr["price"] > price + PAISA:
                        warns.append("the last price %s is above your limit %s: the order may not fill. An unfilled "
                                     "entry must be cancelled by 3:00 PM (the 3 pm close does it only once the owner "
                                     "records its gate)" % (inr(pr["price"]), inr(price)))
            if sym in blocked:
                later.append("blocked: %s is on your blocked list" % sym)
            if value is not None:
                if value < float(c.get("min_order_inr") or 0) - PAISA:
                    later.append("size: %s is under your %s minimum order" % (inr(value), inr(c["min_order_inr"])))
                if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                    later.append("size: %s is over your %s max order" % (inr(value), inr(c["max_order_inr"])))
                cash = acct["cash"]
                total = cash + sum(acct["values"].values())
                buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
                after_cash = cash - value
                if after_cash < -PAISA and not c.get("allow_margin"):
                    later.append("cash: needs %s but only %s cash; MIS leverage is not used (\"allow_margin\": false)"
                                 % (inr(value), inr(cash)))
                elif after_cash < buffer_inr - PAISA:
                    later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                                 % (inr(max(after_cash, 0)), _num(c["cash_buffer_pct"]), inr(buffer_inr)))
                pos_after = acct["values"].get(sym, 0.0) + value
                cap = total * float(c.get("max_position_pct") or 100) / 100.0
                if total > 0 and pos_after > cap + PAISA:
                    later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                                 % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total),
                                    _num(c["max_position_pct"])))
            sj, sleg = stops.get(str(o.get("pair") or ""), (None, None))
            stop_limit = _f(sleg.get("price")) if sleg else None
            stop = _f(sleg.get("trigger_price")) if sleg else None
            target = _f(plan.get("target"))
            lim = _f(c.get("max_loss_per_trade_inr"))
            if sleg is None:
                if c.get("require_stop", True):
                    later.append("stop: no stop leg. Every entry needs one (\"require_stop\": true): an SL SELL of "
                                 "the same quantity, paired with it (trader-budget writes both)")
                elif value is not None:
                    max_loss = round(value, 2)      # R33: with no stop the whole order is at risk
                    info.append("no stop leg: the whole order value %s counts as its max loss" % inr(max_loss))
                    if lim and max_loss > lim + PAISA:
                        later.append("loss: no stop leg, so the whole order value %s is the max loss, over your %s "
                                     "max loss per trade" % (inr(max_loss), inr(lim)))
            elif (str(sleg.get("tradingsymbol", "")).strip().upper() != sym
                  or str(sleg.get("transaction_type", "")).strip().upper() != "SELL"
                  or str(sleg.get("order_type", "")).strip().upper() != "SL"):
                later.append("stop: the stop leg #%d is not an SL SELL of %s (it is %s %s %s)"
                             % (sj, sym, sleg.get("order_type"), sleg.get("transaction_type"),
                                sleg.get("tradingsymbol")))
            elif format_reasons(sleg, tag_want):
                later.append("stop: the stop leg #%d is not a valid order (%s), so this entry would have no stop"
                             % (sj, "; ".join(x[len("format: "):] for x in format_reasons(sleg, tag_want))))
            elif _f(sleg.get("quantity")) != q:      # (a missing trigger or limit is a format reason above)
                later.append("stop: the stop leg's quantity %s is not the entry's %s" % (sleg.get("quantity"), qty))
            elif price and stop_limit and stop and stop >= price - PAISA:
                later.append("stop: the stop %s must be below the entry %s" % (_p(stop), _p(price)))
            elif last_px is not None and stop and last_px <= stop + PAISA:
                later.append("stop: the last price %s is at or under the stop's trigger %s: the buy would fill at "
                             "once and Kite refuses an SL sell triggered above the last price (or it fires at once), "
                             "leaving a position with no stop" % (inr(last_px), _p(stop)))
            elif price and stop_limit and q:
                risk_ps = round(price - stop_limit, 2)
                charges = est_charges(int(q), price, stop_limit)
                max_loss = round(q * risk_ps + charges, 2)
                if lim and max_loss > lim + PAISA:
                    later.append("loss: %s shares x %s (entry %s to the stop's limit %s) + %s charges = %s, over "
                                 "your %s max loss per trade" % (_num(q), inr(risk_ps), _p(price), _p(stop_limit),
                                                                 inr(charges), inr(max_loss), inr(lim)))
                min_rr = _f(c.get("min_reward_risk"))
                if target:
                    if target <= price + PAISA:
                        later.append("target: the target %s must be above the entry %s" % (_p(target), _p(price)))
                    else:
                        rr = round((target - price) / (price - stop), 2)
                        if min_rr and rr < min_rr - 1e-9:
                            later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your minimum %s"
                                         % (rr, _p(target), _p(price), _p(stop), _num(min_rr)))
                elif min_rr:
                    later.append("reward:risk: no target to measure it against (min_reward_risk %s)" % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(_f(c.get("max_trades_per_day")) or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if (sym, side) in seen:
                later.append("duplicate: a second BUY %s in this batch" % sym)
            if (sym, side) in placed_keys:
                later.append("duplicate: a BUY %s entry was already placed today" % sym)
            if stop and risk_ps is not None:
                info.append("entry %s, stop trigger %s (limit %s), target %s: risk %s a share"
                            % (_p(price), _p(stop), _p(stop_limit), _p(target) if target else "none", inr(risk_ps)))
                info.append("max loss %s (incl. %s estimated charges; Zerodha's ₹50 + GST square-off fee is not "
                            "in it)%s, reward:risk %s"
                            % (inr(max_loss), inr(charges), (" (limit %s)" % inr(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "", ("%.2f" % rr) if rr is not None
                               else "n/a"))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))
            seen.add((sym, side))

        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(_f(c.get("max_orders_per_day")) or 0)
        if cap_n and len(done_today) + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, your limit is %d a day"
                         % (len(done_today), passed_so_far, cap_n))
        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)
            now_t = now_ist.time()
            last = SQUARE_OFF if o.get("fo") is False else SQUARE_OFF_FO   # unknown counts as F&O: the earlier time
            if not nse_trading_day(now_ist.date())[0] or now_t < NSE_OPEN or now_t >= last:
                warns.append("exit outside the session for it (9:15 to %s): the broker will refuse it; Zerodha's "
                             "square-off closes an MIS position" % last.strftime("%H:%M"))

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "BLOCK" and leg == "entry" and o.get("pair"):
            blocked_pairs[str(o["pair"])] = i
        if verdict == "PASS" and q:
            if kind == "entry" and side == "BUY":
                batch_entries += 1
                batch_loss += max_loss or 0.0
                acct["cash"] -= value or 0
                acct["values"][sym] = acct["values"].get(sym, 0.0) + (value or 0)
                acct["shares"][sym] = held + q
            elif side == "SELL":
                acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind, "leg": leg or None, "pair": o.get("pair"),
            "quantity": qty, "order_type": otype, "price": price, "trigger_price": trig,
            "value_inr": round(value, 2) if value is not None else None,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "charges_est": charges,
            "max_loss": max_loss, "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
            "entries_until": cut if kind == "entry" else None,
        })

    return {
        "date": today, "now_india": now_ist.strftime("%Y-%m-%d %H:%M"), "entries_until": cut,
        "dry_run": bool(c.get("dry_run", True)), "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn, "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": len(done_today), "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = ["WARNING: " + w for w in res["warnings"]]
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        what = "%s %s x %s %s%s" % (r["side"], r["symbol"], r["quantity"], r["order_type"],
                                    (" @ %s" % _p(r["price"])) if r["price"] else "")
        if r["trigger_price"]:
            what += " trigger %s" % _p(r["trigger_price"])
        lines.append("%-5s #%d %s, %s%s" % (r["verdict"], r["index"], what, r["kind"],
                                           (" (%s leg)" % r["leg"]) if r["leg"] else ""))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s India time. Entries until %s. Cash after the passed orders: %s (%s)."
                 % (res["pass"], res["block"], res["now_india"], res["entries_until"] or "never today (no session)",
                    inr(res["cash_after"]), "dry run" if res["dry_run"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-11-11 10:16\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdio()
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-11")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, load_prices(pdoc), state, a.today, now)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["check_id"] = os.urandom(8).hex()  # trader-broker binds each yes to this check
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
