---
name: trader-risk
description: >-
  Risk Manager. Checks every NSE order against your limits and stops anything
  over them, before it can reach the broker. For a day trade: the order must
  be a Kite order this pack makes (NSE, MIS, LIMIT or SL, DAY, on the tick,
  tagged), a stop leg is required, the loss at the stop's limit plus the
  estimated charges must fit your max loss per trade, reward:risk must meet
  your minimum, the day's total risk must fit your daily loss limit, no more
  than your trades per day, a last price with a known source and age under
  15 minutes, and no new entries before 9:30 AM or from 3:00 PM India time
  or on a day with no NSE session. It also keeps the basics: blocked symbols,
  order size, position cap, cash buffer, short sales, duplicates and a daily
  order limit. Getting out is never blocked by a day limit. Prints PASS or
  BLOCK with the reason and records the verdicts so trader-broker only shows
  an order that passed. Use when the user says "check my order", "is this
  safe", "risk check", "does this break my rules", "can I take this trade",
  "how much have I risked today", or before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget passes through it. A BLOCK is final for that order:
it does not go to the user for a yes, and it does not go to the broker. Most
people who day trade lose money. These limits keep each loss and each day's
losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-11 10:16"  # pin the clock (India time)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed Kite orders (trader-budget writes an entry and its stop as a pair) |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | each stock's last price with its source and time; also values holdings |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## The owner's rules, in code

- **Indian market only:** NSE, through Zerodha (Kite), intraday (MIS), long only.
- **The window is absolute:** no order before 9:30 AM and none at or after 3:00 PM India time, on an
  NSE trading day. Weekends, NSE holidays and special sessions (Muhurat, Budget Sunday) are closed
  for this pack, and so is any year without NSE's holiday circular in the pack. `no_entries_before`,
  `no_entries_after` and `flat_by` may only make it tighter; an earlier start, a later end or a null
  blocks every order with a config reason that names the rule.

## Entries and exits

An **entry** opens or adds to a position (a BUY). An **exit** only reduces
what you hold (a SELL of shares held, such as the stop leg once its entry has
filled). Getting out must always be possible, so an exit is checked for
format, asset and short sales only. Every other rule turns into a warning for
an exit, never a BLOCK. An exit outside its session (9:15 to Zerodha's
square-off, 15:12 for the F&O list and 15:25 for the rest; an unknown F&O
flag counts as F&O) gets a warning: the broker will refuse it.

## The checks

Orders are checked in file order, as if each passed order had filled, so an
entry and its stop leg are checked together, and a batch that is fine order
by order but breaks a limit together is caught.

Every order:

| check | blocks |
| --- | --- |
| config | a key from the old US pack (`*_usd`, `paper`, `pdt_guard`, `sec_user_agent`, `fractional`, `extended_hours`), a broker other than zerodha, `allow_short` or `allow_options` true, a looser or switched-off window or flat_by, a limit that is not a plain number (`"2,000"`; null and 0 mean no limit) |
| format | variety other than regular (AMO, cover, iceberg, auction), exchange other than NSE, product other than MIS, MARKET or SL-M, validity other than DAY, a quantity that is not a whole number above 0, a price or trigger off the stock's tick, an SL with no trigger or a limit above it, disclosed quantity or TTL, the tag missing |
| asset | a symbol that is not an NSE equity symbol |
| short | a SELL of more than you hold (an MIS sell with no position is a short sale); a stop leg whose entry was blocked says so |

Every entry, on top:

| check | blocks |
| --- | --- |
| price | no last price, one with no time (age unknown), or one older than 15 minutes; the info line names the source and age |
| blocked | a symbol on your blocked list |
| size | under `min_order_inr`, over `max_order_inr` |
| position | the stock would be over `max_position_pct` of the account |
| cash | it would dip into your cash buffer, or need margin while `allow_margin` is false |
| stop | no stop leg while `require_stop` is true; a stop leg that is not an SL SELL of the same symbol, or is not a valid order itself (its format reasons are named); one with another quantity; a stop not below the entry; a last price at or under the stop's trigger (the buy fills at once and Kite refuses the stop) |
| loss | quantity x (entry limit - the stop leg's limit) + the estimated charges, over `max_loss_per_trade_inr`; with `require_stop` false and no stop leg, the whole order value is the max loss (for the daily loss too) |
| reward:risk | (target - entry) / (entry - stop trigger) under `min_reward_risk`; a target not above the entry |
| daily loss | today's entries' max losses plus this one over `max_daily_loss_inr` (win or lose, each counts in full) |
| trades | `max_trades_per_day` entries already placed today |
| duplicate | the same symbol bought twice in this batch, or already today |
| daily limit | `max_orders_per_day` orders placed today |
| window | outside 9:30 to 15:00 India time (or your tighter window), or no NSE session today |

Charges are an estimate, labelled as one (Zerodha intraday equity, the plan's
F12 rates); Zerodha's ₹50 + GST fee for a position it squares off is not in
it. The tag, DAY validity and the stop's limit band are B0-provisional: one
named set (`B0_PROVISIONAL`) in the shared block.

## Steps

1. If the price may be old, refresh it first (trader-alerts:
   `alerts.py quote M&M --write`, or Kite's LTP).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with the charges estimate, reward:risk, and how much of today's
   limits it uses. For a BLOCK, give the reason and the fix (fewer shares, a
   closer stop from trader-exit, a fresh price, or skip the trade). Never
   change a limit yourself.
4. If an order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint (the Kite fields only), so an
   edited order is not ready until it passes again.

## Example: the reel

The M&M pair from trader-budget at 10:16 AM on Wednesday 2026-11-11, on a
fresh ₹2,00,000 account with the example config:

```
PASS  #1 BUY M&M x 32 LIMIT @ 3427.00, entry (entry leg)
          last price ₹3,428.50 (yahoo, 30 s old)
          entry 3427.00, stop trigger 3384.40 (limit 3367.40), target 3469.60: risk ₹59.60 a share
          max loss ₹1,992.76 (incl. ₹85.56 estimated charges; Zerodha's ₹50 + GST square-off fee is not in it) (limit ₹2,000.00), reward:risk 1.00
          today before this: 0 of 3 trades, ₹0.00 of ₹6,000.00 daily loss
PASS  #2 SELL M&M x 32 SL @ 3367.40 trigger 3384.40, exit (stop leg)

2 passed, 0 blocked at 2026-11-11 10:16 India time. Entries until 15:00. Cash after the passed orders: ₹90,336.00 (dry run).
```

One more share and the pair is blocked:

```
BLOCK #1 BUY M&M x 33 LIMIT @ 3427.00, entry (entry leg)
        - loss: 33 shares x ₹59.60 (entry 3427.00 to the stop's limit 3367.40) + ₹86.76 charges = ₹2,053.56, over your ₹2,000.00 max loss per trade
BLOCK #2 SELL M&M x 33 SL @ 3367.40 trigger 3384.40, entry (stop leg)
        - short: the entry this stop protects (#1) was blocked, so there is nothing for it to sell; alone it would be a short sale
```

At 9:29 AM the same pair is blocked by the window (no orders before 9:30),
and at 3:00 PM by the owner's rule (no new entries from 15:00).

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its limit, or not at all, in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
