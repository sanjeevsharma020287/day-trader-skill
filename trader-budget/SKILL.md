---
name: trader-budget
description: >-
  Budget Manager. Sizes an NSE day trade to your budget and your max loss per
  trade. Reads the trade plan from trader-exit (entry, stop, target, the
  stock's tick), finds the whole shares that fit inside four limits (max loss
  per trade with the estimated charges, position cap, max order, cash after
  your buffer), says which limit decided it, and writes the trade's two Kite
  orders to trader/orders.json in the shape of the kite MCP server's
  place_order: a LIMIT entry and its SL stop, as a pair. A stop too far away
  for your max loss is a plain refusal, not a smaller order. Nothing is
  placed. Use when the user says "how many shares", "size this trade", "how
  much should I buy", "I have 2 lakh", "I only want to risk 2,000 a trade",
  "set my rules", "keep 10% in cash", or after /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy M&M at 3427.00, stop 3384.40") into
exact orders ("buy 32 shares, and a stop for 32"), inside rules the user sets
once. The stop decides the size: the further away the stop, the fewer shares.
It writes a proposal only. trader-risk checks it and trader-broker asks the
user yes or no. Day trading loses most people money. Sizing to a max loss
keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 3428.5 --json   # an entry limit of your own, on the tick
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=60,SBIN=40 --config split.json   # weights mode, below
```

`--now "2026-11-11 10:16"` pins the clock (India time). A plan for another
day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, tick, F&O flag, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | the stock's last price, with its source and time: an entry needs one under 15 minutes old |
| `trader/portfolio.json` | optional: its cash caps the size |
| `trader/orders.json` | the proposed entry and its stop, written with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose at the stop, charges included (default 2,000).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (2,00,000) and the smallest order (500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (6,000), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "15:00") and **flat_by** ("15:00"), all India time. The window and flat_by can only be
  made tighter: no order before 9:30 or from 3:00 PM, and everything closed by 3:00 PM, is the
  owner's rule.
- **blocked**: NSE symbols they never want to trade.
- Keep **dry_run** true. The pack is not yet proven on a real account (see the README).

A config from the old US pack (`max_loss_per_trade_usd` and the other `*_usd`
keys, `paper`, `pdt_guard`, `sec_user_agent`, `fractional`, `extended_hours`)
or a broker other than `zerodha` is refused by name, so it never half-applies.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only (Kite takes an integer quantity). The size is the smallest
of four limits:

| limit | shares |
| --- | --- |
| max loss | shares x (entry minus the stop's limit price) + the estimated charges <= `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

The two orders, in the kite MCP server's `place_order` shape:

- **entry**: `regular`, `NSE`, `BUY`, `MIS`, `LIMIT` at the plan's entry (or `--limit`), on the
  stock's tick, `DAY`, tag `traderpack`. Market orders are not used: Kite wants market protection
  the kite MCP server does not send.
- **stop**: `SELL`, `SL`, the trigger at the plan's stop, the limit one band (0.5%) below it, on the
  tick, the same quantity. It goes in after the entry fills (Kite has no bracket order for this).

The band, the tag and DAY validity are **B0-provisional**: one named set in
the shared block (`B0_PROVISIONAL`), until the owner's two real orders (B0)
prove them.

Charges are an estimate, labelled as one (Zerodha intraday equity: brokerage
the lower of 0.03% or ₹20 an order, STT 0.025% on the sell, NSE transaction
charges, SEBI fee, stamp duty 0.003% on the buy, GST). Zerodha's ₹50 + GST
fee for a position it squares off is not in it.

It refuses, and writes no order, when: there is no last price under 15
minutes old (each price carries its source and age; one with no time is of
unknown age and not used), the size is zero shares, the order is under the
minimum, reward:risk is under `min_reward_risk`, the stop is not below the
entry, the target is not above it (the move already happened), the plan is
for another day or has no tick, the symbol is blocked, or the plan is a
short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
charges estimate and the max loss at the stop. Say plainly that a stop is an
order, not a promise: in a fast market it can fill past its limit, or not at
all. Then hand over to `/trader-risk`.

## Example: the reel

M&M on Wednesday 2026-11-11. Opening-range breakout: entry 3427.00 at 10:15
AM, stop 3384.40 (the opening-range midpoint), target 3469.60 (1R), tick 0.1,
on the F&O list. The stop's limit is 3384.40 less 0.5%, down to the tick:
3367.40, so the risk is 59.60 a share. The example config: ₹2,00,000 budget,
10% cash buffer, ₹2,000 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 33 x 59.60 + ₹86.76 = ₹2,053.56 is over; 32 x 59.60 + ₹85.56 = ₹1,992.76 | **32 (decides it)** |
| position cap | 75% of ₹2,00,000 = ₹1,50,000 / 3427.00 | 43 |
| max order | ₹2,00,000 / 3427.00 | 58 |
| cash | ₹2,00,000 minus ₹20,000 = ₹1,80,000 / 3427.00 | 52 |

32 shares = ₹1,09,664. It writes the entry and its stop:

```json
{"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "BUY",
 "quantity": 32, "product": "MIS", "order_type": "LIMIT", "price": 3427.0, "validity": "DAY",
 "tag": "traderpack", "leg": "entry", "pair": "M&M-2026-11-11-101600", "tick": 0.1, "fo": true}
{"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "SELL",
 "quantity": 32, "product": "MIS", "order_type": "SL", "price": 3367.4, "trigger_price": 3384.4,
 "validity": "DAY", "tag": "traderpack", "leg": "stop", "pair": "M&M-2026-11-11-101600"}
```

(Each order also carries `plan`, `price_source` and `why`: notes for
trader-risk and trader-broker, never sent to Kite.) With a max loss of ₹50,
one share would risk 59.60 plus charges: no order.

## Weights mode (kept from v1)

`--weights RELIANCE=30,SBIN=10` or `--targets FILE` (`targets.example.json`
here is a made-up example, not a recommendation) splits a budget across a
list by weight: the cash buffer comes off the top, weights are renormalised
to 100, any stock over `max_position_pct` is pinned at the cap and its excess
shared out, slices under the minimum order are dropped, and each slice is
bought in whole shares at the price in `trader/prices.json`. Each order is a
Kite LIMIT at that price, down to the stock's tick from trader-scanner's
`universe.txt` (`--universe`). These are plain MIS buys with no stop:
trader-risk blocks them while `require_stop` is true. This pack is for day
trades.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options,
margin or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
