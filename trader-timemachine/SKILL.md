---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of an NSE day trade before you
  risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired. Takes the plan from trader-exit (entry, stop, target), rebuilds the
  rest of the day to 3:00 PM India time 10,000 times from the stock's own
  5-minute bars over the last 60 days, and re-runs the same rule on every
  past session. Prints the odds of target first, stop first, or neither by
  3:00 PM, where the price sits then, the average R, the history, and one
  honest verdict, then hands off to /trader-budget. Use when the user says
  "run the time machine", "replay this trade", "what are the odds", "simulate
  this trade", "monte carlo", "backtest this setup", "how often does this hit
  the target", "is this a coin flip", or after /trader-exit writes a plan,
  before /trader-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither by 3:00 PM? It answers with 10,000 replays and with the real record
of the same rule on every past session. It is a simulation built from the
stock's own recent bars, not a prediction. Day trading loses most people
money, and a good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 14:45      # end each replay earlier than 3:00 PM
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol "M&M" --date 2026-11-11 \
    --entry 3427 --stop 3384.4 --target 3469.6 --entry-time "10:15 AM" --tick 0.1
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-11-11 10:15"` (India time) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by, tick |
| Yahoo chart API | one request for `SYMBOL.NS`: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the end |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 69
   five-minute bars from 9:15 to 3:00 PM India time, the part of the NSE
   session this pack trades. Every position is closed by 3:00 PM (the
   owner's rule), so the last half hour and the closing auction of F&O-list
   stocks (from 3:15 PM, where Yahoo often shows a null bar) are never used.
   Gaps, special sessions and today are left out. Every bar becomes four
   ratios to the bar before it: open, high, low and close over the previous
   close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default the plan's
   flat_by, else 3:00 PM; never later). Green, which means target first or
   above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, stops on the stock's tick) runs again on every one of those
   past sessions, with an entry only at or after 9:30 and before 3:00 PM.
   That is what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), when the plan's
day is not an NSE session for this pack, or when the plan does not make
sense (a stop above a buy's entry, an entry off the 5-minute grid, an entry
before 9:30 or from 3:00 PM). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. It ends each replay at the plan's
   flat_by, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the end, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news and circuit limits can do things those 60 days never did.
   Check `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: the reel

M&M, Wednesday 2026-11-11. The 15-minute opening range is 3,347.70 to
3,421.10. The 10:10 bar closes above it: entry 3,427.00 at 10:15 AM (bar
11), stop 3,384.40 (the range midpoint, on the 0.1 tick), target 3,469.60
(1R, risk 42.60 a share).

```
10,000 replays of the rest of the session, 10:15 AM to 3:00 PM (57 five-minute bars), seed 19.
  target first   16.1 %
  stop first     18.7 %
  neither        65.2 %   still open at 3:00 PM
  green          48.2 %   target first, or above the entry at 3:00 PM
  Price at 3:00 PM: median ₹3,425.34, 90 % of replays between ₹3,375.26 and ₹3,477.82.
  Average R: -0.029
History: the same rule on each of those 58 real sessions fired 32 times:
12 target, 10 stop, 10 neither. Average R +0.038.
Close to a coin flip: target first 16.1 % vs stop first 18.7 %.
65.2 % of the replays hit neither by 3:00 PM; the price at 3:00 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 46.3 %. Most replays hit neither by 3:00 PM, so the price at
3:00 PM decides most of them. The replays came out close to a coin flip,
and the real history was close to even. That is the honest answer for this
setup, and it is what the user should hear before `/trader-budget` sizes it.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, never replays past 3:00 PM, and
never mixes today's bars into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
