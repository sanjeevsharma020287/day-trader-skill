---
name: trader-alerts
description: >-
  Price Alerts. Rings when a price, a stop or the opening bell hits. Turns
  the trade plan into three alerts in one go (the stop, the target and "flat
  by 3:00 PM"), plus above / below / stop / target levels, percent moves
  from the previous close, any time of day, and the NSE opening bell, each
  ringing once so you are not spammed. Knows NSE hours (9:15 AM to 3:30 PM
  India time) and the 2026 NSE holidays, works out India time itself,
  fetches free delayed quotes (Yahoo chart JSON for SYMBOL.NS) one symbol at
  a time, and writes trader/prices.json for sizing and the risk check. Use
  when the user says "alert me when", "tell me if M&M hits", "watch my stop",
  "watch my trade", "remind me to close by 3", "is the market open", "when
  does the market open", "get me a price", or right after /trader-exit
  writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. The stop order (an SL order at Zerodha, once
trader-broker has placed it) does that work; the alerts tell the user what
happened and that every position has to be closed by 3:00 PM India time.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Quote a symbol with `&` in the shell: `"M&M"`.

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by 3:00 PM from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol "M&M" --stop 3384.4
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol "M&M" --target 3469.6
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 14:45 --label "flat by 2:45 PM"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote "M&M" --write            # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset m&m-plan-stop
```

`--now "2026-11-11 15:00"` pins the clock (India time); `--offline DIR`
reads saved quote files (`yahoo-<SYMBOL>.json`) instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the India clock passed `at` ("14:45") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that India date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. The plan's
flat-by alert is at 15:00 (or the plan's earlier flat_by); a plan with a
later flat_by is refused.

## Quotes

- Yahoo: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS`, an
  unofficial free JSON endpoint, one request per symbol, half a second
  apart. On a 429 it waits and tries once more; on a 429 or a 5xx after
  that it tries `query1`. It identifies itself as `day-trader-skill/2.0`.
- There is no second free source in this pack. If Yahoo is down, Claude can
  ask Kite for the last price (`get_ltp` through the kite MCP server) and
  write it into `trader/prices.json` as `{"as_of": "...Z", "prices":
  {"M&M": 3465.0}}`. That is a Claude step, not something this tool fetches.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  trader-risk only uses one under 15 minutes old.

## The NSE clock

Normal market 9:15 AM to 3:30 PM India time (UTC+5:30 all year, no summer
time), closed on weekends and on the 2026 NSE trading holidays from NSE's
circular (built into every tool). For F&O-list stocks continuous trading
ends at 3:15 PM and a closing auction follows. This pack: orders only from
9:30 AM, none from 3:00 PM, every position closed by 3:00 PM. Special
sessions (Muhurat trading, a Budget Sunday) are not sessions for this pack.
A year with no NSE calendar in the pack is treated as closed until NSE's
circular is added. The computer's own time zone is never read.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the 3:00 PM
   flat-by.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the SL order
   has probably filled: check Kite's positions and record the fill with
   trader-broker. A target alert is a prompt to decide: taking it is an exit
   through trader-broker, then trader-risk and the user's yes. "Flat by 3:00
   PM" means anything still open must close now; Zerodha squares off what is
   left (3:12 PM for an F&O-list stock, 3:25 PM for the rest, for a fee).
   Never act for them.

## Example: the reel

M&M on Wednesday 2026-11-11: entry 3,427.00 at 10:15 AM, stop 3,384.40,
target 3,469.60, flat by 15:00. `add --from-plan` writes `m&m-plan-stop`,
`m&m-plan-target` and `m&m-flat-by` for that day. A `check` reads the saved
quote (the day's last price, 3,465.00, between the stop and the target):

```
NSE OPEN (2026-11-11 10:17 India time, regular session).
Nothing hit. 3 rule(s) watched today, M&M ₹3,465.00.
```

At 3:00 PM:

```
ALERT Flat by 3:00 PM: it is 15:00 India time (set for 15:00), the market closes at 15:30. If M&M is
still open, it has to be closed now: no position is held past 3:00 PM. trader-broker builds the exit;
Zerodha squares off what is left (3:12 PM for an F&O-list stock, 3:25 PM for the rest).
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 9:15 (and says no
orders before 9:30).

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
