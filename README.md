# Discord_Bot

Weekly Discord trade-summary bot. It reads trade posts from a YAGPDB-fed
channel, keeps a persistent running log, and posts a trader-by-trader summary
("Trades taken this week", "Options settled this week", "Open trades") with a
quasi win rate.

## Trade syntax

Stock trades:

```
#Long $PENG 77.15
#Short AAPL 190.00
#Exit CRWD at 187.60
#Exit partial NVDA $208.66 for over $13 profit per share.
```

Adding to a position uses the channel's existing add function:

```
#add Long ALAB at 429.54. New avg: 449.76
#Add Long ALAB. New avg: 438.97
```

The posted `New avg` is authoritative (it knows the real sizes) and replaces
the tracked entry price; without one, the add price is averaged in
equal-weight. The original open date is kept. A repeat `#Long`/`#Short`
simply refreshes the position, as before.

Partial exits are recognized from the explicit `partial` keyword or from
free text (`half of`, `swinging the rest`, `leaving quarter size on`,
`this add on`, `trimming`, ...). A stated return like `for 50%` / `for -32%`
scores an exit even when no price is given.

Option trades — a `call`/`put` (or a `c`/`p` strike suffix) plus a strike, an
expiration date, and an optional premium:

```
#Short put $SPY 400 2026-07-18 @ 3.20
#Long call NVDA 500c 8/15 for 12.00
#Long put AAPL 175p exp 8/15/2026 5.50
#Short call TSLA 250c 7/18 4.00
```

Dates may be ISO (`2026-07-18`) or US (`8/15`, `8/15/26`, `8/15/2026`); a date
without a year infers the next occurrence. The premium may be introduced by
`@`, `for`, `at`, `$`, or given bare.

Options can be closed before expiration (a roll is just an exit plus a new
open). The exit premium scores against the opening premium — buying back a
short option below the premium collected is a win:

```
#Exit NVDA 500c 8/15 @ 15.00
#Exit partial SPY 400p 2026-07-18 @ 1.10
```

A plain `#Exit TICKER price` with no contract details also works when the
trader has exactly one open option on that ticker (an open stock position on
the same ticker takes precedence, as before).

## Option settlement at expiration

Once an option's expiration date has passed, the bot fetches the underlying's
closing spot price on that date (via `yfinance`) and settles it:

| position   | spot ≥ strike (call ITM / put OTM) | spot < strike (call OTM / put ITM) |
|------------|-------------------------------------|-------------------------------------|
| Long call  | in the money (worth spot − strike)  | expired worthless → loss of premium |
| Short call | assigned — shares called away       | expired worthless → **win** (premium kept) |
| Long put   | expired worthless → loss of premium | in the money (worth strike − spot)  |
| Short put  | expired worthless → **win** (premium kept) | assigned — buy shares @ strike |

A short (cash-secured) put — a theta trade trying to expire — that finishes at
or above its strike is taken off as expiring worthless for a win; finishing
below the strike is reported as **shares assigned** at a cost basis of the
strike minus the premium collected, and those shares are then tracked as an
open stock position. The wheel continues: a short call that gets assigned
while the trader holds tracked shares of the same ticker closes those shares
at the effective sale price (strike + premium) and scores the share P&L.
Settled option wins/losses fold into the quasi win rate. If the spot price
can't be fetched (network/`yfinance` unavailable), the position is left open
(`awaiting settlement`) and settled on a later run; fetched settlement spots
are cached in the running log so they are never refetched.

## Summary extras

- Per-trader stats line: quasi win rate (all-time), average %/trade, and this
  week's W–L. **One data point per position**: partial exits don't score
  individually — the number of partials is tracked, every tranche (each
  partial plus the final close, or the option's expiry settlement) is assumed
  equal size, and the position scores a single win/loss at the equal-weighted
  average return, shown as e.g. `(+5.3%, 1 partial, held 6d)`.
- Scored exits show their % return and hold time, e.g. `(+10.0%, held 8d)`.
- A position opened AND fully closed within the same week collapses into one
  round-trip line instead of two separate bullets, e.g.
  `**HPE**: Long @ 48.9 → Exit @ 49.7 (+1.6%, held 0d)`. Adds and partial
  exits always stay on their own line; a position opened this week with no
  closing exit yet is tagged `(still open)`.
- Open positions are marked to market with one batched quote fetch: stocks get
  `→ 82.3 (+6.7%)`, options show the underlying spot; open options expiring
  within 7 days are tagged `⏳ expires this week`. Marking is skipped silently
  if the quote fetch fails.

## Running

```
pip install -r requirements.txt
python weekly_summary.py --dry-run   # preview without posting
python -m unittest test_weekly_summary test_options
```

`DISCORD_BOT_TOKEN` must be set (never committed) for a live run.
