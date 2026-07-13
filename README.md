# Discord_Bot

Weekly Discord trade-summary bot. It reads trade posts from a YAGPDB-fed
channel, keeps a persistent running log, and posts a trader-by-trader summary
("Closed this week", "Options settled this week", "Open trades") with a
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

The strike and date may also come in the other order (the channel's date-first
shape), with a bare `c`/`p` suffix:

```
#Long BE 07/10/2026 235.00 P 4.9
#Long DELL 07/17/2026 385.00p 3.35 this am
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

**This channel's put labels are inverted from textbook.** A `#Long put` is a
premium **sale** (a theta play), and a `#Short put` is a **bought** put (a
directional bearish bet). Calls keep their direction. So:

| trade       | kind          | wins when… | otherwise |
|-------------|---------------|------------|-----------|
| `#Long call`  | directional long  | spot > strike (worth spot − strike) | worthless → loss of premium |
| `#Short call` | sold call         | spot ≤ strike → **win** (premium kept) | assigned — shares called away |
| `#Long put`   | theta (sold)      | spot ≥ strike → **win** (premium kept) | assigned — buy shares @ strike |
| `#Short put`  | directional short | spot < strike (worth strike − spot) | worthless → loss of premium |

The "theta" plays (`#Long put`, `#Short call`) collect premium and win when the
option expires worthless; the "directional" plays (`#Long call`, `#Short put`)
pay premium and win when it finishes in the money. A theta put finishing in the
money is **shares assigned** at a cost basis of the strike minus the premium
(then tracked as an open stock position). The wheel continues: a short call
assigned while the trader holds tracked shares of the same ticker closes those
shares at the effective sale price (strike + premium) and scores the share P&L.
Settled option wins/losses fold into the win rate. If the spot price can't be
fetched (network/`yfinance` unavailable), the position is left open (`awaiting
settlement`) and settled on a later run; fetched settlement spots are cached in
the running log so they are never refetched.

### Multi-leg spreads

Spread lines — a spread keyword plus two strikes — are tracked by their type,
read from the keyword:

| keyword | kind | direction | wins at expiry when… |
|---------|------|-----------|----------------------|
| `PCS` (put credit spread)  | credit / theta | — | spot ≥ first strike (expires worthless, keep the credit) |
| `CDS` (call debit spread)  | debit          | bullish, like a call | spot > first strike |
| `PDS` (put debit spread)   | debit          | bearish, like a put  | spot < first strike |

```
#Long CRWV via 97/96 (Jul 26) PCS for .25c credit
#Long RKLB 100/112 cds $0.80
#Short SPY lotto PDS 746/745 for 37c
```

A `via`/`spread` line with no explicit type defaults to `PCS` (the common
case). Credit spreads win by keeping the credit (buying back cheaper); debit
spreads pay a debit and win by selling higher — so early exits are scored with
the right sign either way (a stated `for +67%` is used directly when present).

## Summary extras

- Per-trader stats line: **this week's** win rate and average %/trade (only
  trades that closed this week are scored). **One data point per position**:
  partial exits don't score individually — the number of partials is tracked,
  every tranche (each partial plus the final close, or the option's expiry
  settlement) is assumed equal size, and the position scores a single win/loss
  at the equal-weighted average return.
- **"Closed this week" lists only positions that fully closed this week**, one
  round-trip line each, e.g. `**HPE**: Long @ 48.9 → Exit @ 49.7 (+1.6%, day
  trade)`. A close held 0 days is a **day trade**; longer is a **swing Nd**.
  Opens, adds, and partials of a still-open position are *not* repeated here —
  they live under Open trades — so a position never shows up twice.
- Each closed trade is marked ✅ profit / ❌ loss / ➖ scratch. When the exit
  gives a number (a price, `for 50%`, an option premium) the sign comes from
  that; otherwise the result is read from the exit's own words (`for a loss`,
  `5 dollars of profit`, `with a scratch`, `1 dollar per share`). Only ✅/❌
  count toward the weekly win rate.
- Open positions are marked to market with one batched quote fetch: stocks get
  `→ 82.3 (+6.7%)`, options show the underlying spot; open options expiring
  within 7 days are tagged `⏳ expires this week`. A position that was trimmed
  gets a one-line `· trimmed 2× (+50%, +100%)` summary rather than a bullet per
  partial. Marking is skipped silently if the quote fetch fails.

## Running

```
pip install -r requirements.txt
python weekly_summary.py --dry-run   # preview without posting
python -m unittest test_weekly_summary test_options
```

`DISCORD_BOT_TOKEN` must be set (never committed) for a live run.
