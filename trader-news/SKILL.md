---
name: trader-news
description: >-
  News Monitor. Tracks breaking news on every NSE stock you watch and flags
  what moves it: Google News headlines for India per company, each company's
  NSE corporate announcements (results, board meetings, management changes,
  acquisitions, credit ratings) and how far each stock is from yesterday's
  close. Shows only what is new since the last check, newest first, in India
  time, tags each item (results, analyst, deal, rating, people, halt,
  macro...) and never repeats one. Use when the user says "any news on M&M",
  "why is this moving", "what's the news on my watchlist", "morning
  briefing", "pre-market news", "monitor the news", "did anything break",
  "check the exchange filings", or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
suspension or a regulator's order. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the watchlist,
and tells you only what is new. It also prints how far each stock has moved
today, so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys). Quote a symbol with
`&` in the shell: `"M&M"`.

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --symbols "M&M" SBIN --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--symbols`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | company names for the headline search (trader-scanner's `scan.py universe`) |
| Google News RSS for India | `news.google.com/rss/search?q="<company>"&hl=en-IN&gl=IN&ceid=IN:en`: free, for personal reading in a feed reader, which is what this is |
| NSE corporate announcements | `nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>` for the last 7 days: what the company filed with the exchange, with the PDF link. NSE's website API is unofficial and can refuse; then the tool says so and the headlines still run |
| Yahoo Finance chart endpoint | the last price of `SYMBOL.NS` against yesterday's close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE announcement and a headline
  disagree, the announcement is what the company told the exchange.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: results, analyst, legal, deal, capital, people, rating, risk, halt,
  macro. Filings marked `!` are the kinds that can move a stock the same
  day: financial results, a board meeting's outcome, a director or
  management change, a credit rating, an acquisition or merger, a takeover
  disclosure, a news clarification, a dividend, buyback, bonus or split.
- **Two filings are two documents.** A company sometimes files two papers
  with the same wording on one day; both are listed. Only headlines with
  the same title on the same day are merged.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged results,
   halt, legal, rating or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are due after the close: the stock can gap tomorrow, and this pack holds
   nothing overnight").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date, a suspension, a block deal. Never act on a
   headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the open.

## Example: the reel

Thursday 2026-11-12 before the open, after M&M broke its opening range on
Wednesday. The check at 9:00 AM IST over M&M and SBIN leads with the price
lines (`M&M ₹3,465.00 +5.30% vs yesterday's close ₹3,290.60 at 15:29 IST
MOVING`) and M&M's NSE filings: "Analysts/Institutional Investor Meet/Con.
Call Updates: ... Transcript of M&M Analyst Meet Q2F27" (Nov 11, 18:59 IST,
tagged results), and, with `--hours 168`, the Nov 6 "Acquisition" filing
marked `!`. The brief: "M&M's Q2 results and analyst call were last week;
the transcript is out. An acquisition was filed on Nov 6: open the PDF
before trading it." When a symbol has no news, say so rather than padding
the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, a pause between
  calls. It never logs in to anything and sends no cookies.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
