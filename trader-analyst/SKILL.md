---
name: trader-analyst
description: >-
  The Results Analyst. Lines up an NSE company's quarterly numbers (revenue,
  gross, operating and net margins, net income, diluted EPS), each against
  the same quarter a year earlier, with the trailing twelve months when four
  quarters in a row are there, and always links the company's newest results
  filing on NSE. The numbers come from Yahoo's free fundamentals series,
  unofficial, and the tool says so and says when they are behind the
  filings. Reads an earnings call transcript when you paste one. Use when the
  user says "how did M&M's results look", "analyse the results", "what
  changed this quarter", "is revenue growing", "compare margins", "sum up the
  call", "when are the results", or before trading a stock around its
  results.
argument-hint: "[symbols]"
---

# trader-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill lines up the
quarters, says plainly what changed, and points to the filing the company
made with NSE, which is the document that counts. For a day trader it
answers two questions before a trade: what did the last results say, and are
the next ones inside this trade?

Tool in this folder (standard library Python, no keys). Quote a symbol with
`&` in the shell: `"M&M"`.

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" "M&M"
python3 "${CLAUDE_SKILL_DIR}/facts.py" "M&M" HDFCBANK --metrics trader/metrics.json
```

## What it reads and writes

- The numbers: Yahoo Finance's fundamentals time series for `SYMBOL.NS`
  (quarterly revenue, cost of revenue, gross profit, operating income, net
  income, diluted EPS, in INR). Unofficial and free. It usually holds the
  last five quarters or so, sometimes with a quarter missing, and it does
  not say whether a figure is consolidated or standalone. Every table says
  "Yahoo, unofficial: check against the filing".
- The filing: NSE's corporate announcements for the last 120 days. The
  newest one that says "financial results for the ... ended <date>" is the
  company's own results filing (a PDF), linked every time. NSE's older
  results list stops at the quarter ended 31 December 2024 (checked
  2026-11-12), so it is not used.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net income and EPS, the latest margins and growth, the source,
  when it was fetched, the gaps, the newest filing, and whether the numbers
  are behind it). `metrics.example.json` in this folder is a run for M&M and
  HDFCBANK on 2026-11-12.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's investor relations page has it), read it and sum up what
  management said changed: guidance, margins, demand, anything they declined
  to answer.
- The next results date: look for a "Board Meeting Intimation" in
  `/trader-news` (NSE announcements) and say where it came from.

## The rules

- **The filing first.** The table is a convenience from an unofficial
  source. When the user's question turns on a number, open the linked filing
  and read it, and say which numbers came from where.
- **Behind the filings, said first.** When NSE has a results filing for a
  later quarter than Yahoo's newest, the report opens with a NOTE: the table
  does not include it, read the filing. When NSE cannot be read, the NOTE
  says so instead: the report cannot tell whether a newer result is filed
  (`behind_filings` is null in the metrics file, and the error is in it).
  A board-meeting intimation ("to consider the financial results") is not a
  results filing. A results filing whose quarter the tool cannot read is
  counted in a NOTE, and the filing it links is then "the newest this tool
  could read", not the newest.
- **Compare like with like, by date.** Year on year uses the quarter that
  ended in the same month a year earlier. If that quarter is missing, there
  is no growth figure: a gap prints as a gap, never as a neighbour's number.
  The trailing twelve months need four quarter ends three months apart.
- **Missing stays missing.** A bank has no gross profit or operating income
  at Yahoo: those margins print as "-", never as zero. The basis
  (consolidated or standalone) is "not stated by the source". Numbers in any
  currency other than INR are refused; a number with no currency on it stays
  missing.
- **No numbers is still an answer.** If Yahoo has nothing (or answers in a
  shape the tool does not know), it says there is no table and links the
  filing.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`).
2. If there is a NOTE, or the question turns on one number, open the linked
   filing and read it.
3. Write the summary: one line on the quarter (revenue, EPS, year on year
   when there is one), one on margins, one on anything unusual (a gap, a
   table behind the filings), and the link. Plain words, real numbers, no
   adjectives the numbers do not support.
4. If the next results fall inside the planned trade (they would only for a
   report during market hours: this pack holds nothing overnight), say it
   before `/trader-exit` sets the stop.

## Example: the reel's stock

`facts.py "M&M"` (run 2026-11-12, offline fixtures): quarter ended
2026-09-30, revenue ₹57,515 cr, net income ₹4,588 cr, diluted EPS ₹40.90;
gross margin 35.3 %, operating 14.6 %, net 8.0 %. No year-on-year figure for
it: Yahoo has no quarter ended 2025-09-30 (a gap, said as one). The June
2026 quarter grew revenue 26.6 % and EPS 33.4 % on June 2025. Trailing
twelve months through 2026-09-30: revenue ₹2,21,520 cr, EPS ₹172.66. The
newest results filing on NSE is for the same quarter (filed 2026-11-05),
linked.

`facts.py HDFCBANK` opens with a NOTE: NSE has a results filing for the
quarter ended 2026-09-30 (filed 2026-10-17), and Yahoo's newest quarter is
2025-06-30. The summary reads the filing, not the table. Its gross and
operating margins print as "-": a bank does not report them.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell.
- Never invents a number the source does not have, fills a gap, or reads a
  missing field as zero.
- Never logs in anywhere; one request per source per symbol, with a pause.

## Safety

Education and tooling, not financial advice. This skill only reads filings
and figures. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
