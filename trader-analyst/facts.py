#!/usr/bin/env python3
"""Results reader for trader-analyst. Standard library only.

Lines up an NSE company's reported quarterly numbers and says what changed:
revenue, gross, operating and net margins, net income and diluted EPS,
quarter by quarter, each against the same quarter a year earlier, with the
trailing twelve months when four quarters in a row are there. It always links
the company's newest results filing on NSE, which is the document to read.

    python3 facts.py "M&M"
    python3 facts.py "M&M" HDFCBANK --quarters 8
    python3 facts.py "M&M" --json
    python3 facts.py "M&M" HDFCBANK --metrics trader/metrics.json

Sources (no keys, no login):
  - The numbers: Yahoo Finance's fundamentals time series for SYMBOL.NS
    (quarterlyTotalRevenue, quarterlyCostOfRevenue, quarterlyGrossProfit,
    quarterlyOperatingIncome, quarterlyNetIncome, quarterlyDilutedEPS):
    https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYMBOL>.NS
    Unofficial. It usually holds the last five quarters or so, sometimes with
    a quarter missing, and it does not say whether a figure is consolidated
    or standalone. Every number is printed as "Yahoo, unofficial: check
    against the filing".
  - The filing: NSE's corporate announcements for the last 120 days; the
    newest one that says "financial results for the ... ended <date>" is the
    company's own results filing, linked as a PDF.
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>&from_date=..&to_date=..
    (NSE's older results list, api/corporates-financial-results, stops at
    the quarter ended 31-Dec-2024, checked 2026-11-12, so it is not used.)

The rules it keeps:
  - Periods are matched by date, never by position. Year on year uses the
    quarter that ended in the same month one year earlier; with that quarter
    missing there is no growth figure. The trailing twelve months need four
    quarter ends exactly three months apart; a gap means no TTM.
  - A missing field stays missing. A bank has no gross profit or operating
    income at Yahoo: those margins are not computed and print as "-", never
    as zero.
  - Numbers in another currency than INR are refused.
  - When NSE has a results filing for a later quarter than Yahoo's newest,
    the first lines say so: the table is behind, read the filing.
  - No numbers at all (Yahoo empty, erroring, or a new shape) is still a
    report: the newest filing is linked and the tool says there is no table.

--metrics FILE merges one row per symbol into a JSON file (TTM revenue, net
income and EPS, growth, margins, the source, when it was fetched, the gaps
and the newest filing). Offline: --offline DIR reads yts-<SYMBOL>.json and
ann-<SYMBOL>.json from DIR.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YTS_URL = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
           "?symbol=%s&type=%s&period1=%d&period2=%d")
NSE_ANN = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
           "&from_date=%s&to_date=%s")
FILING_DAYS = 120
FIELDS = [("revenue", "quarterlyTotalRevenue"), ("cost_of_revenue", "quarterlyCostOfRevenue"),
          ("gross_profit", "quarterlyGrossProfit"), ("operating_income", "quarterlyOperatingIncome"),
          ("net_income", "quarterlyNetIncome"), ("eps_diluted", "quarterlyDilutedEPS")]
SOURCE = "Yahoo fundamentals time series (unofficial)"
CHECK = "Yahoo, unofficial: check against the filing"
BASIS = "not stated by the source (consolidated or standalone)"
IST = _dt.timedelta(hours=5, minutes=30)
MONTHS = {m.lower(): i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                                    "August", "September", "October", "November", "December"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


class FactsError(Exception):
    pass


# ---------------------------------------------------------------- dates

def month_end(year, month):
    nxt = _dt.date(year + (month == 12), month % 12 + 1, 1)
    return nxt - _dt.timedelta(days=1)


def months_before(d, n):
    """The month-end n months before the month-end d."""
    m = d.year * 12 + (d.month - 1) - n
    return month_end(m // 12, m % 12 + 1)


def is_month_end(d):
    return d == month_end(d.year, d.month)


def parse_iso_date(s):
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def parse_text_date(text):
    """"September 30, 2026", "30 June 2026", "Jun 30, 2026", "30th September 2026" -> date or None."""
    t = text.strip().rstrip(".")
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})", t)
    if m:
        day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    else:
        m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
        if not m:
            return None
        mon, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    if mon not in MONTHS:
        return None
    try:
        return _dt.date(year, MONTHS[mon], day)
    except ValueError:
        return None


def nse_time(value):
    try:
        return _dt.datetime.strptime((value or "").strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return None


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
                raise FactsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise FactsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))


def yts_url(symbol, now_utc):
    ysym = urllib.parse.quote(symbol.upper() + ".NS", safe=".-")
    end = int((now_utc - _dt.datetime(1970, 1, 1)).total_seconds())
    return YTS_URL % (ysym, ysym, ",".join(t for _, t in FIELDS), end - 4 * 366 * 86400, end)


def ann_url(symbol, now_utc):
    today = (now_utc + IST).date()
    return NSE_ANN % (urllib.parse.quote(symbol.upper(), safe="-"),
                      (today - _dt.timedelta(days=FILING_DAYS)).strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y"))


# ---------------------------------------------------------------- parsing

def parse_yts(data):
    """Yahoo fundamentals time series -> {quarter end date: {field: value}}.
    Raises FactsError on a shape it does not know or a currency other than INR."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        results = doc["timeseries"]["result"]
    except (ValueError, KeyError, TypeError):
        raise FactsError("Yahoo's answer is not the shape this tool knows")
    if not isinstance(results, list):
        raise FactsError("Yahoo's answer is not the shape this tool knows")
    names = dict((t, k) for k, t in FIELDS)
    quarters = {}
    for r in results:
        try:
            typ = r["meta"]["type"][0]
        except (KeyError, IndexError, TypeError):
            continue
        key = names.get(typ)
        if key is None:
            continue
        for v in r.get(typ) or []:
            if not v:
                continue
            cur = v.get("currencyCode")
            if cur != "INR":
                if cur:
                    raise FactsError("Yahoo reports %s in %s, not INR: refused" % (typ, cur))
                continue                   # no currency on it: the value stays missing (R21, R25)
            if v.get("periodType") not in (None, "3M"):
                continue
            d = parse_iso_date(v.get("asOfDate"))
            raw = (v.get("reportedValue") or {}).get("raw")
            if d is None or not isinstance(raw, (int, float)):
                continue
            quarters.setdefault(d, {})[key] = float(raw)
    return quarters


def parse_filings(data):
    """NSE announcements -> results filings [{period_end, filed, link, text, desc}], newest period first."""
    return read_filings(data)[0]


def read_filings(data):
    """-> (results filings newest period first, how many results rows had a quarter this tool could not read).
    A board-meeting intimation ("to consider the financial results ...") is not a results filing. An outcome
    row (desc "Outcome of Board Meeting" or "Financial Result...") is, even when it says "held ... to consider"."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise FactsError("NSE announcements are not JSON (NSE may have refused the request)")
    if isinstance(doc, dict):
        doc = doc.get("data") or []
    out, unread = [], 0
    for a in doc if isinstance(doc, list) else []:
        if not isinstance(a, dict):
            continue
        text = " ".join((a.get("attchmntText") or "").split())
        if not re.search(r"financial results", text, re.I):
            continue
        desc = (a.get("desc") or "").strip()
        outcome = re.search(r"outcome of board meeting|financial result", desc, re.I)
        if re.search(r"intimation", desc, re.I) or (
                not outcome and re.search(r"\bto consider\b|\bto be held\b", text, re.I)):
            continue                       # the board will consider results: none are filed yet
        m = re.search(r"\bended\s+(?:on\s+)?(.+?)(?:[\".]|$)", text, re.I)
        end = parse_text_date(m.group(1)) if m else None
        if end is None:
            unread += 1
            continue
        out.append({"period_end": end, "filed": nse_time(a.get("an_dt")), "link": a.get("attchmntFile") or "",
                    "text": text, "desc": desc})
    rank = lambda f: 0 if re.search(r"outcome of board meeting|financial result", f["desc"], re.I) else 1
    out.sort(key=lambda f: (-f["period_end"].toordinal(), rank(f), -(f["filed"] or _dt.datetime.min).toordinal()))
    return out, unread


# ---------------------------------------------------------------- the numbers

def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100 * (1 if b > 0 else -1), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(quarters, n=6):
    """{date: fields} -> rows newest first, the gaps, and the TTM (or None)."""
    ends = sorted(quarters, reverse=True)
    rows = []
    for d in ends[:n]:
        q = quarters[d]
        a = months_before(d, 12) if is_month_end(d) else None
        prev = quarters.get(a) if a else None
        rows.append({"period_end": d.isoformat(), "fields": dict(q),
                     "gross_margin": margin(q.get("gross_profit"), q.get("revenue")),
                     "operating_margin": margin(q.get("operating_income"), q.get("revenue")),
                     "net_margin": margin(q.get("net_income"), q.get("revenue")),
                     "yoy_vs": a.isoformat() if prev else None,
                     "revenue_yoy": pct(q.get("revenue"), (prev or {}).get("revenue")),
                     "net_income_yoy": pct(q.get("net_income"), (prev or {}).get("net_income")),
                     "eps_yoy": pct(q.get("eps_diluted"), (prev or {}).get("eps_diluted"))})
    gaps = []
    if ends:
        d = ends[0]
        while d > ends[-1]:
            d = months_before(d, 3)
            if d not in quarters and d >= ends[-1]:
                gaps.append(d.isoformat())
    ttm = None
    if len(ends) >= 4 and all(ends[i + 1] == months_before(ends[i], 3) for i in range(3)):
        four = [quarters[d] for d in ends[:4]]
        ttm = {}
        for k in ("revenue", "net_income", "eps_diluted"):
            vals = [q.get(k) for q in four]
            ttm[k] = round(sum(vals), 2) if all(v is not None for v in vals) else None
        ttm["through"] = ends[0].isoformat()
    return rows, gaps, ttm


def crore(x):
    """Rupees -> "₹57,515 cr" (Indian grouping, 1 crore = 1,00,00,000)."""
    if x is None:
        return "-"
    v = abs(x) / 1e7
    s = "{:.0f}".format(v)
    head, groups = s[:-3], [s[-3:]]
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ("-" if x < 0 else "") + "₹" + ",".join(g for g in groups if g) + " cr"


def _p(v):
    return "-" if v is None else "%.1f%%" % v


def _g(v):
    return "-" if v is None else "%+.1f%%" % v


def report(symbol, rows, gaps, ttm, filing, error=None, nse_error=None, unread=0):
    out = ["%s: quarterly results" % symbol]
    newest = rows[0]["period_end"] if rows else None
    if nse_error:
        out.append("NOTE: NSE's announcements could not be read (%s): this report cannot tell whether a newer "
                   "result is filed." % nse_error)
    if unread:
        out.append("NOTE: %d results filing%s on NSE name%s a quarter this tool could not read: the filing below "
                   "may not be the newest. Check NSE's announcements." % (unread, "" if unread == 1 else "s",
                                                                          "s" if unread == 1 else ""))
    which = "Newest results filing on NSE" if not unread else "Newest results filing this tool could read"
    if filing and (newest is None or filing["period_end"].isoformat() > newest):
        out.append("NOTE: NSE has a results filing for the quarter ended %s (filed %s) that the numbers below do "
                   "not include%s. Read the filing: %s"
                   % (filing["period_end"].isoformat(), filing["filed"].strftime("%Y-%m-%d") if filing["filed"]
                      else "date unknown", "" if rows else " (there are no numbers)", filing["link"]))
    if not rows:
        out.append("No table: %s." % (error or "Yahoo returned no quarterly numbers for %s.NS" % symbol))
        if filing and newest is None and not any("NOTE: NSE has" in l for l in out):
            out.append("%s: %s" % (which, filing["link"]))
        if not filing and not nse_error:
            out.append("No results filing found in NSE's announcements of the last %d days." % FILING_DAYS)
        out.append("Numbers only, not a recommendation.")
        return "\n".join(out)
    out.append("Source: %s; %s. Basis: %s." % (SOURCE, CHECK, BASIS))
    out.append("")
    out.append("%-10s %14s %14s %9s %9s %9s %9s %9s %9s" % ("quarter", "revenue", "net income", "EPS", "gross",
                                                            "operating", "net", "rev YoY", "EPS YoY"))
    for r in rows:
        f = r["fields"]
        eps = f.get("eps_diluted")
        out.append("%-10s %14s %14s %9s %9s %9s %9s %9s %9s" % (
            r["period_end"], crore(f.get("revenue")), crore(f.get("net_income")),
            "-" if eps is None else "₹%.2f" % eps, _p(r["gross_margin"]), _p(r["operating_margin"]),
            _p(r["net_margin"]), _g(r["revenue_yoy"]), _g(r["eps_yoy"])))
    out.append("")
    if gaps:
        out.append("Gap: no numbers for the quarter%s ended %s. Growth against a missing quarter and the trailing "
                   "twelve months across a gap are not computed." % ("s" if len(gaps) > 1 else "", ", ".join(gaps)))
    if ttm:
        out.append("Trailing twelve months through %s: revenue %s, net income %s, EPS %s."
                   % (ttm["through"], crore(ttm["revenue"]), crore(ttm["net_income"]),
                      "-" if ttm["eps_diluted"] is None else "₹%.2f" % ttm["eps_diluted"]))
    else:
        out.append("No trailing twelve months: the four newest quarters are not four in a row.")
    if filing:
        out.append("%s: quarter ended %s, filed %s: %s"
                   % (which, filing["period_end"].isoformat(),
                      filing["filed"].strftime("%Y-%m-%d %H:%M IST") if filing["filed"] else "date unknown",
                      filing["link"]))
    elif not nse_error:
        out.append("No results filing found in NSE's announcements of the last %d days." % FILING_DAYS)
    out.append("Numbers only, not a recommendation.")
    return "\n".join(out)


def metrics_row(symbol, rows, gaps, ttm, filing, fetched, error=None, nse_error=None, unread=0):
    r0 = rows[0] if rows else {}
    return {"symbol": symbol, "period_end": r0.get("period_end"),
            "revenue_ttm": (ttm or {}).get("revenue"), "net_income_ttm": (ttm or {}).get("net_income"),
            "eps_ttm": (ttm or {}).get("eps_diluted"),
            "revenue_growth_yoy": r0.get("revenue_yoy"), "net_income_growth_yoy": r0.get("net_income_yoy"),
            "eps_growth_yoy": r0.get("eps_yoy"), "gross_margin": r0.get("gross_margin"),
            "operating_margin": r0.get("operating_margin"), "net_margin": r0.get("net_margin"),
            "gaps": gaps, "basis": BASIS, "source": SOURCE, "fetched_at": fetched,
            "newest_filing": ({"period_end": filing["period_end"].isoformat(), "link": filing["link"],
                               "filed": filing["filed"].isoformat() if filing["filed"] else None} if filing else None),
            "behind_filings": None if nse_error else
            bool(filing and (not rows or filing["period_end"].isoformat() > r0["period_end"])),
            "unread_filings": unread, "error": error}


def run_one(fetcher, symbol, now_utc, n):
    """-> (text, json doc, metrics row). Never raises for missing numbers (R22)."""
    error = nse_error = None
    unread = 0
    try:
        quarters = parse_yts(fetcher.get(yts_url(symbol, now_utc), "yts-%s.json" % symbol))
    except FactsError as e:
        quarters, error = {}, str(e)
    try:
        filings, unread = read_filings(fetcher.get(ann_url(symbol, now_utc), "ann-%s.json" % symbol))
    except FactsError as e:
        filings, nse_error = [], str(e)
        error = (error + "; " if error else "") + "NSE filings: %s" % e
    rows, gaps, ttm = analyse(quarters, n)
    filing = filings[0] if filings else None
    fetched = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    metrics = metrics_row(symbol, rows, gaps, ttm, filing, fetched, error, nse_error, unread)
    doc = {"symbol": symbol, "rows": rows, "gaps": gaps, "ttm": ttm, "basis": BASIS, "source": SOURCE,
           "fetched_at": fetched, "error": error, "newest_filing": metrics["newest_filing"],
           "behind_filings": metrics["behind_filings"], "unread_filings": unread}
    yahoo_error = error.split("; NSE filings:")[0] if error and not error.startswith("NSE filings:") else None
    text = report(symbol, rows, gaps, ttm, filing, yahoo_error, nse_error, unread)
    for r in doc["rows"]:
        r["period_end"] = str(r["period_end"])
    return text, doc, metrics


def write_metrics(path, rows, now_utc):
    doc = {"as_of": None, "companies": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise FactsError("%s is not valid JSON: %s" % (path, e))
    keep = [c for c in doc.get("companies", []) if c.get("symbol") not in {r["symbol"] for r in rows}]
    doc["companies"] = sorted(keep + rows, key=lambda c: c.get("symbol") or "")
    doc["as_of"] = now_utc.strftime("%Y-%m-%d")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quarterly results for NSE stocks (trader-analyst).")
    ap.add_argument("symbols", nargs="+", help="NSE symbols, e.g. M&M HDFCBANK")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--metrics", metavar="FILE", help="merge one row per symbol into this JSON file")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time (no offset = India time), for reproducible runs")
    a = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        if a.now:
            try:
                t = _dt.datetime.fromisoformat(a.now.strip().replace("Z", "+00:00"))
            except ValueError:
                raise FactsError("--now wants an ISO time like 2026-11-12T09:00")
            now = (t - t.utcoffset()).replace(tzinfo=None) if t.tzinfo else t - IST
        else:
            now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        if a.quarters < 1:
            raise FactsError("--quarters must be at least 1")
        fetcher = Fetcher(a.offline)
        texts, docs, metrics = [], [], []
        for sym in a.symbols:
            text, doc, row = run_one(fetcher, sym.upper(), now, a.quarters)
            texts.append(text)
            docs.append(doc)
            metrics.append(row)
        if a.metrics:
            write_metrics(a.metrics, metrics, now)
        if a.json:
            print(json.dumps(docs, indent=2, ensure_ascii=False, default=str))
        else:
            print("\n\n".join(texts))
            if a.metrics:
                print("\nWrote %d row(s) to %s." % (len(metrics), a.metrics))
        return 0
    except FactsError as e:
        print("facts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
