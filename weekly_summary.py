#!/usr/bin/env python3
"""Weekly Discord trade-summary bot.

Fetches trade posts from a YAGPDB-fed Discord channel via the REST API (v10,
plain HTTP -- no gateway), stitches the raw posts into whole *trade journeys*
(entry -> adds -> partial exits -> final exit), keeps a persistent running log
so long swings are never forgotten, and posts a one-line-per-trade weekly
review back to a target channel.

Each trade gets exactly one line, marked by outcome:
    checkmark = closed for a win, red = closed for a loss,
    orange    = still open (including "only partials taken"),
    white     = closed flat (scratch/breakeven),
    question  = closed but the result could not be determined.

The bot token is read from the DISCORD_BOT_TOKEN environment variable and is
never stored in code, the running log, or version control.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://discord.com/api/v10"
SOURCE_CHANNEL_ID = "1473806053975261452"   # where YAGPDB posts trades
TARGET_CHANNEL_ID = "1525965273306235051"   # where the summary is posted

HISTORY_DAYS = 90     # rolling history the bot keeps fetched (~3 months)
WEEK_DAYS = 7         # "this week" window for the per-trader breakdown
STALE_DAYS = 30       # an open position carried this long is flagged for review
OPEN_DETAIL_DAYS = 14  # for an outsized book, detail only what moved this recently
MAX_OPEN_DETAIL = 12   # a book bigger than this is summarised instead of listed
MAX_TICKERS = 6       # tickers listed before a carried book is summarised
EXPIRED_DAYS = 45     # an untouched position this old was closed and never posted
RETENTION_DAYS = 400  # prune log entries older than this (open positions kept)

LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.json")
CHUNK_LIMIT = 1900       # stay under Discord's 2000-char message limit

DISCORD_EPOCH = 1420070400000  # 2015-01-01T00:00:00Z in ms
USER_AGENT = (
    "trades-summary-bot/1.0 "
    "(+https://github.com/Isidore94/trades-summary)"
)

# Outcome markers used in the posted summary.
ICON_WIN = "✅"       # closed, profitable
ICON_LOSS = "🔴"      # closed, losing
ICON_OPEN = "🟠"      # still open, including "only partials taken"
ICON_FLAT = "⚪"      # closed at scratch / breakeven
ICON_UNKNOWN = "❔"   # closed, result neither stated nor computable
ICON_STALE = "⏳"     # open position carried past STALE_DAYS

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# YAGPDB posts look like:
#     <user> posted a trade:
#     #Long $PENG 77.15
#     #Long CUZ 28.55
# One message may contain several such blocks, and one block may carry several
# trade lines -- every "#" line belongs to the most recent header above it.
HEADER_RE = re.compile(r"^(?P<user>.+?)\s+posted a trade:\s*$")

# A trade line, e.g.
#   #Long $PENG 77.15
#   #Exit NVDA 11.70 for -32% on calls
#   #Exit CRWD at 187.60
#   #Exit partial NVDA $208.66 for over $13 profit per share. (Still have ...).
#   #Exit FTNT, DELL for a scratch.
#   #Exit Long ARM 351.56 - holding onto last 1/4 position
#   #Add Short TSLA 393.41 avg 394.82
TICKER = r"\$?[A-Z][A-Z.]{0,6}"
# A direction word can sit either side of the ticker ("#Exit long ARM 351.56",
# "#Exit COST short $925.10"); it describes the position being acted on, not
# the ticker or the price, so the pattern steps over it in both places.
DIRECTION = r"(?:\s+(?:[Ll]ong|[Ss]hort))?"
TRADE_RE = re.compile(
    r"^#\s*"
    r"(?P<side>[Ll]ong|[Ss]hort|[Ee]xit|[Aa]dd)"        # what happened
    # "#exit Trim long ERX 97.85" -- without "trim" here the ticker match lands
    # on the "T" of "Trim" and the real ticker is swallowed into the notes.
    r"(?:\s+(?P<partial>[Pp]artial|[Tt]rim(?:med)?))?"
    r"(?:\s+\d/\d(?:th)?)?"                             # "Trim 1/4 Long PENG"
    + DIRECTION +
    rf"\s+(?P<tickers>{TICKER}(?:\s*(?:,|&|and)\s*{TICKER})*)"
    + DIRECTION +
    # Optional price. "1/2", "7/17" and "200p" are a fraction, a date and a
    # strike -- none of them is a fill price, so they must not be captured.
    r"(?:\s+(?:[Aa]t\s+|@\s*)?\$?(?P<price>\d+(?:\.\d+)?|\.\d+)"
    r"(?![\d/])(?P<strike_suffix>[CcPp]\b)?)?"
    r"(?P<notes>.*)$"
)

TICKER_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b)\s*")

# A sweep that closes the day's trades in one line, named tickers surviving:
#   #Exit all DTs at moc except FUTU CRML and remainder of AMD (...)
#   Exited all DTs at MOC except META BE and remainder of AAPL CRML DAL
# Only the day's trades go: 55 tickers opened before one of these sweeps are
# still being posted about afterwards, so "all" means the day trades, never
# the whole book. The MOC/DT/except signal is required -- a bare "exit all"
# is too vague to close positions on.
BULK_EXIT_RE = re.compile(r"^#?\s*exit(?:ed)?\s+all\b(?P<rest>.*)$", re.IGNORECASE)
BULK_SIGNAL_RE = re.compile(
    r"\bmoc\b|\bdts?\b|\bday ?trades?\b|\bexcept\b", re.IGNORECASE
)
EXCEPT_RE = re.compile(r"\bexcept\b(?P<rest>[^(]*)", re.IGNORECASE)
TICKER_TOKEN_RE = re.compile(r"\b[A-Z]{1,6}\b")
# Scaling into a position is said out loud. A second entry on a ticker that
# does not say so is a new trade at a new strike -- opreme posts "June 105c
# @ 4.6" then ".29 lotto" weeks later -- so the earlier position is gone even
# though its exit was never posted. 317 of the 341 repeat entries in the log
# carry no add language.
ADD_RE = re.compile(
    r"\badd(?:ed|ing|s)?\b|\bavg\b|\baverage|\bscal(?:e|ing)\s*in\b", re.IGNORECASE
)

# A credit spread is sold, not bought, so its premium moves the opposite way
# to a plain long call or put. PDS/CDS are debit spreads and behave normally.
CREDIT_RE = re.compile(r"\b(?:pcs|ccs|credit)\b", re.IGNORECASE)

# Which contract a leg is written on. "#Long QCOM 170c" is a call, "#Long VRT
# 5p" a put.
CALL_RE = re.compile(r"\bcalls?\b|(?<![.\d])\d{1,5}\s*c\b", re.IGNORECASE)
PUT_RE = re.compile(r"\bputs?\b|(?<![.\d])\d{1,5}\s*p\b", re.IGNORECASE)


def instrument_of(text):
    """'call', 'put', or None when the text names both or neither."""
    call, put = bool(CALL_RE.search(text)), bool(PUT_RE.search(text))
    if call == put:
        return None          # a strangle names both; plain shares name neither
    return "call" if call else "put"


def bought_the_contract(side, instrument):
    """True when the trader is long premium, False when they sold it.

    The side is the directional bet and the contract says how it was taken.
    Bullish is a bought call or a sold put -- "#Long puts" is a theta play, so
    selling puts at 5 and buying them back at 2 is a three dollar win, not a
    loss. Bearish is a bought put or a sold call.
    """
    if instrument is None:
        return True          # nothing says otherwise; assume premium was paid
    return (side == "Long") == (instrument == "call")

# Uppercase words that turn up in these lines but are not tickers.
NOT_TICKERS = {"MOC", "DT", "DTS", "EOD", "AND", "OF", "THE", "ALL", "TP", "SL",
               "OPEN", "CLOSE", "PM", "AM", "ET", "EST"}


def parse_bulk_exit(line):
    """Parse an 'exit all ... except X Y' sweep, or None if the line is not one."""
    m = BULK_EXIT_RE.match(line.strip())
    if not m or not BULK_SIGNAL_RE.search(m.group("rest")):
        return None
    kept = EXCEPT_RE.search(m.group("rest"))
    excluded = set()
    if kept:
        excluded = {
            tok for tok in TICKER_TOKEN_RE.findall(kept.group("rest"))
            if tok not in NOT_TICKERS
        }
    return {
        "side": "ExitAll",
        "ticker": "*",
        "excluding": sorted(excluded),
        "partial": False,
        "price": None,
        "gain": None,
        "option": False,
        "notes": m.group("rest").strip(),
        "outcome": None,
        "firm": True,
        "result_text": "closed in a MOC sweep",
    }

# Free text that tells us the position was an options trade. On those lines the
# leading number is a strike, an expiry day or a contract count -- never a fill
# price -- so it must not be compared against the share price on the other side
# of the trade. The strike alternative demands at least two digits not preceded
# by a decimal point so "50c" (a strike) matches but ".50c" (fifty cents) does
# not.
OPTION_RE = re.compile(
    r"\b(?:calls|puts|contracts?|premium|pcs|pds|spread|strangle|straddle|"
    r"lottos?)\b"
    r"|\d\s*(?:call|put)\b"          # "675 PUT" -- singular only after a strike,
                                     # so "i put a TP on the low" stays prose
    r"|(?<![.\d])\d{2,5}[cp]\b"      # a strike: "700p", but not ".50c"
    r"|\b\d{2,5}/\d{2,5}\b",         # spread strikes: "680/670", "700p/712c"
    re.IGNORECASE,
)

# The cost of an option leg is the premium, posted as "@1.79", "@ .36" or
# "for 3.45". On an exit "for 3.45" is just as likely to be the gain as the
# fill, so the looser form is only trusted when a position is being opened.
# A price must be captured whole. "(\d*\.?\d+)" happily backtracks to the "5"
# of "50%", which is how a strangle closed for +50% came to read as a fill of 5.
PRICE = r"(\d+(?:\.\d+)?|\.\d+)\b(?!\d)"
NOT_A_FILL = r"(?!\s*(?:%|cents?\b|c\b|dollars?\b))"
PREMIUM_RE = re.compile(r"@\s*\$?" + PRICE)
# "#Long QCOM 170c 1.53" -- the strike takes the price slot, so the premium is
# the first thing left in the notes. Rejected when a "/" follows, which makes
# it a date: "#Long NFLX 5/01/26 93 C .73".
PREMIUM_LEADING_RE = re.compile(r"^\$?" + PRICE + r"(?![/%])")
# "#Long NFLX 5/01/26 93 C .73" -- expiry, then strike, then the premium per
# contract. The premium is whatever follows the strike's C or P.
PREMIUM_AFTER_STRIKE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*[CP]\b\s*\$?" + PRICE, re.IGNORECASE
)
# (2) "puts 4.05", "calls this am 8.25" -- for a position taken via contracts,
# the number after the instrument is the price it filled at.
INSTRUMENT_PRICE_RE = re.compile(
    r"^(?:puts?|calls?|shares?)\s+(?:[\w']+\s+){0,2}\$?" + PRICE + r"(?!\s*%)",
    re.IGNORECASE
)
AT_PRICE_RE = re.compile(r"\bat\s+\$?" + PRICE + NOT_A_FILL, re.IGNORECASE)
PREMIUM_FOR_RE = re.compile(r"\bfor\s+\$?" + PRICE + NOT_A_FILL)

# Traders state the result of a trade in plain English far more reliably than
# the posted numbers allow it to be computed, so the words win over arithmetic.
FLAT_RE = re.compile(r"scratch|breakeven|break even|b/e|\bflat\b", re.IGNORECASE)
# "#Exit HIMS 29.24 partial for .50c gain" -- the word "partial" is only
# matched by TRADE_RE when it directly follows "#Exit", so the notes are
# checked too. Without this a trim closes the position and every later exit on
# the same ticker is orphaned.
PARTIAL_RE = re.compile(
    r"\bpartial(?:ly)?\b|\btrim(?:med|ming)?\b|\bstill (?:have|holding|hold|in)\b"
    r"|\bleaving a runner\b|\b\d/\d(?:th)?\s+(?:out|left)\b"
    r"|\b(?:out|left)\s+\d/\d(?:th)?\b"
    r"|\bhalf\s+(?:out|left)\b",
    re.IGNORECASE,
)
# Everything that is not called a loss is treated as a win (see score_journey),
# which puts the whole weight of the record on this pattern. "stopped .45" is
# how a stop-out is written 47 times in the log and only "stopped out" used to
# be caught, so every one of them would have flipped to a win.
LOSS_RE = re.compile(
    r"\bloss(?:es)?\b|\blost\b|\bworthless\b"
    r"|took the l\b|\bthe L\b|\bfor L\b",   # "for L 32%"
    re.IGNORECASE,
)
# Being stopped does not say which way the trade went -- a trailing stop banks
# a gain -- so the word only decides when there is no price to compare. The
# amount is posted right after it: "stopped .45", "stopped out at 2.44",
# "stopped starter at 49.25".
STOP_RE = re.compile(r"\bstopp?ed\b|\bhit (?:my |the )?stops?\b", re.IGNORECASE)
STOP_PRICE_RE = re.compile(
    r"stopp?ed\s+(?:[\w']+\s+){0,2}(?:at\s+)?\$?" + PRICE + r"(?!\s*%)",
    re.IGNORECASE
)
# "-$168" is a loss; "at $28.69 - 56c gain" is a dash between two figures, so
# the minus has to be tight against the number to count.
NEG_MONEY_RE = re.compile(r"(?<![\d/.])-\$?\d")
WIN_RE = re.compile(
    r"\bprofits?\b|\bgains?\b|\bwin(?:s|ner|ning)?\b|\breturns?\b",
    re.IGNORECASE,
)
SIGNED_PCT_RE = re.compile(r"(?<![\d.])([-+])(\d+(?:\.\d+)?)\s*%")

# "for 2 dollars per contract", "3.80 per share": the number is what the trade
# *made*, not what it closed at -- pay 14 for a contract, take a dollar, and you
# exited at 15. The amount can sit in the notes, or be the only number on the
# line and so get captured as the price.
GAIN_IN_NOTES_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(dollars?|cents?|c)?\s*(?:\w+\s+)?(?:per|a)\s+"
    r"(?:contract|share)",
    re.IGNORECASE,
)
GAIN_IS_PRICE_RE = re.compile(
    r"^(dollars?|cents?)?\s*(?:\w+\s+)?(?:per|on)\s+(?:contract|share)s?\b",
    re.IGNORECASE,
)
# "exit QQQ strangle for 50%" -- a bare percentage the trade was taken *for*
# means it was taken in the green; losses in this channel are always said out
# loud, and LOSS_RE has already had its turn by the time this is checked.
FOR_PCT_RE = re.compile(r"\bfor\s+(?:an?\s+)?(\d+(?:\.\d+)?)\s*%")
# "puts .35 70%", "other half 61%" -- a percentage on its own says how much the
# trade made. Only reached when nothing on the line called it a loss.
BARE_PCT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")


def _classify_notes(notes):
    """Return ('win'|'loss'|'flat'|None, display_text, firm) read from free text.

    ``firm`` is False for a verdict that should give way to the prices -- being
    stopped out is the only one: the word says the trade ended, not whether it
    ended up or down.
    """
    if not notes:
        return None, "", True
    if FLAT_RE.search(notes):
        return "flat", "scratch", True
    if LOSS_RE.search(notes):
        return "loss", _trim_note(notes), True
    if WIN_RE.search(notes):
        return "win", _trim_note(notes), True
    if NEG_MONEY_RE.search(notes):
        return "loss", _trim_note(notes), True
    m = SIGNED_PCT_RE.search(notes)
    if m:
        pct = f"{m.group(1)}{m.group(2)}%"
        return ("loss" if m.group(1) == "-" else "win"), pct, True
    if STOP_RE.search(notes):
        # Checked before the bare-percentage rule: a figure on a stop line is
        # the size of the move, not proof it was a gain.
        return "loss", _trim_note(notes), False
    m = FOR_PCT_RE.search(notes) or BARE_PCT_RE.search(notes)
    if m:
        # A percentage with no loss word on the line is a gain -- losses in
        # this channel are always said out loud, and FLAT_RE and LOSS_RE have
        # both already had their turn above.
        return "win", f"+{m.group(1)}%", True
    return None, "", True


def _to_dollars(amount, unit):
    """Normalise a posted amount to dollars ('50 cents' -> 0.5)."""
    return amount / 100 if unit and unit.lower().startswith("c") else amount


def _extract_gain(price, notes):
    """Return (gain_per_unit, price) for an exit, in dollars.

    ``price`` comes back as None when the number turned out to be the gain
    rather than a fill. A gain stated in the notes is only trusted when no fill
    was posted -- "#Exit RDDT 205.61 for 4.00 profit per share" has both, and
    the fill is the better number.
    """
    m = GAIN_IS_PRICE_RE.match(notes)
    if m and price is not None:
        return _to_dollars(price, m.group(1)), None
    if price is None:
        m = GAIN_IN_NOTES_RE.search(notes)
        if m:
            return _to_dollars(float(m.group(1)), m.group(2)), None
    return None, price


def _trim_note(notes, limit=52):
    """Shorten a trader's free-text note to something one line can carry."""
    note = re.sub(r"\s+", " ", notes).strip(" .,-—/")
    note = re.sub(r"^(?:for|at)\s+(?:an?\s+)?", "", note, flags=re.IGNORECASE)
    if len(note) > limit:
        note = note[:limit].rstrip() + "…"
    return note


def parse_trade_lines(line):
    """Parse one '#Long/Short/Exit/Add ...' line into a list of trade dicts.

    A line may name several tickers ("#Exit FTNT, DELL for a scratch."), which
    yields one dict per ticker. Returns [] when the line is not a trade.
    """
    bulk = parse_bulk_exit(line)
    if bulk:
        return [bulk]
    m = TRADE_RE.match(line.strip())
    if not m:
        return []
    notes = (m.group("notes") or "").strip()
    price = float(m.group("price")) if m.group("price") else None
    side = m.group("side").capitalize()   # Long / Short / Exit / Add
    is_option = bool(m.group("strike_suffix")) or bool(OPTION_RE.search(notes))
    if is_option:
        # "#Long NVDA 200p July 17th for 17.10", "#Short QQQ 100 21 AUG 26
        # 680/675 PUT @1.79": the captured number is a strike or a contract
        # count. The premium is what the trade actually cost.
        premium = (PREMIUM_RE.search(notes) or PREMIUM_LEADING_RE.match(notes)
                   or PREMIUM_AFTER_STRIKE_RE.search(notes))
        if premium is None and side != "Exit":
            premium = PREMIUM_FOR_RE.search(notes)
        if premium is not None:
            price = float(premium.group(1))
        elif m.group("strike_suffix"):
            price = None      # proven a strike by its C/P, and no premium given
        # Otherwise the captured number is kept: "#Long VRT 2.25 calls" prices
        # the contract at 2.25, and only the word "calls" made it look otherwise.
    tickers = [
        t.lstrip("$").upper().rstrip(".")
        for t in TICKER_SPLIT_RE.split(m.group("tickers"))
        if t.strip()
    ]
    gain = candidate = None
    outcome, result_text, firm = None, "", True
    if side == "Exit":
        gain, price = _extract_gain(price, notes)
        if price is None and gain is None:
            # "#Exit XLP for $1.85" -- no fill was captured, but the trader
            # named a number. Scoring accepts it only if it sits close enough
            # to the entry to be the same instrument.
            m2 = (PREMIUM_RE.search(notes) or PREMIUM_FOR_RE.search(notes)
                  or STOP_PRICE_RE.search(notes)
                  or INSTRUMENT_PRICE_RE.match(notes) or AT_PRICE_RE.search(notes))
            candidate = float(m2.group(1)) if m2 else None
        outcome, result_text, firm = _classify_notes(notes)
        if outcome is None and gain is not None:
            # Taking an amount off a position means it was taken in the green;
            # a loss is always said out loud and caught above.
            outcome, result_text = "win", _trim_note(notes)
    return [
        {
            "side": side,
            "partial": bool(m.group("partial"))
            or (side == "Exit" and bool(PARTIAL_RE.search(notes))),
            "ticker": ticker,
            "price": price,
            "candidate_price": candidate,
            "gain": gain,
            "option": is_option,
            "notes": notes,
            "outcome": outcome,
            "firm": firm,
            "result_text": result_text,
        }
        for ticker in tickers
    ]


def parse_trade_line(line):
    """Parse a single-ticker trade line into one dict, or None if no match."""
    trades = parse_trade_lines(line)
    return trades[0] if trades else None


def parse_message(content):
    """Return a list of trade dicts parsed from one message's raw content.

    Every "#" line is attributed to the nearest "<user> posted a trade:" header
    above it, so a header followed by several trade lines yields several dicts
    (the previous parser kept only the first and silently dropped the rest).
    """
    trades = []
    user = None
    for raw_line in (content or "").split("\n"):
        line = raw_line.strip()
        header = HEADER_RE.match(line)
        if header:
            user = header.group("user").strip()
            continue
        if not user:
            continue
        # Sweeps are sometimes posted without the "#" ("Exited all DTs at MOC
        # except META BE ..."), so they are matched on their own before the
        # "#" requirement that every other trade line has to meet.
        if not line.startswith("#") and not parse_bulk_exit(line):
            continue
        for trade in parse_trade_lines(line):
            trade["user"] = user
            trades.append(trade)
    return trades


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------
def snowflake_for(dt):
    """Return a Discord snowflake id representing the given datetime."""
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH) << 22


def _headers(token):
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def _request(method, url, token, **kwargs):
    """HTTP request with basic 429 rate-limit handling."""
    for _ in range(6):
        resp = requests.request(
            method, url, headers=_headers(token), timeout=30, **kwargs
        )
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(float(retry_after) + 0.5)
            continue
        if resp.status_code == 401:
            raise SystemExit(
                "Discord returned 401 Unauthorized -- the DISCORD_BOT_TOKEN "
                "secret is being rejected. Check that its value is the Bot "
                "token from the Developer Portal (NOT the Application ID, "
                "Public Key, or OAuth client secret), has no stray spaces or "
                "newlines, and was not regenerated after you saved the secret."
            )
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def fetch_messages(token, channel_id, after_snowflake, max_pages=400):
    """Fetch messages newer than after_snowflake, oldest-first, 100 per page."""
    messages = []
    after = str(after_snowflake)
    for _ in range(max_pages):
        url = (
            f"{API_BASE}/channels/{channel_id}/messages"
            f"?limit=100&after={after}"
        )
        page = _request("GET", url, token).json()
        if not page:
            break
        messages.extend(page)
        # Pages come back newest-first; advance past the newest id we've seen.
        after = str(max(int(m["id"]) for m in page))
        if len(page) < 100:
            break
        time.sleep(0.3)
    return messages


def post_message(token, channel_id, content):
    """Post a single message to a channel."""
    url = f"{API_BASE}/channels/{channel_id}/messages"
    _request("POST", url, token, data=json.dumps({"content": content}))
    time.sleep(0.6)


# ---------------------------------------------------------------------------
# Running log
# ---------------------------------------------------------------------------
def load_log(path=LOG_PATH):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"messages": {}}


def save_log(log, path=LOG_PATH):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=2, sort_keys=True)
        fh.write("\n")


def merge_messages(log, raw_messages):
    """Merge freshly fetched Discord messages into the running log (dedup by id).

    The raw message ``content`` is stored (not the parsed result) so parser
    improvements apply retroactively when the log is re-read on the next run.
    """
    store = log.setdefault("messages", {})
    added = 0
    for msg in raw_messages:
        content = msg.get("content", "")
        if not parse_message(content):
            continue  # skip messages that contain no trades
        mid = str(msg["id"])
        if mid not in store:
            added += 1
        store[mid] = {"timestamp": msg["timestamp"], "content": content}
    return added


def parse_ts(value):
    """Parse a Discord ISO-8601 timestamp into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def history_start(now):
    """Oldest moment the bot wants covered by the running log."""
    return now - timedelta(days=HISTORY_DAYS)


def needs_backfill(log, now):
    """True when the log does not yet reach back HISTORY_DAYS.

    ``covered_since`` records how far back the log has been filled. Without it
    the bot only ever fetched forward from the newest logged message, so trades
    opened before the very first run stayed invisible for good -- which is why
    exits kept showing up with no entry to pair them against.
    """
    if not log.get("messages"):
        return True
    covered = log.get("covered_since")
    if not covered:
        return True
    return parse_ts(covered) > history_start(now)


def fetch_after(log, now):
    """Snowflake to fetch messages after.

    Backfill runs (first run, or a log that does not reach back HISTORY_DAYS)
    start at the history horizon; otherwise resume from the newest message
    already logged so the weekly run only pulls what is new.
    """
    if needs_backfill(log, now):
        return snowflake_for(history_start(now))
    return max(int(mid) for mid in log["messages"])


def log_to_trades(log):
    """Flatten the log into a chronologically sorted list of trade dicts.

    Entries store raw ``content`` and are re-parsed each run; older entries
    that stored a pre-parsed ``trades`` list are still supported.
    """
    trades = []
    for mid, entry in log.get("messages", {}).items():
        parsed = parse_message(entry["content"]) if "content" in entry \
            else entry.get("trades", [])
        for i, tr in enumerate(parsed):
            t = dict(tr)
            t.setdefault("option", False)
            t.setdefault("gain", None)
            t.setdefault("candidate_price", None)
            t.setdefault("firm", True)
            t.setdefault("excluding", [])
            t.setdefault("outcome", None)
            t.setdefault("result_text", "")
            t["message_id"] = mid
            t["timestamp"] = entry.get("timestamp")
            t["index"] = i
            trades.append(t)
    trades.sort(key=lambda t: (int(t["message_id"]), t["index"]))
    return trades


# ---------------------------------------------------------------------------
# Closing prices
# ---------------------------------------------------------------------------
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"


def yahoo_last(ticker):
    """Most recent close for a ticker, or None."""
    url = YAHOO_CHART.format(ticker=ticker) + "?range=5d&interval=1d"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        closes = [c for c in resp.json()["chart"]["result"][0]
                  ["indicators"]["quote"][0]["close"] if c is not None]
        return round(float(closes[-1]), 4) if closes else None
    except Exception as exc:
        print(f"  no mark for {ticker}: {type(exc).__name__}")
    return None


def yahoo_close(ticker, day):
    """Closing price for a ticker on a trading day, or None if unavailable.

    Only used for "at MOC", where the close *is* the fill. Any other exit
    happened at a time the post does not give, and a day's close would be a
    different number dressed up as the trader's.
    """
    try:
        start = datetime.fromisoformat(day + "T00:00:00+00:00")
    except ValueError:
        return None
    params = {
        "period1": int(start.timestamp()) - 86400,
        "period2": int(start.timestamp()) + 3 * 86400,
        "interval": "1d",
    }
    url = YAHOO_CHART.format(ticker=ticker) + "?" + urlencode(params)
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        for stamp, close in zip(result["timestamp"], closes):
            at = datetime.fromtimestamp(stamp, tz=timezone.utc)
            if at.strftime("%Y-%m-%d") == day and close is not None:
                return round(float(close), 4)
    except Exception as exc:                      # network, ticker, or shape
        print(f"  no close for {ticker} on {day}: {type(exc).__name__}")
    return None


def mark_lookups(journeys):
    """Tickers whose latest close would mark an open share position.

    Options are left out: free data covers the underlying, not the premium a
    contract is actually worth, and the underlying's move is not the trade's.
    """
    return sorted({
        j["ticker"] for j in journeys
        if not j["closed"] and not j.get("unreadable")
        and not j["option"] and j["entry_price"] and j["side"]
    })


def apply_marks(journeys, marks):
    """Attach an unrealised move to each open share position we have a mark for."""
    for j in journeys:
        j["mark"] = None
        if j["closed"] or j["option"] or not j["entry_price"] or not j["side"]:
            continue
        last = marks.get(j["ticker"])
        if last is None or not _comparable(j["entry_price"], last):
            continue
        move = (last - j["entry_price"]) / j["entry_price"]
        j["mark"] = {
            "price": last,
            "pct": 100 * (move if j["side"] == "Long" else -move),
        }


def sweep_lookups(journeys):
    """(ticker, day) pairs whose MOC close would let a sweep be scored."""
    wanted = []
    for j in journeys:
        if not j["swept"] or j["option"] or not j["closed_at"]:
            continue
        if j["exits"] and j["exits"][-1]["price"] is None and j["entry_price"]:
            wanted.append((j["ticker"], j["closed_at"][:10]))
    return sorted(set(wanted))


def apply_sweep_prices(journeys, prices):
    """Give swept share positions their closing price and score them."""
    for j in journeys:
        if not j["swept"] or j["option"] or not j["closed_at"]:
            continue
        close = prices.get(f"{j['ticker']}|{j['closed_at'][:10]}")
        if close is None or not j["exits"] or j["exits"][-1]["price"] is not None:
            continue
        j["exits"][-1]["price"] = close
        j["exits"][-1]["result_text"] = "closed at MOC"
        j["result"] = score_journey(j)


# ---------------------------------------------------------------------------
# Trade journeys
# ---------------------------------------------------------------------------
def _new_journey(t):
    return {
        "user": t["user"],
        "ticker": t["ticker"],
        "side": t["side"] if t["side"] in ("Long", "Short") else None,
        "entry_price": t["price"] if t["side"] in ("Long", "Short") else None,
        "opened": t["timestamp"],
        "option": t["option"],
        "entry_notes": t["notes"],
        "instrument": instrument_of(t["notes"]),
        "credit": bool(CREDIT_RE.search(t["notes"])),
        "swept": False,
        "superseded": False,
        "adds": 0,
        "exits": [],
        "closed": False,
        "closed_at": None,
        "last_touch": t["timestamp"],
        "message_ids": [t["message_id"]],
    }


def _apply_sweep(open_journeys, t):
    """Close the poster's day trades on an 'exit all at MOC' line.

    Only positions opened the same calendar day are swept -- those are the day
    trades the line is talking about -- and any ticker named after "except"
    survives. No result is stated per position, so each one closes unscored
    rather than being guessed at.
    """
    day = (t["timestamp"] or "")[:10]
    for key, journey in list(open_journeys.items()):
        user, ticker = key
        if user != t["user"] or ticker in t["excluding"]:
            continue
        if not journey["opened"] or journey["opened"][:10] != day:
            continue
        journey["exits"].append(dict(t, ticker=ticker))
        journey["swept"] = True
        journey["closed"] = True
        journey["closed_at"] = t["timestamp"]
        journey["last_touch"] = t["timestamp"]
        journey["message_ids"].append(t["message_id"])
        open_journeys.pop(key)


def build_journeys(trades):
    """Stitch chronological trade events into one journey per round trip.

    A Long/Short opens a position; further same-side entries and #Add lines
    scale into it; partial exits leave it open; a full exit closes it. An exit
    with no logged entry still gets a journey of its own so nothing a trader
    posted disappears from the review -- it is simply shown without an entry.
    """
    open_journeys = {}  # (user, ticker) -> journey
    journeys = []
    for t in trades:
        if t["side"] == "ExitAll":
            _apply_sweep(open_journeys, t)
            continue
        key = (t["user"], t["ticker"])
        current = open_journeys.get(key)
        if t["side"] in ("Long", "Short"):
            if current and current["side"] == t["side"]:
                if ADD_RE.search(t["notes"]):
                    current["adds"] += 1      # scaling into the same position
                    current["message_ids"].append(t["message_id"])
                    current["option"] = current["option"] or t["option"]
                    current["last_touch"] = t["timestamp"]
                    continue
                # Neither an add nor a trim, so the old position is closed and
                # this is a fresh one. No result was ever posted for it.
                current["closed"] = True
                current["closed_at"] = t["timestamp"]
                current["superseded"] = True
                open_journeys.pop(key, None)
            if current:                        # flipped direction: close the old
                current["closed"] = True
                current["closed_at"] = t["timestamp"]
            journey = _new_journey(t)
            journeys.append(journey)
            open_journeys[key] = journey
            continue
        if t["side"] == "Add":
            if not current:                    # an add to a position we never saw
                current = _new_journey(t)
                current["entry_price"] = t["price"]
                journeys.append(current)
                open_journeys[key] = current
            else:
                current["adds"] += 1
                current["message_ids"].append(t["message_id"])
                current["last_touch"] = t["timestamp"]
            continue
        # Exit
        if not current:
            current = _new_journey(t)
            current["opened"] = None
            journeys.append(current)
            open_journeys[key] = current
        current["message_ids"].append(t["message_id"])
        current["option"] = current["option"] or t["option"]
        current["credit"] = current["credit"] or bool(CREDIT_RE.search(t["notes"]))
        if current["instrument"] is None:
            current["instrument"] = instrument_of(t["notes"])
        current["last_touch"] = t["timestamp"]
        current["exits"].append(t)
        if not t["partial"]:
            current["closed"] = True
            current["closed_at"] = t["timestamp"]
            open_journeys.pop(key, None)
    for journey in journeys:
        journey["result"] = score_journey(journey)
    return journeys


def _comparable(entry_price, exit_price):
    """True when two posted numbers are plausibly the same instrument.

    Traders post share prices, option premiums and strikes in the same field,
    so an entry of 200 against an exit of 11.70 is a strike-vs-premium pair,
    not a 94% loss. Anything outside a 3x band is treated as unscoreable.
    """
    if not entry_price or not exit_price:
        return False
    ratio = exit_price / entry_price
    return 0.34 <= ratio <= 3.0


def score_journey(journey):
    """Return {'outcome', 'pct', 'text', 'exit_price', 'implied'} for a journey.

    Outcome is 'open' while the position is live, otherwise it is taken from
    what the trader wrote on the exit ('for a loss', '+53%', 'for a scratch'),
    falling back to entry-vs-exit arithmetic when both numbers are comparable,
    and 'unknown' when neither is available.
    """
    if not journey["closed"]:
        return {"outcome": "open", "pct": None, "text": "",
                "exit_price": None, "implied": False, "conflict": False}
    if journey["superseded"] and not journey["exits"]:
        # Closed only because a later entry replaced it; the trader never said
        # how it went, so nothing is claimed either way.
        return {"outcome": "unknown", "pct": None,
                "text": "replaced by a later entry",
                "exit_price": None, "implied": False, "conflict": False}

    # The trader's last word on the position decides, so ordering is kept:
    # a partial taken for profit does not outrank the stop that ended it.
    verdicts = [e for e in journey["exits"] if e["outcome"]]
    last = verdicts[-1] if verdicts else None
    text = next(
        (e["result_text"] for e in reversed(journey["exits"]) if e["result_text"]),
        "",
    )
    if not text and journey["exits"]:
        # Nothing scoreable was written, so carry the trader's own words through
        # rather than posting a line that says nothing at all.
        text = _trim_note(journey["exits"][-1]["notes"])

    final = journey["exits"][-1] if journey["exits"] else None
    entry = journey["entry_price"]
    pct, exit_price = None, None
    posted = final["price"] if final is not None else None
    if posted is None and final is not None:
        posted = final.get("candidate_price")
    if final is not None and _comparable(entry, posted):
        # Both sides are plausibly the same instrument, so the move is real.
        exit_price = posted
        move = (exit_price - entry) / entry
        if journey["option"]:
            # Premium up wins when the contract was bought and loses when it
            # was sold. Credit spreads are left alone: telling which way one
            # runs from an abbreviation is not something to guess at.
            if not journey["credit"]:
                paid = bought_the_contract(journey["side"], journey["instrument"])
                pct = 100 * (move if paid else -move)
        elif journey["side"]:
            pct = 100 * (move if journey["side"] == "Long" else -move)

    if last is not None and last.get("firm", True):
        outcome = last["outcome"]
    elif pct is not None:
        outcome = "flat" if abs(pct) < 0.05 else ("win" if pct > 0 else "loss")
    elif last is not None:
        # Stopped out with no price to check it against: the word is all there
        # is, and a stop usually means the trade went the wrong way.
        outcome = last["outcome"]
    else:
        # No verdict and no price. Nothing is claimed either way; the post is
        # counted as unreadable instead of being scored on a guess.
        return {"outcome": "unknown", "pct": None, "text": text,
                "exit_price": exit_price, "implied": False, "conflict": False}

    implied = False
    if pct is None and entry and final is not None and final.get("gain") is not None:
        # No fill was posted, but the trader said what the trade made: pay 14
        # for a contract, take a dollar, and the exit was 15. Options are
        # bought, so their premium always moves up on a winner; short stock is
        # covered lower.
        signed = -final["gain"] if outcome == "loss" else final["gain"]
        candidate = 100 * signed / entry
        if journey["option"]:
            away = bought_the_contract(journey["side"], journey["instrument"])
        else:
            away = journey["side"] != "Short"
        price = round(entry + signed if away else entry - signed, 4)
        if price > 0 and abs(candidate) <= 300:
            pct, exit_price, implied = candidate, price, True
        # Otherwise the two numbers are different units -- a $25/share gain
        # against a $0.25 option premium is not a -10000% trade -- so the
        # outcome stands on the trader's words and no price is invented.

    # If the trader's own verdict and the arithmetic disagree, there is no way
    # to tell which is right: "puts .8 64%" is +64% on a sold put and -64% on a
    # bought one, and "starter swing at 2.20 stopped out" names the entry where
    # a fill is expected. The post is marked unreadable rather than resolved on
    # a coin flip.
    conflict = (pct is not None and outcome != "flat"
                and (pct > 0) != (outcome == "win"))
    if conflict:
        pct, exit_price, implied = None, None, False

    # A computed percentage beats the trader's prose ("for profit", "the rest"),
    # but a move that rounds to nothing is better described as a scratch.
    if pct is not None and abs(pct) >= 0.05:
        text = f"{pct:+.1f}%"
    elif not text and outcome == "flat":
        text = "scratch"
    return {"outcome": outcome, "pct": pct, "text": text,
            "exit_price": exit_price, "implied": implied, "conflict": conflict}


def mark_unreadable(journeys, now):
    """Flag journeys that cannot be scored, with the reason, and return them.

    These are excluded from every record rather than guessed at. Each one is a
    post whose format needs fixing, so the summary reports the count and names
    the tickers and dates to go back and check.
    """
    cutoff = now - timedelta(days=EXPIRED_DAYS)
    unreadable = []
    for j in journeys:
        reason = None
        if j["superseded"] and not j["exits"]:
            reason = "replaced by a later entry, no result posted"
        elif not j["closed"] and j["last_touch"] and parse_ts(j["last_touch"]) < cutoff:
            # Untouched for longer than any of these positions run. An option
            # this old has expired; shares this old were sold without a post.
            reason = f"no exit posted in {EXPIRED_DAYS}+ days"
        elif j["closed"] and j["result"].get("conflict"):
            reason = "stated result disagrees with the posted prices"
        elif j["closed"] and j["result"]["outcome"] == "unknown":
            reason = ("closed in a MOC sweep, no result per ticker"
                      if j["swept"] else "exit posted without a price or result")
        j["unreadable"] = reason
        if reason:
            unreadable.append(j)
    return unreadable


def compute_holdings(journeys):
    """Return {user: [open journeys]}, oldest first."""
    holdings = {}
    for j in journeys:
        if not j["closed"] and not j.get("unreadable"):
            holdings.setdefault(j["user"], []).append(j)
    for items in holdings.values():
        items.sort(key=lambda j: (j["opened"] or "", j["ticker"]))
    return holdings


def tally(journeys):
    """Count outcomes across closed journeys."""
    counts = {"win": 0, "loss": 0, "flat": 0, "unknown": 0}
    for j in journeys:
        if j.get("unreadable"):
            continue          # not a result, a post that could not be read
        outcome = j["result"]["outcome"]
        if outcome in counts:
            counts[outcome] += 1
    return counts


def prune_log(log, journeys, now):
    """Drop messages older than RETENTION_DAYS, but always keep those that
    belong to a position that is still open (so long swings survive)."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    keep_ids = {
        mid for j in journeys if not j["closed"] for mid in j["message_ids"]
    }
    store = log.get("messages", {})
    for mid in list(store.keys()):
        if mid in keep_ids:
            continue
        if parse_ts(store[mid]["timestamp"]) < cutoff:
            del store[mid]


# ---------------------------------------------------------------------------
# Summary building
# ---------------------------------------------------------------------------
def _fmt_date(dt):
    return f"{dt.strftime('%b')} {dt.day}"


def _fmt_price(price):
    return f"{price:g}" if price is not None else ""


ICONS = {
    "win": ICON_WIN,
    "loss": ICON_LOSS,
    "flat": ICON_FLAT,
    "unknown": ICON_UNKNOWN,
    "open": ICON_OPEN,
}


def _plural(count, word):
    return f"{count} {word}{'s' if count > 1 else ''}"


def _age(opened, now):
    """'19d', or '34d ⏳' once a position has been carried past STALE_DAYS."""
    days = max((now - opened).days, 0)
    return f"{days}d {ICON_STALE}" if days >= STALE_DAYS else f"{days}d"


def _open_lines(open_trades, now):
    """Every carried position, with what it cost and where it stands.

    A book past MAX_OPEN_DETAIL is not a list anyone reads, so only what has
    moved lately is spelled out and the remainder is summarised. Every other
    trader sees all of theirs.
    """
    if len(open_trades) <= MAX_OPEN_DETAIL:
        return [_journey_line(j, now) for j in open_trades]
    cutoff = now - timedelta(days=OPEN_DETAIL_DAYS)
    moved = [j for j in open_trades
             if j["last_touch"] and parse_ts(j["last_touch"]) >= cutoff]
    parked = [j for j in open_trades if j not in moved]
    lines = [_journey_line(j, now) for j in moved]
    if parked:
        lines.append(f"_+{len(parked)} carried: {_book_line(parked, now)}_")
    return lines


def _book_line(journeys, now):
    """One line standing in for positions nobody has touched lately.

    Ninety days of history turns an unbounded "Still open" list into a wall --
    207 positions across the group, 192 untouched for a fortnight. The detail
    belongs to trades that actually moved; the rest is a carried book, and a
    book is a sentence, not a section. Oldest first, so the ones most likely to
    need closing out are the ones that get named.
    """
    ordered = sorted(journeys, key=lambda j: (j["opened"] or "", j["ticker"]))
    names = ", ".join(
        f"{j['ticker']} ({_age(parse_ts(j['opened']), now)})" if j["opened"]
        else f"{j['ticker']} (entry not logged)"
        for j in ordered[:MAX_TICKERS]
    )
    if len(ordered) > MAX_TICKERS:
        names += f", +{len(ordered) - MAX_TICKERS} more"
    trimmed = sum(1 for j in ordered if j["exits"])
    return names + (f" · {trimmed} trimmed" if trimmed else "")


def _open_summary(open_trades):
    """'5 open, 3 trimmed' -- how much of a record is still unresolved.

    A position that was trimmed but never fully exited stays open forever and
    so never scores. A trader who only closes their winners therefore shows a
    win rate with nothing to drag it down; the trimmed count is what makes that
    visible instead of silent.
    """
    if not open_trades:
        return None
    trimmed = sum(1 for j in open_trades if j["exits"])
    text = f"{len(open_trades)} open"
    return f"{text}, {trimmed} trimmed" if trimmed else text


def _journey_line(j, now):
    """The whole life of one trade -- entry, adds, partials, exit -- on one line.

    Anything that was never posted is left out rather than rendered as a
    placeholder, so a sparse trade still reads as a sentence.
    """
    head = [ICONS[j["result"]["outcome"]], f"**{j['ticker']}**"]
    if j["side"]:
        head.append(j["side"])
    if j["option"]:
        head.append("opts")

    # score_journey already discarded numbers that are not fills -- an exit of
    # 3.80 against an entry of 404.25 is a gain per share, and printing
    # "404.25 → 3.8" would read as a catastrophic loss. A "~" marks an exit
    # derived from a stated gain rather than one the trader posted.
    entry = _fmt_price(j["entry_price"])
    exit_price = _fmt_price(j["result"]["exit_price"])
    if exit_price and j["result"]["implied"]:
        exit_price = "~" + exit_price
    if entry and exit_price:
        head.append(f"{entry} → {exit_price}")
    elif exit_price:
        head.append(f"→ {exit_price}")
    elif entry:
        head.append(entry)

    detail = []
    if j.get("mark"):
        detail.append(f"now {_fmt_price(j['mark']['price'])} "
                      f"({j['mark']['pct']:+.1f}%)")
    if j["adds"]:
        detail.append(_plural(j["adds"], "add"))
    partials = sum(1 for e in j["exits"] if e["partial"])
    if partials and not j["closed"]:
        detail.append(_plural(partials, "partial") + " taken")
    elif partials:
        detail.append(_plural(partials, "partial"))
    if j["result"]["text"]:
        detail.append(j["result"]["text"])
    detail.append(_span(j, now))

    return "- " + " · ".join([" ".join(head)] + [d for d in detail if d])


def _span(j, now):
    """Date span for a journey: 'Jul 10→12' or 'open since Jul 8 (19d)'."""
    opened = parse_ts(j["opened"]) if j["opened"] else None
    closed = parse_ts(j["closed_at"]) if j["closed_at"] else None
    if j["closed"]:
        if opened and closed:
            if opened.date() == closed.date():
                return _fmt_date(closed)
            if opened.month == closed.month:
                return f"{_fmt_date(opened)}→{closed.day}"
            return f"{_fmt_date(opened)}→{_fmt_date(closed)}"
        if closed:
            return f"exited {_fmt_date(closed)}, entry not logged"
        return ""
    if opened:
        return f"open since {_fmt_date(opened)} ({_age(opened, now)})"
    if j["exits"]:
        last = parse_ts(j["exits"][-1]["timestamp"])
        return f"trimmed {_fmt_date(last)}, entry not logged"
    return "open, entry not logged"


def _record(counts, label, verbose=False):
    """'This week: 3W–1L–1S (75%)', or None when there is nothing to report.

    Wins, losses and scratches each get a column; the percentage is wins over
    wins-plus-losses, so scratches are reported without dragging the win rate
    down. Posts that could not be read are not in here at all -- they are
    counted on their own, so a record only ever reflects trades that said how
    they ended.
    """
    wins, losses, scratches = counts["win"], counts["loss"], counts["flat"]
    scored = wins + losses
    if not (scored or scratches):
        return None
    text = f"{label} {wins}W–{losses}L–{scratches}S"
    if scored:
        text += f" ({round(100 * wins / scored)}%)"
    return text


def build_summary(log, now):
    """Build a trader-by-trader weekly review (Markdown) from the running log.

    One line per trade journey, grouped into trades closed this week and
    positions still carried, plus this-week and 90-day records per trader.
    """
    journeys = build_journeys(log_to_trades(log))
    apply_sweep_prices(journeys, log.get("prices", {}))
    unreadable = mark_unreadable(journeys, now)
    apply_marks(journeys, log.get("marks", {}))
    holdings = compute_holdings(journeys)

    week_start = now - timedelta(days=WEEK_DAYS)
    horizon = history_start(now)

    closed_week = {}
    closed_history = {}
    opened_week = set()
    for j in journeys:
        if j["opened"] and parse_ts(j["opened"]) >= week_start:
            opened_week.add(j["user"])
        if not j["closed"] or not j["closed_at"] or j["unreadable"]:
            continue
        closed_at = parse_ts(j["closed_at"])
        if closed_at >= horizon:
            closed_history.setdefault(j["user"], []).append(j)
        if closed_at >= week_start:
            closed_week.setdefault(j["user"], []).append(j)
    for items in closed_week.values():
        items.sort(key=lambda j: j["closed_at"])

    if week_start.month == now.month:
        label = f"{_fmt_date(week_start)}–{now.day}, {now.year}"
    else:
        label = f"{_fmt_date(week_start)}–{_fmt_date(now)}, {now.year}"

    lines = [
        f"# \U0001F4CA Weekly Trader Review — {label}",
        f"_{ICON_WIN} win · {ICON_LOSS} loss · {ICON_OPEN} open · "
        f"{ICON_FLAT} scratch · {ICON_STALE} carried {STALE_DAYS}d+ — "
        f"one line per trade, entry through exit._",
        "_An open position shows `now` at its latest close, unrealised. "
        "Records read W–L–S (wins–losses–scratches); scratches are counted but "
        "left out of the win rate. A `~` exit is derived from a stated gain "
        "rather than a posted fill. A trimmed position stays open and never "
        "scores, so the trimmed count shows how much of a record is still "
        "unresolved._",
    ]

    bad_week = {}
    for j in unreadable:
        if j["opened"] and parse_ts(j["opened"]) >= week_start:
            bad_week[j["user"]] = bad_week.get(j["user"], 0) + 1

    active = sorted(set(closed_week) | opened_week, key=str.lower)
    carrying = sorted(set(holdings) - set(active), key=str.lower)
    if not active and not carrying:
        lines.append("")
        lines.append("_No trades closed this week and no open positions._")
        return "\n".join(lines) + "\n"

    week_total = tally([j for items in closed_week.values() for j in items])
    closed_count = sum(week_total.values())
    if closed_count:
        community = _record(week_total, "**Community this week:**", verbose=True)
        lines.append(
            f"{community} — {closed_count} trade(s) closed by "
            f"{len(closed_week)} trader(s)."
        )

    for user in active:
        lines.append("")
        lines.append(f"## {user}")

        stats = [
            _record(tally(closed_week.get(user, [])), "This week:", verbose=True),
            _record(tally(closed_history.get(user, [])),
                    f"Last {HISTORY_DAYS}d:"),
            _open_summary(holdings.get(user, [])),
        ]
        # Posts from this week that could not be read. Counted here rather
        # than listed, so the number is visible without a wall of tickers.
        if bad_week.get(user):
            stats.append(f"{bad_week[user]} unreadable")
        stat_line = " · ".join(s for s in stats if s)
        if stat_line:
            lines.append(f"_{stat_line}_")

        week_trades = closed_week.get(user, [])
        if week_trades:
            lines.append("**Closed this week**")
            lines.extend(_journey_line(j, now) for j in week_trades)

        open_trades = holdings.get(user, [])
        if open_trades:
            lines.append("**Still open**")
            lines.extend(_open_lines(open_trades, now))

        if not week_trades and not open_trades:
            lines.append("- _no activity_")

    if carrying:
        # A trader who neither opened nor closed anything this week has no
        # review to give, so their book collapses to a single line instead of
        # a section of its own.
        lines.append("")
        lines.append("## Carrying — nothing traded this week")
        for user in carrying:
            record = _record(tally(closed_history.get(user, [])), "")
            stats = f" _{record.strip()}_" if record else ""
            lines.append(f"**{user}**{stats}")
            lines.extend(_open_lines(holdings[user], now))

    return "\n".join(lines).rstrip() + "\n"


def chunk_message(text, limit=CHUNK_LIMIT):
    """Split text into <=limit-char chunks, preferring line boundaries."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single overlong line -> hard split
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    force_backfill = "--backfill" in sys.argv
    now = datetime.now(timezone.utc)
    log = load_log()

    if not dry_run:
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("DISCORD_BOT_TOKEN environment variable is required.")
        # Tolerate a token accidentally pasted with an auth-scheme prefix.
        for prefix in ("Bot ", "Bearer "):
            if token.startswith(prefix):
                token = token[len(prefix):].strip()
        if force_backfill:
            log.pop("covered_since", None)
        backfilling = needs_backfill(log, now)
        after = fetch_after(log, now)
        raw = fetch_messages(token, SOURCE_CHANNEL_ID, after)
        added = merge_messages(log, raw)
        mode = f"{HISTORY_DAYS}-day backfill" if backfilling else "weekly incremental"
        print(f"[{mode}] Fetched {len(raw)} messages; "
              f"{added} new trade message(s) logged.")
        if backfilling:
            log["covered_since"] = history_start(now).isoformat()

        journeys = build_journeys(log_to_trades(log))
        # Closes for the "at MOC" sweeps, cached in the log so each one is
        # fetched once and the number that was used stays on the record.
        prices = log.setdefault("prices", {})
        missing = [(t, d) for t, d in sweep_lookups(journeys)
                   if f"{t}|{d}" not in prices]
        if missing:
            print(f"Fetching {len(missing)} closing price(s) for MOC exits...")
            for ticker, day in missing:
                close = yahoo_close(ticker, day)
                if close is not None:
                    prices[f"{ticker}|{day}"] = close
                time.sleep(0.4)
        mark_unreadable(journeys, now)
        marks = {}
        tickers = mark_lookups(journeys)
        if tickers:
            print(f"Marking {len(tickers)} open share position(s) to market...")
            for ticker in tickers:
                last = yahoo_last(ticker)
                if last is not None:
                    marks[ticker] = last
                time.sleep(0.4)
        log["marks"] = marks   # replaced each run: a mark is only ever current

        prune_log(log, journeys, now)
        save_log(log)

    summary = build_summary(log, now)
    chunks = chunk_message(summary)

    if dry_run:
        print("\n----- DRY RUN: summary preview -----\n")
        print(summary)
        print(f"\n({len(chunks)} chunk(s) would be posted)")
        return

    for chunk in chunks:
        post_message(token, TARGET_CHANNEL_ID, chunk)
    print(f"Posted summary in {len(chunks)} chunk(s) to channel {TARGET_CHANNEL_ID}.")


if __name__ == "__main__":
    main()
