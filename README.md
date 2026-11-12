# The Day Trader skill

10 Claude skills for day trading on NSE: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a rupee.

> **Not proven on a real account yet.** This pack runs in **dry run** (it
> shows the exact order and sends nothing) until its owner records the first
> two real orders by hand (`b0_proven_on`, PLAN_INDIA_ZERODHA.md R14). **The 3
> pm close is off** until its own gate is recorded (`close_gate_proven_on`,
> R17): until then Zerodha's square-off at 15:12 / 15:25 closes anything you
> leave open, for ₹50 + GST an order.

Indian market only: NSE equity, intraday (MIS), long only, through Zerodha.
Free and MIT. The tools are plain Python that read free public data: Yahoo
Finance charts (`.NS`), NSE's own lists and announcements, Kite's public
instruments list and Google News for India. Orders go through Zerodha's own
open-source MCP server, run on your machine with your Kite Connect app.

One scans thousands of charts for setups. Another tracks breaking news on
every stock you watch. One watches a setup bar by bar and tells you the moment
to get in, and another sets your stop and your target before you do. And the
craziest one is the Time Machine: before you risk a rupee, it replays the rest
of the session 10,000 times from the stock's own recent bars, plus every past
day the same setup fired, and tells you the honest odds.

No entry goes through until you say yes. The window is the owner's rule, in
code: no order before 9:30 AM, no new entry from 3:00 PM India time, and every
position closed by 3:00 PM. The one order this pack can place with no yes is
the 3 pm close of its own positions, and that is off until its gate is
recorded.

> **Looking for the original Trader skill?** The US pack this one grew from
> lives in [github.com/Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill).

## Install

Clone or download this repository (the one you are reading), then, from its
folder, copy the skill folders into Claude Code:

```bash
cp -r trader-* ~/.claude/skills/
```

They show up as `/trader-scanner` and so on. For one project only, copy them
into that project's `.claude/skills/` instead. The upstream repository
credited above is the US pack, not this code: installing from it gives you
the US broker and none of these gates.

The tools need Python 3.8 or newer. Nothing to pip install. To place real
orders (after B0) the pack runs in Claude Code on this project folder, with
the kite server in `.mcp.json` and the guard hook in `.claude/settings.json`:
`/trader-broker` walks through it.

## The ten

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans NSE charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks headlines and NSE filings on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in, from 9:30 AM until 3:00 PM. |
| `/trader-exit` | Exit Planner | Sets your stop and your target before you enter, on the stock's tick, flat by 3:00 PM. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired. |
| `/trader-risk` | Risk Manager | Checks every Kite order against your limits, the window and the charges, and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every trade to your budget and your max loss, charges included, as a LIMIT buy and its stop. |
| `/trader-broker` | Order Executor | Sends approved orders to Zerodha, dry run by default. No entry without your yes; the one exception is the 3 pm close of its own positions, off until its gate. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, the opening bell or 3:00 PM hits. |
| `/trader-analyst` | Earnings Analyst | Reads quarterly results and sums up what changed. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote. Add `trader/` to your project's `.gitignore`.

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 3:00 PM                                         (while it is open)
news / analyst                                                                 (on a schedule)
```

The reel: Mahindra & Mahindra (M&M), Wednesday 2026-11-11, real NSE data.

1. **`/trader-scanner`** at 10:15 AM IST: of the 218 F&O-list charts, 100 had
   closed above their 15-minute opening range (97 with the default ₹50 price
   floor). M&M is one: range 3,347.70 to 3,421.10, the 10:10 bar closed at
   3,427.00, 0.17 % over the top.
2. **`/trader-news`** reads M&M's NSE filings and Indian headlines. The check
   the next morning led with M&M's move and the transcript of its Q2 analyst
   meet, and flagged an acquisition filed on Nov 6: open the PDF before
   trading it.
3. **`/trader-entry`** watched M&M bar by bar. At 10:12 it said waiting (the
   10:10 bar was still forming). At 10:15 it said triggered: the first
   5-minute close above the range, 3,427.00.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 3,384.40 (on M&M's 0.1 tick), so 42.60 a share at risk; target
   one times that risk, 3,469.60; flat by 3:00 PM either way. M&M is on the
   F&O list, so Zerodha's square-off for it is 3:12 PM.
5. **`/trader-timemachine`** replayed the rest of the day to 3:00 PM 10,000
   times from M&M's own 5-minute bars (seed 19): target first 16.1 %, stop
   first 18.7 %, neither 65.2 %. The same rule on 58 real sessions fired 32
   times: 12 target, 10 stop, 10 neither. Its verdict: close to a coin flip.
   That is the point of looking before you risk a rupee.
6. **`/trader-budget`** with a ₹2,00,000 budget and a ₹2,000 max loss per
   trade: the stop goes in as an SL order with its limit at 3,367.40 (0.5 %
   under the trigger), so the worst fill it allows is 59.60 a share. 32 shares
   x 59.60 + ₹85.56 estimated charges = ₹1,992.76. It writes two Kite orders:
   a LIMIT buy of 32 at 3,427.00, and the SL stop.
7. **`/trader-risk`** checks the pair: NSE, MIS, LIMIT and SL, on the tick,
   tagged; the max loss with charges; the daily loss and trades per day;
   reward to risk; a last price under 15 minutes old with its source; and the
   window. PASS. One more share and it is BLOCKed: ₹2,053.56 is over ₹2,000.
8. **`/trader-broker`** shows one ticket for the pair in plain words with
   the exact `place_order` JSON. In the dry run it records your yes and sends
   nothing. Once the owner unlocks real orders: you type yes, Claude's "Allow
   once / Deny" card asks again, the buy goes in, and the stop goes in after
   the buy fills, for the filled quantity. A guard hook checks every order
   call in code.
9. **`/trader-alerts`** rings on the stop, the target and at 3:00 PM to close
   whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                       # NSE equity + the F&O list -> trader/universe.txt
python3 trader-scanner/scan.py daily                          # before the open: whole universe
python3 trader-scanner/scan.py orb                            # opening-range breakouts so far (after 9:35 AM)
python3 trader-news/news.py --symbols "M&M" SBIN --hours 12   # headlines and NSE filings
python3 trader-entry/entry.py check "M&M" --write             # waiting / triggered / expired
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write   # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write         # 10,000 replays + the history
python3 trader-budget/size.py --write                         # -> trader/orders.json (the buy and its stop)
python3 trader-risk/check.py                                  # PASS / BLOCK per order
python3 trader-broker/ticket.py show                          # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan               # stop, target, flat by 3:00 PM
python3 trader-alerts/alerts.py check                         # rings what hit, once
python3 trader-analyst/facts.py "M&M"                         # quarters, growth, EPS, the newest NSE filing
```

Quote `"M&M"` in a shell: `&` splits a command.

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real NSE data: M&M's 5-minute
bars for 2026-11-11 and the 60 days before, the 10:15 AM scan of 218 F&O-list
charts, NSE's lists and M&M's NSE filings. The headlines in them are made up
(synthetic), so no news text is copied. They reproduce every number in the
example above.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
Most people who day trade lose money: SEBI's study of FY23 found that 71 % of
individual intraday traders in the equity cash segment made a net loss (SEBI
press release, 24 July 2024). You can lose money. Decide for yourself, and
talk to a SEBI-registered adviser if you need advice.

**No entry without your yes.** Only `/trader-broker` can lead to an order,
and only after `/trader-risk` passes that exact order on the real clock and
you type yes for it in the chat. `place_order` stays on "ask" so Claude's
own "Allow once / Deny" card is a second check, and the guard hook blocks any
order call this pack did not prepare. The one exception is the 3 pm close,
which sells only this pack's own positions of the day, and only once its
owner has proved it and recorded the gate. No options, margin or short
selling: the pack is long only, intraday.

**Dry run first.** `"dry_run": true` is the default. Real orders also need
`b0_proven_on`, which only the owner writes, by hand, after the first two
real orders worked. Keys live in your `.mcp.json`, never in the chat.

**Zerodha's rules.** Orders through the API need a static IP whitelisted in
your Kite Connect app, and a login in your browser each trading day. Zerodha
squares off open MIS positions at 3:12 PM (F&O-list stocks) or 3:25 PM, for
₹50 + GST an order. The charges this pack shows are an estimate of Zerodha's
intraday charges; the square-off fee is not in them.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a circuit, a headline. Treat its odds as a reality
check, never as a promise.

**A stop is a plan, not a guarantee.** The stop is an SL order with a limit
under its trigger. In a fast market it can fill worse than the trigger, or
not at all if the price falls through its limit. The max loss shown is the
loss at the stop's limit.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. NSE's pages can refuse a
request. Google News's feed is for personal reading in a feed reader, which
is how the news skill uses it; publishing the pack for others is the owner's
decision. The scanner and the entry timer read 1-minute or 5-minute bars, not
every tick. Check anything that matters in Kite before you act on it.

**Taxes.** Intraday equity trading is generally taxed in India as speculative
business income, apart from capital gains, and losses from it can only be set
off against speculative income. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, guard.py, closer.py, mcp.example.json, settings.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
tests/              unittest suite + offline fixtures
PLAN_INDIA_ZERODHA.md   the move to NSE and Zerodha: findings, rulings, the B0 checks still open
```

MIT. Made by [Jake Schincariol](https://opusjake.ai); moved to NSE and Zerodha
in this fork.
