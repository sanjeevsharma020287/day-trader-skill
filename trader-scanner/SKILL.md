---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of NSE charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of NSE stocks that
  trade in the EQ series from NSE's own equity list, marks the F&O-list
  stocks and takes each stock's tick size from Kite's public instrument
  list, runs a pre-market scan of daily closes (20-day closing-high
  breakout, bull flag, reclaimed the 20-day average, oversold, oversold
  bounce) and an intraday opening-range breakout scan on 5-minute bars
  (range 9:15 to 9:30 AM India time), then writes trader/watchlist.json for
  trader-news and trader-entry. Free Yahoo data (SYMBOL.NS), bar by bar. Use
  when the user says "scan the market", "find me setups", "what is breaking
  out", "opening range breakouts", "scan for bull flags", "oversold stocks",
  "pre-market scan", "build my watchlist", "what should I look at today", or
  at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
2,400 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Quote a symbol with `&` in the shell: `"M&M"`.

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols "RELIANCE,TCS,M&M"  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 50` (rupees) and `--min-volume` (daily 5,00,000 shares,
orb 0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-11-11 10:15"` (India time) to pin the clock, `--offline PATH`
to read saved files, `--no-write`, `--show N`, `--json`. Exit code 0 when
the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name\|tick\|FO` a line. Read by `daily` and `orb`, and by trader-exit, trader-budget and trader-risk for the tick size and the F&O flag |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:00, and never later) |

## The universe

`universe` downloads three public files, no login needed:

1. NSE's `EQUITY_L.csv` (nsearchives.nseindia.com/content/equities): every
   listed equity with its series.
2. NSE's `fo_mktlots.csv` (nsearchives.nseindia.com/content/fo): the F&O
   list. For these stocks continuous trading ends at 3:15 PM, a closing
   auction follows, and Zerodha squares off an MIS position at 3:12 PM.
3. Kite's instrument dump (`api.kite.trade/instruments/NSE`, public): each
   stock's tick size. NSE prices move in ticks of 0.01, 0.05, 0.1, 1 or more,
   and a price off the tick is rejected.

A row is kept when its series is EQ (BE, BZ and the rest are trade-for-trade
or restricted: no intraday), its symbol is 1 to 20 of A-Z, 0-9, `&` and `-`
(M&M, BAJAJ-AUTO, 3MINDIA), and Kite's dump lists it (a stock listing
tomorrow is not there yet). On the files of 2026-11-12 this keeps 2,395
stocks, 218 of them on the F&O list.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so 2,395
symbols take 120 requests. Liquidity comes from the spark meta: last price
at least `--min-price` and last session's volume at least `--min-volume`.
Run it before the open or after the close: during the session the volume is
the day's so far, and today's daily bar is left out until 3:30 PM.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (price times volume). A holiday leaves no
bar, so the 20 sessions are trading sessions.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time. The first possible hit
is the 9:30 bar, at 9:35 AM: no order before 9:30 in any case. With a
shorter range (`--or-minutes 5`), a bar that closes before 9:30 is never a
hit; the first that can be closes at 9:30 AM, as in trader-entry and the
time machine.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:35 AM, on a
weekend, an NSE holiday or a special session, after the close, or at or
after 3:00 PM (or an earlier `no_entries_after`: an expired setup) it prints
one plain line and writes nothing.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. NSE
  stocks are `SYMBOL.NS` there (`M%26M.NS` in a URL). Bars are 5-minute
  bars, bar by bar, never tick by tick. query2 first, query1 on a 429 or
  5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 2,301 of 2,395 charts (94
  failed or had no data)". Never round a partial scan up to "the whole
  market".
- NSE's 2026 trading holidays are built in (NSE's circular); a year with no
  calendar is treated as closed.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (10:15 is the classic): `scan.py orb`. Tell the user
   how many usable charts broke their range and when, and that a later hit
   is closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is
   moving it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick
   for them.

## Example: the reel

Wednesday 2026-11-11, scan at 10:15 AM over the 218 F&O-list stocks
(`tests/fixtures/trader-scanner/wall5m-2026-11-11.json.gz`, real Yahoo
charts):

```
python3 scan.py orb --offline wall5m-2026-11-11.json.gz --now "2026-11-11 10:15" --min-price 0
Fetched 218 of 218 charts; 218 usable; 152 candidates, 152 confirmed with real highs and lows.
100 of 218 charts closed above their opening-range high by 10:15 AM:
   9:35 AM  360ONE        ₹1,171.15  range 1,148.85 to 1,168.75  +0.21% over
  ...
  10:15 AM  M&M           ₹3,427.00  range 3,347.70 to 3,421.10  +0.17% over
```

With the default ₹50 floor it is 97 (SUZLON, SAGILITY, YESBANK and IDEA
drop out). M&M's range is the 9:15 to 9:25 bars, high 3,421.10, low
3,347.70; the 10:10 bar closed at 3,427.00, 0.17 % over the high, at 10:15
AM, the last hit of the morning. The user picks M&M and Claude hands it to
`/trader-news M&M`, then `/trader-entry M&M`.

The pre-market scan the next morning (closes through Nov 11) tags M&M,
RELIANCE, SBIN and HDFCBANK a 20-day breakout, BHARTIARTL a breakout and a
bull flag, COALINDIA a reclaim of the 20-day average, and PIIND oversold
with a bounce.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
