#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE NSE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes the two
Kite orders of that trade to trader/orders.json, in the shape of the kite MCP
server's place_order: an entry (BUY, LIMIT, MIS, DAY) and its stop (SELL, SL,
MIS, DAY) as a pair. It never places anything: trader-risk checks the pair,
and trader-broker asks you yes or no once for it. The stop goes in after the
entry fills (Kite has no bracket order for this; see PLAN_INDIA_ZERODHA.md
section 4.2).

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 3428.5 --write           # an entry limit of your own (on the tick)
    python3 size.py --now "2026-11-11 10:16" --json  # pin the clock (India time)

How many shares. Whole shares only (Kite takes an integer quantity).

    shares = the largest number that fits under every limit:
      max loss       shares x (entry limit - the stop's limit price) + the
                     estimated charges <= max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry, and no more than
                     the cash in trader/portfolio.json if you have one. MIS
                     leverage is not used: the pack sizes with your own cash.

The entry is a LIMIT order (market orders need market protection the kite
MCP server does not send): at the plan's entry, or your --limit, on the
stock's tick. The stop is an SL order: its trigger is the plan's stop and its
limit sits a band below it (B0-provisional, in the shared block), so the
worst fill the pair allows is known before you say yes. Zero shares is a
plain refusal: no order is written. So is a reward:risk under
min_reward_risk, or no last price for the stock (each price carries its
source and age, and an entry needs one under 15 minutes old).

Charges are an estimate, labelled as one (Zerodha intraday equity: brokerage,
STT, NSE transaction charges, SEBI fee, stamp duty, GST). Zerodha's Rs 50 +
GST fee for a position it squares off is not in it.

A config with a key from the old US pack (max_loss_per_trade_usd and the other
*_usd keys, paper, pdt_guard, sec_user_agent, fractional, extended_hours) or a
broker other than zerodha is refused by name, so it never half-applies.

Weights mode (kept from v1, no longer the headline): split a budget across a
list of NSE stocks by weight, in whole shares at the latest prices.

    python3 size.py --weights RELIANCE=30,HDFCBANK=25,INFY=20,TCS=15,SBIN=10
    python3 size.py --targets targets.example.json --budget 200000 --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   symbols are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_inr is dropped and its weight
                   shared out
  5. whole shares  each slice in whole shares at the price in
                   trader/prices.json; each order is a LIMIT at that price
                   on the stock's tick (from trader-scanner's universe.txt)

Money that cannot be invested is reported as "left idle", never hidden.
Weights-mode orders have no stop, so trader-risk blocks them while
"require_stop" is true: this pack is for day trades.
"""

import argparse
import datetime
import json
import math
import os
import sys

# The contract's config.example.json. Missing keys fall back to these (trader-risk has the same).
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 200000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 2000,
    "max_daily_loss_inr": 6000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "15:00",
    "flat_by": "15:00",
    "dry_run": True,
    "broker": "zerodha",
    "blocked": [],
    "allow_margin": False,
    "allow_short": False,
}
OLD_KEYS = {
    "max_loss_per_trade_usd": "max_loss_per_trade_inr", "max_daily_loss_usd": "max_daily_loss_inr",
    "max_order_usd": "max_order_inr", "min_order_usd": "min_order_inr", "paper": "dry_run",
    "pdt_guard": "nothing (India has no pattern-day-trader rule)", "sec_user_agent": "nothing (no SEC in this pack)",
    "fractional": "nothing (Kite takes whole shares only)", "extended_hours": "nothing (NSE has no extended session)",
}
EPS = 1e-9
FRESH_MINUTES = 15
NUMBER_KEYS = ("budget", "cash_buffer_pct", "max_position_pct", "max_order_inr", "min_order_inr",
               "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "max_orders_per_day",
               "min_reward_risk")
DEFAULT_UNIVERSE = "trader/universe.txt"
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
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
        raise SizeError("--now wants a time like \"2026-11-11 10:16\" (India) or ...Z (UTC)")


def parse_utc(text):
    """"2026-11-11T04:46:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    """DEFAULTS + trader/config.json. A config from the old US pack is refused by name."""
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        if not isinstance(doc, dict):
            raise SizeError("%s is not a JSON object" % path)
        config_refusal(doc)
        cfg.update(doc)
    return cfg


def config_refusal(cfg):
    """A config with a key from the old US pack is refused by name, so it never half-applies."""
    bad = ["%r (use %s)" % (k, v) for k, v in sorted(OLD_KEYS.items()) if k in (cfg or {})]
    if bad:
        raise SizeError("trader/config.json has keys from the old US pack: %s" % ", ".join(bad))
    if str((cfg or {}).get("broker") or "zerodha").lower() != "zerodha":
        raise SizeError("broker %r: this pack trades on NSE through Zerodha only" % cfg.get("broker"))
    for key in NUMBER_KEYS:      # R34: null and 0 mean no limit; anything else must be a plain number
        v = (cfg or {}).get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
            raise SizeError("trader/config.json: %s must be a number (or null), got %r" % (key, v))


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_weights(data):
    """Accept a weights file, a {symbol: weight} dict or a list of rows.

    Returns [{"symbol", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"symbol": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("symbol") or r.get("tradingsymbol") or r.get("ticker")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a symbol and a weight" % i)
            rows.append({"symbol": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        w = _f(r["weight"])
        if w is None:
            raise SizeError("weight for %s is not a number: %r" % (r["symbol"], r["weight"]))
        out.append({"symbol": str(r["symbol"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=30,M&M=20 -> {symbol: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        v = _f(p)
        if v is not None and v > 0:
            out[str(t).upper()] = v
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time_utc, fresh) for sym from a prices.json doc, or (None, None, False).
    A price with no time is never fresh: its age is unknown."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


def read_universe(path):
    """trader-scanner's universe.txt (SYMBOL|Name|tick|FO) -> {SYMBOL: (tick, fo)}; {} when there is none."""
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


# ---------------------------------------------------------------- money helpers

def round_paisa(x):
    """Round a price to the paisa (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(amount, per_share):
    """Whole shares that fit in amount at per_share, in exact integer paise."""
    u, p = int(round(float(amount) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def tick_down(x, tick):
    """The tick at or below x (a stop never rounds up toward the entry)."""
    n = x / tick
    if abs(n - round(n)) < 1e-6:
        n = round(n)
    return round(math.floor(n) * tick, 2)


def tick_near(x, tick):
    """The nearest tick (half up)."""
    return round(math.floor(x / tick + 0.5 + 1e-9) * tick, 2)


def _price(x):
    """3427 -> "3427.00"."""
    return "%.2f" % round_paisa(x)


def _num(x):
    """1250 -> "1250", 0.05 -> "0.05"."""
    x = round(float(x), 4)
    if abs(x - round(x)) < 0.00005:
        return str(int(round(x)))
    return ("%.4f" % x).rstrip("0")


def money(x):
    """Rupees, Indian grouping; whole rupees without the paise."""
    x = float(x)
    return inr(x, 0) if abs(x - round(x)) < 0.005 else inr(x)


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, last=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  your entry limit (None = the plan's entry)
    last   {"price", "source", "at", "age_s"}: a FRESH last price, or None
    cash   the cash in trader/portfolio.json, if there is one
    today  India date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    config_refusal(c)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit)" % name)
    tick = _f(plan.get("tick"))
    if not tick or tick <= 0:
        raise SizeError("the plan has no tick size for %s: make it again with trader-exit (it reads the tick "
                        "from trader-scanner's universe.txt)" % sym)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    ref = tick_near(limit if limit is not None else entry_plan, tick)
    ref_from = "limit" if limit is not None else "plan"
    stop_trig = tick_down(stop, tick)
    stop_lim = tick_down(stop_trig * (1 - B0_PROVISIONAL["stop_limit_band_pct"] / 100.0), tick)
    if stop_lim >= stop_trig:
        stop_lim = round(stop_trig - tick, 2)
    target = tick_near(target, tick)
    if stop_trig >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop_trig), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk = round(ref - stop_lim, 2)          # per share, to the stop's limit: the worst fill the SL allows
    rr = round((target - ref) / (ref - stop_trig), 2)

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = shares_for(max_loss, risk)
        while n > 0 and n * risk + est_charges(n, ref, stop_lim) > max_loss + 0.005:
            n -= 1
        limits.append({"name": "max loss", "inr": max_loss, "shares": n,
                       "how": "%s max loss / %s risk a share, with charges" % (money(max_loss), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": available, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    charges = est_charges(qty, ref, stop_lim) if qty else 0.0
    loss = round(qty * risk + charges, 2)
    refusal = None
    if last is None:
        refusal = ("no last price for %s under %d minutes old: get one first (trader-alerts: alerts.py quote %s "
                   "--write, or Kite's LTP). No order." % (sym, FRESH_MINUTES, sym))
    elif _f(last.get("price")) is not None and _f(last["price"]) <= stop_trig + 0.005:
        refusal = ("the last price %s is at or under the stop's trigger %s: the buy would fill at once and Kite "
                   "refuses an SL sell triggered above the last price (or it fires at once), leaving no stop. "
                   "No order." % (money(last["price"]), _price(stop_trig)))
    elif qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s plus charges (entry %s minus the stop's limit %s), more than your %s max "
                       "loss per trade. No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (money(risk), _price(ref), _price(stop_lim), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and rr < _f(c["min_reward_risk"]) - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk would "
                   "block it. No order." % (rr, _price(target), _price(ref), _price(stop_trig),
                                            _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan, "entry": ref, "entry_from": ref_from,
        "stop": stop_trig, "stop_limit": stop_lim, "target": target, "tick": tick,
        "fo": plan.get("fo") is not False, "fo_unknown": plan.get("fo") is None,
        "risk_per_share": risk, "reward_risk": rr, "stop_label": plan.get("stop_label", ""),
        "target_label": plan.get("target_label", ""), "budget": budget, "cash_buffer_pct": buf_pct,
        "cash_buffer": cash_buffer, "cash_available": available, "max_loss_per_trade_inr": max_loss,
        "limits": limits, "qty": qty, "bound_by": bound[0], "cost": cost, "charges_est": charges,
        "max_loss": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"), "last": last, "refusal": refusal,
    }


def kite_pair(s, pair_id):
    """The two Kite orders of one trade: the entry and its stop, as place_order inputs plus what they are."""
    if s["refusal"] or s["qty"] <= 0:
        return []
    base = {"variety": "regular", "exchange": "NSE", "tradingsymbol": s["symbol"], "quantity": int(s["qty"]),
            "product": "MIS", "validity": B0_PROVISIONAL["entry_validity"], "tag": B0_PROVISIONAL["tag"]}
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    entry = dict(base, transaction_type="BUY", order_type="LIMIT", price=s["entry"])
    stop = dict(base, transaction_type="SELL", order_type="SL", price=s["stop_limit"], trigger_price=s["stop"])
    meta = {"pair": pair_id, "tick": s["tick"], "fo": s["fo"],
            "plan": {"entry": s["entry"], "stop": s["stop"], "stop_limit": s["stop_limit"], "target": s["target"],
                     "risk_per_share": s["risk_per_share"], "charges_est": s["charges_est"],
                     "max_loss": s["max_loss"], "setup": s["setup"], "entry_time": s["entry_time"],
                     "flat_by": s["flat_by"]}}
    last = s["last"] or {}
    entry.update(meta, leg="entry",
                 price_source={"price": s["entry"], "from": "your --limit" if s["entry_from"] == "limit"
                               else "the plan (trader-exit)", "last": last.get("price"),
                               "last_source": last.get("source"), "last_at": last.get("at"),
                               "last_age_s": last.get("age_s")},
                 why="%s on %s: %d shares, bound by your %s; max loss %s at the stop (charges included)"
                     % (setup, s["symbol"], s["qty"], s["bound_by"], money(s["max_loss"])))
    stop.update(meta, leg="stop", price_source={"price": s["stop"], "from": "the plan's stop (trader-exit)"},
                why="the stop for the entry above: goes in once the entry fills, for the filled quantity")
    return [entry, stop]


def plan_report(s, orders, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s. Tick %s%s." % (
        _price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "", _price(s["target"]),
        (": " + s["target_label"]) if s["target_label"] else "", _num(s["tick"]),
        (", F&O list unknown (an older plan): counted as F&O, square-off 15:12" if s["fo_unknown"]
         else ", F&O list" if s["fo"] else "")))
    for n in notes:
        lines.append(n)
    lines.append("Entry limit %s (%s). The stop is an SL order: trigger %s, limit %s. Risk %s a share (entry to "
                 "the stop's limit). Reward:risk %.2f."
                 % (_price(s["entry"]), "your --limit" if s["entry_from"] == "limit" else "the plan's entry",
                    _price(s["stop"]), _price(s["stop_limit"]), _price(s["risk_per_share"]), s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if not orders:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s + %s charges (estimate) = %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), money(s["charges_est"]), money(s["max_loss"])))
    lines.append("Charges are an estimate; Zerodha's ₹50 + GST fee for a position it squares off is not in it.")
    lines.append("A stop is an order, not a promise: in a fast market it can fill past %s, or not at all."
                 % _price(s["stop_limit"]))
    lines.append("")
    lines.append("Proposed Kite orders (place_order inputs, nothing has been placed):")
    e, st = orders
    lines.append("  1. entry  BUY %d %s, LIMIT %s, MIS, %s, tag %s"
                 % (e["quantity"], e["tradingsymbol"], _price(e["price"]), e["validity"], e["tag"]))
    lines.append("  2. stop   SELL %d %s, SL trigger %s limit %s, MIS, %s: placed after the entry fills"
                 % (st["quantity"], st["tradingsymbol"], _price(st["trigger_price"]), _price(st["price"]),
                    st["validity"]))
    lines.append("Flat by %s India time: everything is closed by 3:00 PM; a resting entry is cancelled then."
                 % (s["flat_by"] or "15:00"))
    lines.append("")
    lines.append("Next: trader-risk checks the pair, then trader-broker asks you yes or no once for it.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["symbol"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["symbol"])
        seen.add(r["symbol"])
        if r["symbol"] in blocked:
            dropped.append({"symbol": r["symbol"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"symbol": r["symbol"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"symbol": r["symbol"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per stock with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({symbol: rupees}, set of capped symbols)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["symbol"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["symbol"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["symbol"]] = cap
            capped.add(r["symbol"])
            money_left -= cap
        active = [r for r in active if r["symbol"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["symbol"]] = 0.0
            break
    return alloc, capped


def whole_shares(slices, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(slices[t] / prices[t] + EPS)) if slices[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = slices[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict), in whole shares."""
    config_refusal(cfg)
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10) or 0)            # null: no buffer
    cap_pct = float(cfg.get("max_position_pct", 25) or 100)         # null: no cap
    min_order = float(cfg.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")
    missing = [r["symbol"] for r in keep if not prices.get(r["symbol"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                        "e.g. with trader-alerts: alerts.py quote %s --write)"
                        % (", ".join(missing), " ".join(missing)))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["symbol"] for r in sorted(live, key=lambda r: -exact[r["symbol"]])]
        shares = whole_shares(exact, prices, order, cap, investable)
        spend = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if spend[r["symbol"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["symbol"]))
        if shares[worst["symbol"]] == 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % money(prices[worst["symbol"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (money(spend[worst["symbol"]]),
                                                                        money(min_order))
        dropped.append({"symbol": worst["symbol"], "reason": reason})
        keep = [r for r in keep if r["symbol"] != worst["symbol"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["symbol"]
        positions.append({"symbol": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "inr": spend[t], "price": prices[t],
                          "shares": shares[t], "capped": t in capped})
    invested = round(sum(p["inr"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, ticks, source=""):
    """Weights mode: one Kite BUY LIMIT per position, at its price on the tick (never above it)."""
    missing = [p["symbol"] for p in result["positions"] if p["shares"] > 0 and p["symbol"] not in ticks]
    if missing:
        raise SizeError("no tick size for %s: run trader-scanner first (scan.py universe writes %s)"
                        % (", ".join(missing), DEFAULT_UNIVERSE))
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        tick, fo = ticks[p["symbol"]]
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        out.append({"variety": "regular", "exchange": "NSE", "tradingsymbol": p["symbol"],
                    "transaction_type": "BUY", "quantity": int(p["shares"]), "product": "MIS",
                    "order_type": "LIMIT", "price": tick_down(p["price"], tick),
                    "validity": B0_PROVISIONAL["entry_validity"], "tag": B0_PROVISIONAL["tag"],
                    "leg": "entry", "tick": tick, "fo": fo, "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    lines.append("%-12s %-22s %8s %14s %12s %7s  note" % ("symbol", "name", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        row = "%-12s %-22s %7.2f%% %14s %12s %7d" % (p["symbol"], (p["name"] or "")[:22], p["weight"],
                                                     money(p["inr"]), money(p["price"]), p["shares"])
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-12s %-22s %8s %14s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-12s %-22s %8s %14s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: whole shares do not divide evenly, or every stock is at its cap."
                     % money(r["idle"]))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["symbol"], d["reason"]))
    lines.append("")
    lines.append("Proposed Kite orders (place_order inputs, nothing has been placed):")
    for o in orders:
        lines.append("  BUY %d %s, LIMIT %s, %s, %s" % (o["quantity"], o["tradingsymbol"], _price(o["price"]),
                                                       o["product"], o["validity"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "dry_run": bool(cfg.get("dry_run", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None and a.limit != "plan":
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 3428.5")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, last = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        src = ((pdoc.get("detail") or {}).get(sym) or {}).get("source") or pdoc.get("source") or "unknown"
        if p is not None and fresh:
            age = int((now - t).total_seconds())
            last = {"price": p, "source": src, "at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "age_s": age}
            notes.append("Last price %s (%s, %s old)." % (money(p), src, "%d min" % (age // 60) if age >= 60
                                                           else "%d s" % max(age, 0)))
            entry_px = limit if limit is not None else _f(plan.get("entry"))
            if entry_px and p > entry_px + 0.005:
                notes.append("The last price is above the entry limit: the order may not fill. An unfilled entry "
                             "must be cancelled by 3:00 PM (the 3 pm close does it only once the owner records its "
                             "gate). A higher --limit is a new plan: the risk grows.")
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes, or no time on it)."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " India time")
                            if t else "of unknown age", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, last, cash, today)
    pair_id = "%s-%s-%s" % (s["symbol"], (s["date"] or today), to_ist(now).strftime("%H%M%S"))
    orders = kite_pair(s, pair_id)
    if orders and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, orders, cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": orders, "notes": notes}, indent=2, ensure_ascii=False))
    else:
        print(plan_report(s, orders, notes))
        if orders and a.write:
            print("Wrote the entry and its stop to %s" % a.write)
    return 0 if orders else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    result = size(rows, cfg, a.budget, prices, a.top)
    orders = orders_from(result, read_universe(a.universe), source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an NSE day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="the entry limit: the plan's entry (the default) or PRICE, on the stock's tick")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-11-11 10:16\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed orders (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,HDFCBANK=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"symbol\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--universe", default=DEFAULT_UNIVERSE, help="trader-scanner's universe.txt, for the ticks")
    g.add_argument("--whole-shares", action="store_true", help=argparse.SUPPRESS)  # always on now: kept for old commands
    g.add_argument("--cents", action="store_true", help=argparse.SUPPRESS)         # no meaning in whole shares
    a = ap.parse_args(argv)
    utf8_stdio()
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
