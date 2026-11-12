#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and NSE company announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today and a flag on anything that touches your
symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --symbols RELIANCE "M&M" SBIN
    python3 news.py --symbols "M&M" --all --hours 72 # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --symbols is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. Company names come from
trader/universe.txt (trader-scanner's `scan.py universe`) when it is there.

Sources, one request each per symbol:
  - Google News RSS for India, searched by the company's name (or the symbol
    when no name is known):
    https://news.google.com/rss/search?q="<company>"&hl=en-IN&gl=IN&ceid=IN:en
    The feed is for personal reading in a feed reader, which is what this is.
  - NSE's corporate announcements, the filings a listed company must make to
    the exchange (results, board meetings, management changes, acquisitions,
    credit ratings ...), for the last few days:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
    NSE's website API is unofficial and can refuse or rate-limit; the tool
    then says so and the headlines still run.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Times print in India time (IST). State: trader/state/news.json remembers what
you have seen (30 days), so an item shows once. Offline: --offline DIR reads
gnews-<SYMBOL>.xml, nse-ann-<SYMBOL>.json and chart-<SYMBOL>.json from DIR.
The tests use it.

Headlines are leads, not facts: open the story or the filing before acting,
and treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving
NSE_DAYS = 7    # announcements from this many days back

# NSE announcement categories ("desc") that can move a stock the same day
IMPORTANT_DESC = re.compile(
    r"financial result|outcome of board meeting|board meeting intimation|change in director|change in management|"
    r"cessation|resignation|appointment|credit rating|acquisition|amalgamation|merger|scheme of arrangement|"
    r"takeover|news clarification|dividend|buy ?back|bonus|split|insolvency|default|fraud|"
    r"suspension|delisting|pledge", re.I)
KEYWORDS = [
    ("results", r"\bresults?\b|\bearnings\b|\bQ[1-4]\s?(?:FY|F)?\d{2}\b|\bprofit\b|\brevenue\b|\bguidance\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|target price|price target|\boutperform\b|\bunderperform\b|"
                r"\bbuy rating\b|\bsell rating\b"),
    ("legal", r"\bSEBI\b|\bcourt\b|\bNCLT\b|\bprobe\b|\binvestigation\b|\bpenalty\b|\bshow[- ]cause\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bamalgamation\b|\bstake\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuy ?back\b|\bbonus\b|\bsplit\b|\bQIP\b|\brights issue\b|\bOFS\b|\bblock deal\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint|\bcessation\b|change in management"),
    ("rating", r"\bcredit rating\b|\bCRISIL\b|\bICRA\b|\bCARE Ratings\b|\bIndia Ratings\b"),
    ("risk", r"\brecall\b|\binsolven|\bdefault\b|\bdelist|\bhack|\bbreach\b|\bfraud\b|\bpledge\b"),
    ("halt", r"\bsuspen(?:d|sion)\b|\bcircuit\b|\btrading halt"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bMPC\b|\bCPI\b|\binflation\b|\bGDP\b|\bBudget\b|\bFII\b|\bFPI\b|\btariffs?\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

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


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError as exc:
            raise NewsError("%s is not valid JSON: %s" % (path, exc))
    return None


def universe_names(path="trader/universe.txt"):
    """{SYMBOL: company name} from trader-scanner's universe.txt; {} without it."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                parts = ln.rstrip("\n").split("|")
                if len(parts) >= 2 and parts[0].strip() and not parts[0].startswith("#"):
                    out[parts[0].strip().upper()] = parts[1].strip() or None
    return out


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("tradingsymbol") or p.get("symbol") or p.get("ticker")   # trader-broker's fill writes tradingsymbol
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def short_name(name):
    """"Mahindra & Mahindra Limited" -> "Mahindra & Mahindra": the words people write."""
    n = " ".join((name or "").split())
    n = re.sub(r"\s*\b(?:Limited|Ltd\.?|Ltd)\s*$", "", n, flags=re.I)
    return n.strip(" .,") or None


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))


def yahoo_symbol(symbol):
    return urllib.parse.quote(symbol.upper() + ".NS", safe=".-")


def gnews_url(symbol, name=None):
    q = '"%s"' % short_name(name) if short_name(name) else '"%s" NSE' % symbol.upper()
    return GNEWS % urllib.parse.quote(q, safe="")


def nse_url(symbol, now_utc):
    today = to_ist(now_utc).date()
    start = today - _dt.timedelta(days=NSE_DAYS)
    return NSE_ANN % (urllib.parse.quote(symbol.upper(), safe="-"), start.strftime("%d-%m-%Y"),
                      today.strftime("%d-%m-%Y"))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None. An ISO time with no
    offset is India time."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            return parse_ist(value).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def nse_time(value):
    """NSE's "11-Nov-2026 18:59:05" (India time) -> aware UTC datetime, or None."""
    try:
        local = _dt.datetime.strptime((value or "").strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return None
    return ist_to_utc(local).replace(tzinfo=_dt.timezone.utc)


def parse_gnews(data, symbol):
    """Google News RSS -> items. The title's " - Publisher" tail is kept off the title."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("headlines for %s are not valid XML: %s" % (symbol, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        src = _text(it, "source")
        if src and title.endswith(" - " + src):
            title = title[:-len(src) - 3]
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "gn:" + guid, "source": src or "Google News", "kind": "headline",
                    "symbol": symbol, "title": title, "summary": "", "link": link,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse(data, symbol):
    """NSE corporate announcements JSON -> items (a filing to the exchange)."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise NewsError("NSE announcements for %s are not JSON (NSE may have refused the request)" % symbol)
    if isinstance(doc, dict):
        doc = doc.get("data") or []
    if not isinstance(doc, list):
        raise NewsError("NSE announcements for %s are not the shape this tool knows" % symbol)
    out = []
    for a in doc:
        if not isinstance(a, dict):
            continue
        desc = (a.get("desc") or "").strip()
        text = " ".join((a.get("attchmntText") or "").split())
        sym = str(a.get("symbol") or symbol).upper()
        ident = str(a.get("seq_id") or a.get("attchmntFile") or (desc + text + str(a.get("an_dt"))))
        out.append({"id": "nse:" + ident, "source": "NSE filing", "kind": "filing", "symbol": sym,
                    "category": desc, "title": "%s: %s" % (desc or "Announcement", text or "(no text)"),
                    "summary": "", "link": a.get("attchmntFile") or "",
                    "important": bool(IMPORTANT_DESC.search(desc) or IMPORTANT_DESC.search(text)),
                    "time": nse_time(a.get("an_dt") or a.get("exchdisstime"))})
    return out


def parse_chart(data, symbol):
    """Yahoo chart JSON -> {"symbol", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % symbol)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % symbol)
    when = meta.get("regularMarketTime")
    return {"symbol": symbol, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for s in sorted(watch):
        name = "chart-%s.json" % s
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(s)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(s)), name)
            out.append(parse_chart(data, s))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (s, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for s, name in watch.items():
        e = re.escape(s)
        alts = [r"\(%s\)" % e, r"\bNSE\s*:\s*%s(?![\w&-])" % e, r"\bNSE\s+%s(?![\w&-])" % e]
        n = short_name(name)
        if n and len(n) >= 4:
            alts.append(r"\b%s\b" % re.escape(n).replace(r"\ ", r"[\s-]"))
        pats[s] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["symbol"]} if it["symbol"] in watch else set()
        for s, rx in pats.items():
            if rx.search(hay):
                touches.add(s)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and re.search(r"financial result", it.get("category", ""), re.I) \
                and "results" not in it["tags"]:
            it["tags"].insert(0, "results")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or (a headline only) same title the same day.
    Two filings with the same wording are two documents, so a filing is never merged by title."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"]]
        if it["kind"] == "headline":
            keys.append("%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                                   it["time"].date() if it["time"] else ""))
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for s in it.get("touches", []):
                if s not in hit["touches"]:
                    hit["touches"].append(s)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, now_utc, nse=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for s in sorted(watch):
        try:
            items += parse_gnews(fetcher.get(gnews_url(s, watch.get(s)), "gnews-%s.xml" % s), s)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (s, exc))
    if nse:
        for s in sorted(watch):
            try:
                items += parse_nse(fetcher.get(nse_url(s, now_utc), "nse-ann-%s.json" % s), s)
            except NewsError as exc:
                errors.append("NSE announcements for %s: %s" % (s, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["symbol"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["symbol"]
        out.append("%s %s  %-12s %-20s %s" % (mark, when, who, it["source"][:20], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story or the filing before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    try:
        return _main(argv)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="company names for the headline search")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="headlines only, no NSE announcements")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs (no offset = India time)")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    utf8_stdio()

    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if args.symbols:
        watch = {s.upper(): known.get(s.upper()) for s in args.symbols}
    else:
        watch = known
    if not watch:
        print("news: no symbols. Pass --symbols, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    names = universe_names(args.universe)
    for s in watch:
        if not watch[s]:
            watch[s] = names.get(s)
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    if now is None:
        raise NewsError("--now wants an ISO time like 2026-11-12T09:00 (India) or ...Z, not %r" % args.now)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, now.replace(tzinfo=None), nse=not args.no_nse, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(shown, state, now)  # only what this window showed: a short --hours hides nothing else
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
