---
name: trader-broker
description: >-
  Order Executor for Zerodha (Kite) on NSE. Dry run by default, never without
  your yes. Shows each trade as one plain ticket: the LIMIT buy, its SL stop
  (placed after the buy fills, for the filled quantity), the target as an
  alert, the max loss with estimated charges, and the exact JSON for the kite
  MCP server's place_order. Records your yes word for word; then Claude's own
  Allow once / Deny card asks again. Checks the yes on the real clock: only
  09:30 to 15:00 India time, on a risk check under 15 minutes old. Real orders
  stay blocked until the owner records B0 by hand. Builds the LIMIT sell that
  closes a day trade, records fills as today's MIS position, and logs each
  entry's max loss for trader-risk's daily limit. Covers self-hosting
  zerodha/kite-mcp-server in Claude Desktop (keys in the config file, never
  in chat) and setting place_order to "Needs approval". Use when the user
  says "place the order", "buy it", "execute", "send it to Zerodha", "place
  the stop", "close my position", "flatten", "get me out", "connect Kite",
  "set up Zerodha", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. A trade reaches NSE only after four separate
gates, every time:

1. **The owner has unlocked real orders.** `"dry_run": false` in
   `trader/config.json` AND `b0_proven_on` set by hand to the date the owner
   proved the pack with two real orders (B0 in PLAN_INDIA_ZERODHA.md, R14).
   Until both hold, every ticket says DRY RUN and nothing is sent. No tool
   writes `b0_proven_on`; never write it yourself, and never suggest a date.
2. **trader-risk passed this exact order, and its stop, on the real clock in
   the last 15 minutes** (matched by fingerprint). The yes is judged on the
   real clock too (R36): only from 09:30 to 15:00 India time on an NSE trading
   day. A check made with `check.py --now` or `--today` is refused.
3. **The user typed an explicit yes for this one ticket** in the chat.
4. **The user clicked Allow once** on Claude's own permission card, which
   appears because `place_order` is set to "Needs approval".

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --order-id 251111000123456
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --index 1 --filled-qty 32
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 2 --status placed --order-id 251111000123457
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol M&M --side buy --qty 32 --price 3427
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol M&M --price 3460.5
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to the broker. It renders tickets, checks that a
reply is a real yes for that ticket, prints the exact tool input, and keeps
the log in `trader/state/broker.json`. It has no `--now`: it always reads the
real clock, in India time.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed Kite orders (trader-budget writes an entry and its stop as a pair; `flatten` replaces it) |
| `trader/config.json` | `dry_run`, and the owner's hand-written `b0_proven_on` and `close_gate_proven_on` |
| `trader/state/risk.json` | trader-risk's verdicts, with `checked_at` and `check_id` |
| `trader/state/broker.json` | every yes, no, dry run, placed, failed and filled, with each entry's max loss and Kite's order_id |
| `trader/portfolio.json` | cash and today's MIS positions, updated by `fill` and by `result --filled-qty` |
| `trader/universe.txt` | the tick and F&O flag for `flatten` (trader-scanner writes it) |

## Setup: Zerodha through a self-hosted kite-mcp-server

Zerodha's hosted MCP server (`mcp.kite.trade`) leaves out the order tools,
so orders need the open-source `zerodha/kite-mcp-server` run on the user's
own machine with their own Kite Connect app. Read from its source (commit
`5e044dc`, PLAN_INDIA_ZERODHA.md P5); not yet proven on a real account.

1. **A Kite Connect app.** The user creates one at developers.kite.trade.
   The Personal plan is free and covers orders, but has no live market data,
   so prices come from Yahoo (`trader-alerts`) unless they pay for Connect.
2. **A static IP.** Since April 2026 Zerodha takes API orders only from an IP
   the user has whitelisted in that app. The machine that places orders must
   leave the internet from it.
3. **The server.** The user builds or downloads `kite-mcp-server` (Go) from
   github.com/zerodha/kite-mcp-server.
4. **Claude Desktop.** Settings > Developer > Edit Config opens
   `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`, macOS:
   `~/Library/Application Support/Claude/`). The user adds the block in
   `claude_desktop_config.example.json` here and types their own key and
   secret into that file:

```json
{
  "mcpServers": {
    "kite": {
      "command": "C:\\path\\to\\kite-mcp-server.exe",
      "env": {
        "APP_MODE": "stdio",
        "KITE_API_KEY": "type your Kite Connect API key here, in this file only",
        "KITE_API_SECRET": "type your Kite Connect API secret here, in this file only",
        "EXCLUDED_TOOLS": "modify_order,place_gtt_order,modify_gtt_order,delete_gtt_order"
      }
    }
  }
}
```

   **Keys never go in the chat.** Never ask for them, never read this file
   back into the conversation, and if the user pastes a key anyway, tell
   them to delete that message and make a new secret in the Kite developer
   console. `EXCLUDED_TOOLS` removes tools this pack never uses: nothing in
   it modifies an order, and GTT is not for MIS.
5. **Log in each trading morning.** Kite's access token ends at 6 AM the next
   day. Ask the server for its login link (its `login` tool) and the user
   opens it in their browser and logs in there. Never ask for the password or
   the TOTP, and never log in for them.
6. **Tool permissions.** Customize > Connectors (Settings > Connectors in
   older versions), open kite, and for each tool:
   - `place_order` and `cancel_order`: **Needs approval**. Never Always allow.
   - any tool still listed that changes an order (`modify_order`, the GTT
     tools): **Blocked**.
   - read-only tools (`get_orders`, `get_order_history`, `get_order_trades`,
     `get_positions`, `get_holdings`, `get_margins`, `get_profile`,
     `get_trades`, `get_ltp`, `get_quotes`): Always allow is fine.
   The server marks no tool as destructive, so these settings are the only
   thing that tells Claude Desktop which tools need a card.
7. **Check it.** Ask Claude for `get_profile` and `get_margins` (read only)
   and confirm the account is the user's own.

In Claude Code instead of Claude Desktop, keep the order tools on ask in
`.claude/settings.json`: `"permissions": {"ask": ["mcp__kite__place_order",
"mcp__kite__cancel_order"]}`, and never put an order tool in `allow`.

### What the server does with what it is sent (read from source)

- A missing quantity, or one sent as text (`"32"`), becomes **1 share**. Send
  the ticket's JSON exactly: its numbers are JSON numbers. trader-risk blocks
  an order whose quantity, price or trigger is text.
- There is no `market_protection`, so this pack uses LIMIT and SL orders
  only, never MARKET or SL-M.
- A refusal comes back only as "Failed to place order". Kite's reason is in
  `get_orders` (`status_message`). Never retry a failed call: one that timed
  out may still have been placed.

## Steps: the entry and its stop

1. `ticket.py show`. Only READY tickets go to the user. NOT READY or BLOCKED
   goes back to `/trader-risk` (run `check.py` again, never with `--now`).
2. Show ONE ticket and ask: "Place this trade (the buy, then its stop)?
   Reply yes or no." One ticket per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for
   word>"`. "Not recorded" means the reply was not an explicit yes for this
   ticket ("sure", "yes to all", a yes naming another stock or amount). Ask
   again. Never turn a reply into a yes yourself.
4. **Dry run** (the default, and always until B0 is recorded): approve says
   "Do not call place_order". Do not call it. The ticket is done.
5. **Live:** call `place_order` with exactly the JSON approve printed. The
   card asks Allow once or Deny. Then record it: `ticket.py result --index N
   --status placed --order-id <Kite's order_id>`, or `--status denied`, or
   `--status failed` (then read `get_orders` for the reason; do not retry).
   If `show` says EXPIRED or "your yes was on an earlier check", look at
   `get_orders` first: if it went out, record what happened; only if it did
   not, record `--status unsent`, check again and ask for a new yes.
6. **The stop, after the fill.** Until the stop is placed, `show` prints
   **UNPROTECTED** for that position. Read `get_orders`: when the entry is
   COMPLETE, or partly filled, run `ticket.py stop --index N --filled-qty Q`
   with the filled quantity, call `place_order` with the JSON it prints (its
   own Allow once card; the yes already covered the pair), then `ticket.py
   result --index <the stop's number> --status placed --order-id ...`. If
   more fills later, place a second stop for the rest the same way.
7. Record the fill: `ticket.py fill --symbol M&M --side buy --qty 32 --price
   3427` (from `get_order_trades`). It goes in as today's MIS position, the
   only kind trader-risk counts as held for a sell (R35).

## The 3 pm close is not active yet

Until the owner records `close_gate_proven_on` (B4), nothing in this pack
sells or cancels at 15:00. Approve says so at every yes for an entry: an
open position stays open, and a resting BUY can still fill after 15:00.
Zerodha squares off an open MIS position at 15:12 for F&O-list stocks and
15:25 for the rest, for ₹50 + GST an order. Tell the user to close by hand
before then: cancel a resting entry in Kite, and use `flatten` below for a
position.

## Steps: getting out by hand

1. `get_positions` (read only) for what is held.
2. `ticket.py flatten --symbol M&M --price 3460.5` writes a LIMIT sell (DAY,
   MIS, tagged) for today's MIS position to `trader/orders.json`. `--qty` if
   the portfolio is not up to date, `--tick` if the stock is not in
   `trader/universe.txt`.
3. `/trader-risk`. A sell of today's MIS position is never blocked by the
   daily limits.
4. `ticket.py show`, the user's yes, `ticket.py approve`. If the pack placed
   a stop for that stock today, approve names it: **cancel the stop first**
   (`cancel_order`, variety regular, that order_id), so it cannot fire after
   the sell and open a short. Then `place_order`, then `result`.

## Example: the reel

The M&M pair from trader-budget, checked by trader-risk at 10:16 AM India
time on Wednesday 2026-11-11, shown at 10:17 in a dry run:

```
ORDER 1 of 2: DRY RUN, nothing is sent (Zerodha Kite, NSE)
  BUY 32 M&M, limit 3427.00, intraday (MIS), good for today
  its stop, order 2: SELL 32 M&M, stop-loss: trigger 3384.40, limit 3367.40, intraday (MIS), good for today; it goes in only after this buy fills, for the filled quantity
  target 3469.60: an alert, not an order
  max loss about ₹1,992.76 at the stop's limit, including ₹85.56 estimated charges (a stop can fill worse, or not at all, in a fast market)
  last price ₹3,428.50 (yahoo, 30 s old)
  READY: trader-risk PASS on the pair, waiting for your yes
  place_order input:
    {
      "variety": "regular",
      "exchange": "NSE",
      "tradingsymbol": "M&M",
      "transaction_type": "BUY",
      "quantity": 32,
      "product": "MIS",
      "order_type": "LIMIT",
      "price": 3427.0,
      "validity": "DAY",
      "tag": "traderpack"
    }
  Reply "yes" to this pair (the buy, then its stop), or "no" to skip it.
  Dry run: after the yes, Claude does not call place_order. The yes is recorded, nothing is sent.
  The 3 pm close is not active (close_gate_proven_on is not recorded): this pack will not sell this at 15:00, and a resting BUY can still fill after 15:00. Zerodha squares off an open MIS position at 15:12 (F&O list) or 15:25, for ₹50 + GST an order. Close it yourself before then.
```

(The `why` line and a note that the last price is above the limit are left
out here.) The user replies "yes"; approve records it and prints the same
JSON under "Do not call place_order". The plan, the pair id and the `why`
stay in the file; they are never sent.

## What it never does

- Never sends an order in a dry run, and never treats `"dry_run": false` alone as permission. The one
  exception: the stop for a position a live yes already opened, if the owner switched back to dry run since.
- Never writes `b0_proven_on` or `close_gate_proven_on`, or tells the user what date to put there.
- Never places an order without all four gates, and never two tickets on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes. (The 3 pm close is B4's,
  behind its own gate.)
- Never asks for, reads, stores or repeats API keys, secrets, passwords or the TOTP; never logs in for the user.
- Never calls `modify_order` or a GTT tool.
- Never retries a failed `place_order`, and never promises a fill price: a stop can fill worse, or not at all.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Dry run by default (`"dry_run": true`). The pack is not yet proven on a real account: real orders
  stay blocked until the owner records B0 by hand (see the README and PLAN_INDIA_ZERODHA.md R14).
- Nothing in this pack places an order except trader-broker, and only after trader-risk passed that
  exact order and the user said yes to it in the chat.
- Indian market only: NSE, through Zerodha, intraday (MIS). No options, margin or short selling.
- Broker keys live outside the chat, never in it.
