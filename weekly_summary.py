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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://discord.com/api/v10"
SOURCE_CHANNEL_ID = "1473806053975261452"   # where YAGPDB posts trades
TARGET_CHANNEL_ID = "1525965273306235051"   # where the summary is posted

HISTORY_DAYS = 90     # rolling history the bot keeps fetched (~3 months)
WEEK_DAYS = 7         # "this week" window for the per-trader breakdown
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
    r"(?:\s+(?P<partial>[Pp]artial))?"                  # optional "partial"
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

# Free text that tells us the position was an options trade. On those lines the
# leading number is a strike, an expiry day or a contract count -- never a fill
# price -- so it must not be compared against the share price on the other side
# of the trade. The strike alternative demands at least two digits not preceded
# by a decimal point so "50c" (a strike) matches but ".50c" (fifty cents) does
# not.
OPTION_RE = re.compile(
    r"\b(?:calls?|puts?|contracts?|premium|pcs|pds|spread|strangle|straddle|"
    r"lottos?)\b"
    r"|(?<![.\d])\d{2,5}[cp]\b"      # a strike: "700p", but not ".50c"
    r"|\b\d{2,5}/\d{2,5}\b",         # spread strikes: "680/670", "700p/712c"
    re.IGNORECASE,
)

# The cost of an option leg is the premium, posted as "@1.79", "@ .36" or
# "for 3.45". On an exit "for 3.45" is just as likely to be the gain as the
# fill, so the looser form is only trusted when a position is being opened.
PREMIUM_RE = re.compile(r"@\s*\$?(\d*\.?\d+)")
PREMIUM_FOR_RE = re.compile(r"\bfor\s+\$?(\d*\.?\d+)(?!\s*%)")

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
    r"|\bhalf\s+(?:out|left)\b",
    re.IGNORECASE,
)
LOSS_RE = re.compile(
    r"\bloss(?:es)?\b|\blost\b|stopped out|stop out|took the l\b|\bthe L\b",
    re.IGNORECASE,
)
WIN_RE = re.compile(
    r"\bprofits?\b|\bgains?\b|\bwin(?:s|ner|ning)?\b", re.IGNORECASE
)
SIGNED_PCT_RE = re.compile(r"(?<![\d.])([-+])\s?(\d+(?:\.\d+)?)\s*%")
# "exit QQQ strangle for 50%" -- a bare percentage the trade was taken *for*
# means it was taken in the green; losses in this channel are always said out
# loud, and LOSS_RE has already had its turn by the time this is checked.
FOR_PCT_RE = re.compile(r"\bfor\s+(?:an?\s+)?(\d+(?:\.\d+)?)\s*%")


def _classify_notes(notes):
    """Return ('win'|'loss'|'flat'|None, display_text) read from free text."""
    if not notes:
        return None, ""
    if FLAT_RE.search(notes):
        return "flat", "scratch"
    if LOSS_RE.search(notes):
        return "loss", _trim_note(notes)
    if WIN_RE.search(notes):
        return "win", _trim_note(notes)
    m = SIGNED_PCT_RE.search(notes)
    if m:
        pct = f"{m.group(1)}{m.group(2)}%"
        return ("loss" if m.group(1) == "-" else "win"), pct
    m = FOR_PCT_RE.search(notes)
    if m:
        return "win", f"+{m.group(1)}%"
    return None, ""


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
        premium = PREMIUM_RE.search(notes)
        if premium is None and side != "Exit":
            premium = PREMIUM_FOR_RE.search(notes)
        price = float(premium.group(1)) if premium else None
    tickers = [
        t.lstrip("$").upper()
        for t in TICKER_SPLIT_RE.split(m.group("tickers"))
        if t.strip()
    ]
    outcome, result_text = _classify_notes(notes) if side == "Exit" else (None, "")
    return [
        {
            "side": side,
            "partial": bool(m.group("partial"))
            or (side == "Exit" and bool(PARTIAL_RE.search(notes))),
            "ticker": ticker,
            "price": price,
            "option": is_option,
            "notes": notes,
            "outcome": outcome,
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
        if not user or not line.startswith("#"):
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
            t.setdefault("outcome", None)
            t.setdefault("result_text", "")
            t["message_id"] = mid
            t["timestamp"] = entry.get("timestamp")
            t["index"] = i
            trades.append(t)
    trades.sort(key=lambda t: (int(t["message_id"]), t["index"]))
    return trades


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
        "adds": 0,
        "exits": [],
        "closed": False,
        "closed_at": None,
        "message_ids": [t["message_id"]],
    }


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
        key = (t["user"], t["ticker"])
        current = open_journeys.get(key)
        if t["side"] in ("Long", "Short"):
            if current and current["side"] == t["side"]:
                current["adds"] += 1          # scaling into the same position
                current["message_ids"].append(t["message_id"])
                current["option"] = current["option"] or t["option"]
                continue
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
            continue
        # Exit
        if not current:
            current = _new_journey(t)
            current["opened"] = None
            journeys.append(current)
            open_journeys[key] = current
        current["message_ids"].append(t["message_id"])
        current["option"] = current["option"] or t["option"]
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
    """Return {'outcome', 'pct', 'text'} for a journey.

    Outcome is 'open' while the position is live, otherwise it is taken from
    what the trader wrote on the exit ('for a loss', '+53%', 'for a scratch'),
    falling back to entry-vs-exit arithmetic when both numbers are comparable,
    and 'unknown' when neither is available.
    """
    if not journey["closed"]:
        return {"outcome": "open", "pct": None, "text": ""}

    stated = [e["outcome"] for e in journey["exits"] if e["outcome"]]
    text = next(
        (e["result_text"] for e in reversed(journey["exits"]) if e["result_text"]),
        "",
    )
    if not text and journey["exits"]:
        # Nothing scoreable was written, so carry the trader's own words through
        # rather than posting a line that says nothing at all.
        text = _trim_note(journey["exits"][-1]["notes"])
    pct = None
    final = journey["exits"][-1] if journey["exits"] else None
    if (
        journey["side"]
        and not journey["option"]
        and final is not None
        and not final["option"]
        and _comparable(journey["entry_price"], final["price"])
    ):
        move = (final["price"] - journey["entry_price"]) / journey["entry_price"]
        pct = 100 * (move if journey["side"] == "Long" else -move)

    if stated:
        # Several exits can disagree (a partial for profit, the runner for a
        # loss); the trader's last word on the position decides.
        outcome = stated[-1]
    elif pct is not None:
        outcome = "flat" if abs(pct) < 0.05 else ("win" if pct > 0 else "loss")
    else:
        return {"outcome": "unknown", "pct": None, "text": text}

    # A computed percentage beats the trader's prose ("for profit", "the rest"),
    # but a move that rounds to nothing is better described as a scratch.
    if pct is not None and abs(pct) >= 0.05:
        text = f"{pct:+.1f}%"
    elif not text and outcome == "flat":
        text = "scratch"
    return {"outcome": outcome, "pct": pct, "text": text}


def compute_holdings(journeys):
    """Return {user: [open journeys]}, oldest first."""
    holdings = {}
    for j in journeys:
        if not j["closed"]:
            holdings.setdefault(j["user"], []).append(j)
    for items in holdings.values():
        items.sort(key=lambda j: (j["opened"] or "", j["ticker"]))
    return holdings


def tally(journeys):
    """Count outcomes across closed journeys."""
    counts = {"win": 0, "loss": 0, "flat": 0, "unknown": 0}
    for j in journeys:
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

    entry_price = j["entry_price"]
    final_price = j["exits"][-1]["price"] if j["closed"] and j["exits"] else None
    if (
        entry_price is not None
        and final_price is not None
        and not j["option"]
        and not _comparable(entry_price, final_price)
    ):
        # "#Exit APP 3.80 per share" against an entry of 404.25 -- the exit
        # number is a gain, not a fill. Printing "404.25 → 3.8" would read as a
        # catastrophic loss, so only the entry is shown and the trader's own
        # words carry the result.
        final_price = None
    entry, exit_price = _fmt_price(entry_price), _fmt_price(final_price)
    if entry and exit_price:
        head.append(f"{entry} → {exit_price}")
    elif exit_price:
        head.append(f"→ {exit_price}")
    elif entry:
        head.append(entry)

    detail = []
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
        return f"open since {_fmt_date(opened)} ({max((now - opened).days, 0)}d)"
    if j["exits"]:
        last = parse_ts(j["exits"][-1]["timestamp"])
        return f"trimmed {_fmt_date(last)}, entry not logged"
    return "open, entry not logged"


def _record(counts, label, verbose=False):
    """'This week: 3W-2L (60%)', or None when there is nothing to report.

    ``verbose`` appends the trades that could not be scored -- scratches and
    exits whose result was never stated -- which is worth surfacing for the
    current week but only clutters the longer-run number.
    """
    wins, losses = counts["win"], counts["loss"]
    scored = wins + losses
    extra = []
    if verbose and counts["flat"]:
        extra.append(f"{counts['flat']} scratch")
    if verbose and counts["unknown"]:
        extra.append(f"{counts['unknown']} unclear")
    if not scored and not extra:
        return None
    text = f"{label} {wins}W–{losses}L"
    if scored:
        text += f" ({round(100 * wins / scored)}%)"
    if extra:
        text += ", " + " and ".join(extra)
    return text


def build_summary(log, now):
    """Build a trader-by-trader weekly review (Markdown) from the running log.

    One line per trade journey, grouped into trades closed this week and
    positions still carried, plus this-week and 90-day records per trader.
    """
    journeys = build_journeys(log_to_trades(log))
    holdings = compute_holdings(journeys)

    week_start = now - timedelta(days=WEEK_DAYS)
    horizon = history_start(now)

    closed_week = {}
    closed_history = {}
    for j in journeys:
        if not j["closed"] or not j["closed_at"]:
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
        f"_{ICON_WIN} win · {ICON_LOSS} loss · {ICON_OPEN} still open "
        f"· {ICON_FLAT} scratch · {ICON_UNKNOWN} result unclear — "
        f"one line per trade, entry through exit._",
    ]

    users = sorted(set(closed_week) | set(holdings), key=str.lower)
    if not users:
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

    for user in users:
        lines.append("")
        lines.append(f"## {user}")

        stats = [
            _record(tally(closed_week.get(user, [])), "This week:", verbose=True),
            _record(tally(closed_history.get(user, [])),
                    f"Last {HISTORY_DAYS}d:"),
        ]
        open_count = len(holdings.get(user, []))
        stats.append(f"{open_count} open" if open_count else None)
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
            lines.extend(_journey_line(j, now) for j in open_trades)

        if not week_trades and not open_trades:
            lines.append("- _no activity_")

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

        prune_log(log, build_journeys(log_to_trades(log)), now)
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
