#!/usr/bin/env python3
"""Options handling for the weekly trade-summary bot.

Two capabilities layered on top of the plain-stock parser in weekly_summary.py:

1. Parsing single-leg option trades out of a trade line, in either strike-
   then-date order:

       #Short put $SPY 400 2026-07-18 @ 3.20
       #Long call NVDA 500c 8/15 for 12.00
       #Long put AAPL 175p exp 8/15/2026 5.50

   or date-then-strike order, this channel's other common shape (a bare c/p
   suffix, with or without a space, no "call"/"put" word):

       #Long BE 07/10/2026 235.00 P 4.9
       #Long DELL 07/17/2026 385.00p 3.35 this am
       #long NBIS sold 7/2/26 $200p $2.4

   yielding the option's type (call/put), strike, expiration date and the
   premium paid (long) or collected (short).

2. Resolving an option that has reached expiration. The underlying's closing
   spot price on the expiration date (fetched with yfinance) decides the
   outcome. The four single-leg cases:

                 spot >= strike (call ITM / put OTM)   spot < strike (call OTM / put ITM)
     Long call   exercised: worth spot-strike           expired worthless -> loss of premium
     Short call  assigned: shares called away @ strike   expired worthless -> WIN (keep premium)
     Long put    expired worthless -> loss of premium    exercised: worth strike-spot
     Short put   expired worthless -> WIN (keep premium) assigned: buy shares @ strike

   In this channel puts are premium/theta trades regardless of the Long/Short
   label a trader types (a put is written to collect premium, not bought for
   downside): a put that finishes at or above its strike expires worthless for
   a full win (premium kept); below the strike it assigns shares at a cost
   basis of the strike minus the premium. Only calls keep buyer/seller
   direction (a long call is a bought bet, a short call is covered/sold).

yfinance is imported lazily inside spot_close_on(), so parsing and the pure
resolution logic work -- and stay unit-testable -- even where yfinance or
network access is unavailable. Any fetch failure returns None and the caller
leaves the position open/pending rather than crashing.
"""

import re
from datetime import date, timedelta

CALL = "call"
PUT = "put"

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# An expiration date, ISO (2026-07-18) or US (7/18, 7/18/26, 7/18/2026).
_DATE = r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?"

# Filler words traders drop between the action and the ticker, between the
# ticker and a date-first option's expiration, or between a strike-first
# option's strike and its expiration, e.g. "#add Long ALAB", "#Exit half of
# RIVN", "#Exit this add on Long ALAB", "#long NBIS sold 7/2/26 $200p $2.4",
# "#Short QQQ 704p lottos 6/26 3.45". Shared with the stock regex in
# weekly_summary. Backtracking recovers a ticker that collides with a filler
# word ("#Long ON 45.00" still parses).
FILLER = (r"(?:\s+(?i:half|this|that|the|of|my|some|more|adds?|rest|"
          r"long|short|on|sold|bought|lottos?))*")

# A single-leg option line. What distinguishes it from a plain-stock line is
# the presence of BOTH a call/put marker (a "call"/"put" word or a c/p strike
# suffix) AND an expiration date -- neither appears on a "#Long PENG 77.15".
# Long/Short opens a position; Add scales into one; Exit (optionally partial)
# closes one early, with the premium received where the opening premium would be.
OPTION_RE = re.compile(
    r"^#\s*"
    r"(?P<side>[Ll]ong|[Ss]hort|[Ee]xit|[Aa]dd)"           # open / scale-in / close
    r"(?:\s+(?P<partial>[Pp]artial))?"                     # optional 'partial' (Exit)
    + FILLER +
    r"\s+"
    r"(?:(?P<type_word>[Cc]alls?|[Pp]uts?)\s+)?"           # optional 'call'/'put' word
    r"\$?(?P<ticker>[A-Za-z][A-Za-z.]{0,6})\s+"            # TICKER
    r"\$?(?P<strike>\d+(?:\.\d+)?)(?P<type_suffix>[CcPp])?"  # strike (+ optional c/p)
    + FILLER +                                             # e.g. "lottos"
    r"\s+(?:exp(?:iry|iration|ires)?\.?\s*[:.]?\s*)?"      # optional 'exp' label
    r"(?P<exp>" + _DATE + r")"                             # expiration date
    r"(?:\s*(?:@|for|at|premium|prem|:)?\s*\$?"            # optional premium intro
    r"(?P<premium>\d+(?:\.\d+)?))?"                        # premium (optional)
    r"(?P<notes>.*)$"
)

# The date-first shape this channel also uses: TICKER, then the expiration,
# then STRIKE[C/P] (space before the suffix optional), then premium. No
# "call"/"put" word variant has been observed in this order -- a bare suffix
# is required to identify the option (real examples: "#Long BE 07/10/2026
# 235.00 P 4.9", "#Long DELL 07/17/2026 385.00p 3.35 this am"). Tried as a
# fallback only after OPTION_RE fails, so it cannot shadow the strike-first
# form.
OPTION_RE_DATE_FIRST = re.compile(
    r"^#\s*"
    r"(?P<side>[Ll]ong|[Ss]hort|[Ee]xit|[Aa]dd)"
    r"(?:\s+(?P<partial>[Pp]artial))?"
    + FILLER +
    r"\s+"
    r"\$?(?P<ticker>[A-Za-z][A-Za-z.]{0,6})"
    + FILLER +                                             # e.g. "sold"
    r"\s+(?:exp(?:iry|iration|ires)?\.?\s*[:.]?\s*)?"
    r"(?P<exp>" + _DATE + r")"
    r"\s+\$?(?P<strike>\d+(?:\.\d+)?)"
    r"\s*(?P<type_suffix>[CcPp])\b"                        # required: only ID
    r"(?:\s*(?:@|for|at|premium|prem|:)?\s*\$?"            # optional premium intro
    r"(?P<premium>\d+(?:\.\d+)?))?"                        # premium (optional)
    r"(?P<notes>.*)$"
)


def _parse_exp(token, ref_date=None):
    """Parse an expiration token into a ``date``.

    A US-format date without a year (``7/18``) infers its year from ``ref_date``
    -- the next occurrence on or after that reference -- so an expiry posted in
    December for ``1/17`` lands in the following year. A date that passed less
    than ~a month before the reference keeps the current year: a message posted
    days after expiry ("#Exit ... 1/16" on Jan 20) refers to the just-expired
    contract, not next year's, and must key to the same contract the position
    was opened under. ``ref_date`` defaults to today. Returns None if the token
    is not a valid date.
    """
    ref = ref_date or date.today()
    token = token.strip()

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", token)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", token)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y is None:
            try:
                cand = date(ref.year, mo, d)
            except ValueError:
                return None
            # Only roll to next year when the date is well past (>30 days):
            # a recently-passed date refers to the just-expired contract.
            if cand < ref and (ref - cand).days > 30:
                try:
                    cand = date(ref.year + 1, mo, d)
                except ValueError:
                    return None
            return cand
        yi = int(y)
        if yi < 100:
            yi += 2000
        try:
            return date(yi, mo, d)
        except ValueError:
            return None

    return None


def _option_from_match(m, ref_date):
    """Build the parsed-option dict from a successful OPTION_RE(_DATE_FIRST)
    match, or None if it lacks a call/put marker or a valid expiration."""
    groups = m.groupdict()
    type_word = groups.get("type_word")
    type_suffix = groups.get("type_suffix")
    if type_word:
        opt_type = PUT if type_word.lower().startswith("p") else CALL
    elif type_suffix:
        opt_type = PUT if type_suffix.lower() == "p" else CALL
    else:
        return None  # no call/put marker -> treat as a plain-stock line

    exp = _parse_exp(m.group("exp"), ref_date)
    if exp is None:
        return None

    premium = groups.get("premium")
    return {
        "side": m.group("side").capitalize(),   # Long / Short / Exit / Add
        "partial": bool(groups.get("partial")),  # only meaningful on Exit
        "opt_type": opt_type,                    # call / put
        "ticker": m.group("ticker").upper().strip("."),
        "strike": float(m.group("strike")),
        "expiration": exp.isoformat(),           # YYYY-MM-DD
        "premium": float(premium) if premium is not None else None,
        "notes": (m.group("notes") or "").strip(),
    }


def parse_option(line, ref_date=None):
    """Parse a single-leg option trade line into a dict, or None if the line is
    not a recognizable option.

    Tries the strike-first form (OPTION_RE) then, if that doesn't match, the
    date-first form this channel also uses (OPTION_RE_DATE_FIRST) -- see the
    module docstring for examples of each. ``ref_date`` is only used to infer
    the year of an expiration written without one; it defaults to today.
    """
    line = line.strip()
    for pattern in (OPTION_RE, OPTION_RE_DATE_FIRST):
        m = pattern.match(line)
        if m:
            parsed = _option_from_match(m, ref_date)
            if parsed is not None:
                return parsed
    return None


# ---------------------------------------------------------------------------
# Resolution (pure -- no network)
# ---------------------------------------------------------------------------
def is_itm(opt_type, strike, spot):
    """In-the-money test. At-the-money (spot == strike) counts as out-of-the-
    money: a strike-pinned option is assumed to expire worthless rather than be
    auto-exercised."""
    if opt_type == CALL:
        return spot > strike
    return spot < strike


def resolve_option(side, opt_type, strike, premium, spot):
    """Resolve a single-leg option held to expiration, given the underlying's
    closing spot price. Pure function -- no network.

    Returns a dict:
      status   -- 'expired_worthless', 'assigned', or 'exercised'
      itm      -- bool, finished in the money
      win      -- True/False for a realized win/loss, or None when the position
                  converts into shares (assignment) so its P&L is not yet fixed
      pnl      -- realized profit/loss per share where defined, else None
      pct      -- signed fractional return on the premium (e.g. a short option
                  expiring worthless is +1.0, a long one -1.0), else None
      basis    -- per-share price at which shares change hands (assignment /
                  exercise), else None
      summary  -- short human-readable description of the outcome
    """
    side = side.capitalize()
    opt_type = opt_type.lower()
    # Puts in this channel are premium/theta trades, not downside bets: a put
    # expiring worthless is a win (premium kept) and a put finishing in the
    # money assigns shares -- the short-put outcome -- whether it was typed
    # "#Long ... P" or "#Short ... P". Only calls keep long/short direction.
    if opt_type == PUT:
        side = "Short"
    itm = is_itm(opt_type, strike, spot)
    prem = premium  # may be None when the poster omitted it

    if side == "Long":
        if itm:  # long call above strike, or long put below strike -> has value
            intrinsic = (spot - strike) if opt_type == CALL else (strike - spot)
            basis = strike  # buy (call) / sell (put) shares at the strike
            if prem is None:
                pnl, pct, win = None, None, True
                summary = f"in the money — worth {_d(intrinsic)}/sh at expiry"
            else:
                pnl = intrinsic - prem
                pct = pnl / prem if prem > 0 else None
                win = pnl > 0
                summary = (f"in the money — worth {_d(intrinsic)}/sh vs "
                           f"{_d(prem)} paid ({_signed(pnl)}/sh)")
            return _outcome("exercised", itm, win, pnl, pct, basis, summary)
        # out of the money -> worthless, the premium paid is a total loss
        pnl = -prem if prem is not None else None
        pct = -1.0 if prem else None
        summary = (f"expired worthless — loss of {_d(prem)} premium"
                   if prem is not None else "expired worthless — premium lost")
        return _outcome("expired_worthless", itm, False, pnl, pct, None, summary)

    # side == "Short"
    if not itm:  # short call below strike, or short put above strike -> worthless
        pnl = prem if prem is not None else None
        pct = 1.0 if prem else None
        summary = (f"expired worthless — win, kept {_d(prem)} premium"
                   if prem is not None else "expired worthless — win")
        return _outcome("expired_worthless", itm, True, pnl, pct, None, summary)

    # in the money short option -> assigned
    if opt_type == PUT:  # cash-secured put: buy shares at the strike
        basis = (strike - prem) if prem is not None else strike
        if prem is not None:
            summary = (f"shares assigned at {_d(basis)} "
                       f"(strike {_d(strike)} − {_d(prem)} premium)")
        else:
            summary = f"shares assigned at strike {_d(strike)}"
        return _outcome("assigned", itm, None, None, None, basis, summary)

    # short call: shares called away / sold at the strike
    basis = (strike + prem) if prem is not None else strike
    if prem is not None:
        summary = (f"shares called away at {_d(strike)} "
                   f"(effective {_d(basis)}/sh incl. premium)")
    else:
        summary = f"shares called away at strike {_d(strike)}"
    return _outcome("assigned", itm, None, None, None, basis, summary)


def _outcome(status, itm, win, pnl, pct, basis, summary):
    return {
        "status": status,
        "itm": itm,
        "win": win,
        "pnl": pnl,
        "pct": pct,
        "basis": basis,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Spot price (network -- yfinance, isolated and failure-tolerant)
# ---------------------------------------------------------------------------
def spot_close_on(ticker, exp_date, today=None):
    """Return the underlying's closing price on ``exp_date`` (or the most recent
    trading day on or before it), or None if it cannot be determined.

    Returns None when the expiration is still in the future, or when yfinance /
    the network is unavailable or yields no data -- callers then leave the
    position open/pending instead of crashing. ``today`` is injectable for
    testing.
    """
    today = today or date.today()
    if exp_date > today:
        return None
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        start = (exp_date - timedelta(days=7)).isoformat()
        end = (exp_date + timedelta(days=1)).isoformat()
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if hist is None or getattr(hist, "empty", True) or "Close" not in hist:
            return None
        close = None  # last close on or before the expiration date
        for idx, value in hist["Close"].items():
            idx_date = idx.date() if hasattr(idx, "date") else idx
            if idx_date <= exp_date:
                close = float(value)
        return close
    except Exception:
        return None


def last_closes(tickers):
    """Return {ticker: latest close} for the given tickers in ONE batched
    yfinance download, or {} (possibly missing some tickers) on any failure.

    Used to mark open positions to market. Like spot_close_on, this is
    failure-tolerant by design: no yfinance, no network, or a partial result
    just means fewer (or no) mark-to-market annotations.
    """
    tickers = sorted(set(tickers))
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    try:
        data = yf.download(tickers, period="5d", interval="1d",
                           auto_adjust=False, progress=False,
                           group_by="ticker", threads=False)
        if data is None or getattr(data, "empty", True):
            return {}
        out = {}
        multi = getattr(data.columns, "nlevels", 1) > 1
        for tk in tickers:
            try:
                series = data[tk]["Close"] if multi else data["Close"]
                series = series.dropna()
                if len(series):
                    out[tk] = float(series.iloc[-1])
            except Exception:
                continue
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _d(x):
    """Format a dollar amount, e.g. 172.8 -> '$172.80'."""
    return "$?" if x is None else f"${x:,.2f}"


def _signed(x):
    """Signed dollar amount, e.g. 3.2 -> '+$3.20', -1.5 -> '-$1.50'."""
    if x is None:
        return "$?"
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _contract(t):
    """Compact contract label, e.g. 'SPY $400p exp 2026-07-18'."""
    suffix = "c" if t["opt_type"] == CALL else "p"
    strike = f"{t['strike']:g}"
    return f"{t['ticker']} ${strike}{suffix} exp {t['expiration']}"


def format_option_open(t, verb=None):
    """One line for an option position; ``verb`` overrides the leading word
    (e.g. 'Partial exit' instead of the trade's own side)."""
    prem = f" @ {_d(t['premium'])}" if t.get("premium") is not None else ""
    return f"{verb or t['side']} {t['opt_type']} **{_contract(t)}**{prem}"


def format_option_resolution(t, outcome, spot):
    """One line describing a resolved (expired) option and its outcome."""
    icon = {True: "✅", False: "❌"}.get(outcome["win"], "\U0001F4E5")
    return (f"- {icon} {t['side']} {t['opt_type']} **{_contract(t)}**: "
            f"{outcome['summary']} — spot {_d(spot)} at expiry")
