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
  entry's max loss for trader-risk's daily limit. A guard hook checks every
  order call in code, and the 3 pm close (off until the owner proves it)
  sells this pack's positions with no yes. Covers self-hosting
  zerodha/kite-mcp-server in Claude Code (keys in .mcp.json, never in chat),
  the guard and the ask permissions. Use when the user
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
   appears because `place_order` is on ask in `.claude/settings.json`.
   Behind all four, the guard hook (`guard.py`) checks each order call in
   code and blocks anything this pack did not prepare (R7, R14).

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --order-id 251111000123456
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --pair "M&M-2026-11-11-101600" --filled-qty 32
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --pair "M&M-2026-11-11-101600" --status placed --order-id 251111000123457
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
| `trader/universe.txt` | the tick and F&O flag for `flatten` and the 3 pm close (trader-scanner writes it) |
| `trader/kite/*.json` | the kite replies the 3 pm close reads (`closer.py save`), with the time they were saved |
| `trader/close-cancels.json`, `trader/close-plan.json` | the 3 pm lines; the guard marks each one used |
| `trader/state/guard.json` | the yeses the guard has let through once |

## Setup: Zerodha through a self-hosted kite-mcp-server, in Claude Code

Zerodha's hosted MCP server (`mcp.kite.trade`) leaves out the order tools,
so orders need the open-source `zerodha/kite-mcp-server` run on the user's
own machine with their own Kite Connect app. Read from its source (commit
`5e044dc`, PLAN_INDIA_ZERODHA.md P5); not yet proven on a real account.

The pack runs in **Claude Code** (the Code tab of Claude Desktop, or the CLI)
opened on this project folder, because only there can the guard hook stop an
order tool (R7). The chat tab's `claude_desktop_config.json` has no hooks, so
the kite server is not set up there. Every step below is the user's own.

1. **A Kite Connect app.** The user creates one at developers.kite.trade.
   The Personal plan is free and covers orders, but has no live market data,
   so prices come from Yahoo (`trader-alerts`) unless they pay for Connect.
2. **A static IP.** Since April 2026 Zerodha takes API orders only from an IP
   the user has whitelisted in that app. The machine that places orders must
   leave the internet from it.
3. **The server.** The user builds or downloads `kite-mcp-server` (Go) from
   github.com/zerodha/kite-mcp-server.
4. **`.mcp.json` in the project folder**, from `mcp.example.json` here. The
   server must be named `kite`, so its tools are exactly `mcp__kite__*`. The
   user types their own key and secret into that file:

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
   console. Keep `.mcp.json` out of git. `EXCLUDED_TOOLS` removes tools this
   pack never uses: nothing in it modifies an order, and GTT is not for MIS.
5. **`.claude/settings.json` in the project folder**, from
   `settings.example.json` here. It does two things:
   - `permissions`: `place_order` and `cancel_order` on **ask** (the Allow
     once card), `modify_order` and the GTT tools on **deny**. Never put an
     order tool in `allow`.
   - `hooks`: `guard.py` as a PreToolUse hook for `place_order`,
     `modify_order` and `cancel_order`. On Windows, change `python3` in its
     command to the Python the user runs (`py` or `python`).
   Until the guard is registered for all three, ticket.py stays in dry run
   and `closer.py precheck` refuses (R7). The server marks no tool as
   destructive, so these settings and the guard are the only gates on it.
   **Then test the installed command by hand.** A hook command that does not
   start (for example `python3` where Windows has only `py`) exits with a
   code other than 2 and blocks nothing. Run the command exactly as it is in
   `.claude/settings.json`, from the project folder, on empty input:

```bash
CLAUDE_PROJECT_DIR="$PWD" bash -c 'echo "" | python3 "$CLAUDE_PROJECT_DIR/trader-broker/guard.py"; echo "exit $?"'
```

```powershell
$env:CLAUDE_PROJECT_DIR = (Get-Location).Path; '' | py "$env:CLAUDE_PROJECT_DIR\trader-broker\guard.py"; "exit $LASTEXITCODE"
```

   It must print `trader-broker guard blocked this call: the hook input is
   not an object` and `exit 2`. Anything else: fix the command first.
6. **Log in each trading morning.** Kite's access token ends at 6 AM the next
   day. Ask the server for its login link (its `login` tool) and the user
   opens it in their browser and logs in there. Never ask for the password or
   the TOTP, and never log in for them.
7. **Check it.** Ask Claude for `get_profile` and `get_margins` (read only)
   and confirm the account is the user's own.

### The guard

`guard.py` reads each `place_order`, `modify_order` and `cancel_order` call
before it runs and blocks it (exit 2) unless this pack allows it:
`modify_order` never; a `place_order` only in the Kite shape (regular, NSE,
MIS, LIMIT or SL, the tag, whole-number JSON quantity); while real orders are
locked, nothing but the stop of a real position (one prepared with
`ticket.py stop` for an entry recorded as placed); once unlocked, a BUY only
for your recorded yes, within 15 minutes of the risk check it was given on,
sent once, 09:30 to 15:00; a stop inside the session and never above what
Kite shows filled (a saved get_orders under 5 minutes old, locked or not); an
approved exit inside the session, once every stop this pack placed for that
stock was cancelled through the guard; a 3 pm line only from today's
plan, once, 15:00 to 15:25 (15:10 for the F&O list). A `cancel_order` only
for a 3 pm cancel line or a stop before an approved exit. Letting a call
through is not a yes: the Allow once card still follows in a chat session.
A hook that times out does not block (a known gap; its timeout is 30 s).
The guard judges the project it belongs to (`CLAUDE_PROJECT_DIR`, else the
folder above `trader-broker/`), never the folder a command was run from.

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
6. **The stop, after the fill.** While the stops cover less than the
   entry's quantity, `show` prints **UNPROTECTED** with the pair id and how
   many shares the stops cover ("stops cover 20 of 32"). Read `get_orders`:
   when the entry is COMPLETE, or partly filled, save that reply (`closer.py
   save orders`) and run `ticket.py stop --pair "<pair>" --filled-qty Q`
   with the filled quantity no stop covers yet (quote the pair id: `&`
   splits a shell command). It refuses more than Kite's `filled_quantity`
   for the entry minus what the stops already cover (R43). Call
   `place_order` with the JSON it prints (its own Allow once card; the yes
   already covered the pair), then `ticket.py result --pair "<pair>" --status
   placed --order-id ...`. If more fills later, run `stop` again for the new
   shares. The stop comes from the log of your yes, not from
   `trader/orders.json`, so a new plan or a flatten ticket loses nothing, and
   `stop` never covers more than the entry's quantity in all.
7. Record the fill: `ticket.py fill --symbol M&M --side buy --qty 32 --price
   3427` (from `get_order_trades`). It goes in as today's MIS position, the
   only kind trader-risk counts as held for a sell (R35).

## The 3 pm close

Until the owner records `close_gate_proven_on` by hand (after proving the
close on a real account), nothing in this pack sells or cancels at 15:00
(R17). Approve says so at every yes for an entry: an open position stays
open, and a resting BUY can still fill after 15:00. Zerodha squares off an
open MIS position at 15:12 for F&O-list stocks and 15:25 for the rest, for
₹50 + GST an order. Tell the user to close by hand before then: cancel a
resting entry in Kite, and use `flatten` below for a position.

When it is active (`"dry_run": false`, `b0_proven_on`, `close_gate_proven_on`
and the guard registered), the owner's local scheduled task "trader-close"
(cron `0 15 * * 1-5` on a machine set to India time, "Keep computer awake",
Always allow in that task only for `get_orders`, `get_positions`,
`place_order`, `cancel_order` and the two `py` commands) runs:

```bash
python3 "${CLAUDE_SKILL_DIR}/closer.py" precheck      # 0 GO, 3 REPORT ONLY, 1 nothing to do (or missed)
# get_orders -> save the reply to trader/kite/orders.reply.json, then:
python3 "${CLAUDE_SKILL_DIR}/closer.py" save orders
python3 "${CLAUDE_SKILL_DIR}/closer.py" cancels       # cancel_order for each line it prints
# get_orders and get_positions again (and get_ltp if it answers), save each, then:
python3 "${CLAUDE_SKILL_DIR}/closer.py" plan          # place_order with exactly each line's input
# get_orders again, save it, then:
python3 "${CLAUDE_SKILL_DIR}/closer.py" result        # one retry if a line did not fill: plan --retry
```

- `cancels` lists every open or part-filled BUY and every open stop this pack
  placed today (by the order_id in its log), and writes
  `trader/close-cancels.json`.
- `plan` reads the positions again after the cancels and writes one IOC LIMIT
  SELL per position this pack opened today: the quantity is the smaller of
  what is held and what this pack's BUYs filled. The limit is the last price
  (Kite's LTP, then the positions' last price, then Yahoo) minus 0.5 %, or
  1.5 % when the price is over 5 minutes old or of unknown age, on the tick.
  No price, no tick, or an F&O-list stock after 15:10: no sell, said, and
  Zerodha's square-off applies.
- Positions opened by hand are listed and left alone. A position whose
  order_id is in the log but whose order has no tag gets its own line and no
  sell.
- In REPORT ONLY (exit 3) `cancels` and `plan` print what is open and write
  nothing. After 15:25 `precheck` logs "missed". The next morning `ticket.py
  log` says which days Zerodha's square-off closed, not this pack.
- Place only the lines `closer.py` printed. The guard takes each line once,
  from a file written in the last 5 minutes, inside its window.

## Steps: getting out by hand

1. `get_positions` (read only) for what is held. Save the reply to
   `trader/kite/positions.reply.json` and run `closer.py save positions`: the
   guard lets an exit through only against positions saved in the last 5
   minutes, for no more than today's MIS quantity.
2. `ticket.py flatten --symbol M&M --price 3460.5` writes a LIMIT sell (DAY,
   MIS, tagged) for today's MIS position to `trader/orders.json`. `--qty` if
   the portfolio is not up to date, `--tick` if the stock is not in
   `trader/universe.txt`.
3. `/trader-risk`. A sell of today's MIS position is never blocked by the
   daily limits.
4. `ticket.py show`, the user's yes, `ticket.py approve`. If the pack placed
   stops for that stock today, approve names every one: **cancel each stop
   first** (`cancel_order`, variety regular, each order_id), so none can fire
   after the sell and open a short. A stop prepared with no result recorded
   is named too: look for it in `get_orders`. Then `place_order`, then
   `result`.

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
- Never places orders from a scheduled or background task: nobody is there to say yes. The one exception is the
  owner's "trader-close" task, and only the lines `closer.py` printed, once active (the owner's gates and the guard).
- Never runs the 3 pm close by hand to get around its gates, and never writes `trader/close-plan.json` or
  `trader/close-cancels.json` itself, and never edits or deletes `trader/state/guard.json` (the guard's record of
  what it let through once, and of the stops cancelled before an exit).
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
