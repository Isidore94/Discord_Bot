#!/usr/bin/env python3
"""Weekly Discord trade-summary bot.

Fetches recent trade posts from a YAGPDB-fed Discord channel via the REST API
(v10, plain HTTP -- no gateway), parses them, keeps a persistent running log so
weekly/monthly swings are never forgotten, and posts a two-section summary
("Closed this week" / "Still holding") back to a target channel.

The bot token is read from the DISCORD_BOT_TOKEN environment variable and is
never stored in code, the running log, or version control.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

import options

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://discord.com/api/v10"
SOURCE_CHANNEL_ID = "1473806053975261452"   # where YAGPDB posts trades
TARGET_CHANNEL_ID = "1525965273306235051"   # where the summary is posted

INITIAL_LOOKBACK_DAYS = 30  # first run (empty log) backfills this many days
WEEK_DAYS = 7               # "this week" window for the per-trader breakdown
RETENTION_DAYS = 400        # prune log entries older than this (open positions kept)

LOG_PATH = os.environ.get("TRADE_LOG_PATH", "trade_log.json")
CHUNK_LIMIT = 1900       # stay under Discord's 2000-char message limit

DISCORD_EPOCH = 1420070400000  # 2015-01-01T00:00:00Z in ms
USER_AGENT = (
    "trades-summary-bot/1.0 "
    "(+https://github.com/Isidore94/trades-summary)"
)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# A single Discord message can contain several "<user> posted a trade:" blocks,
# each followed by one "#..." trade line. The username capture is non-greedy and
# newline-free so it grabs exactly the name in front of " posted a trade:".
PAIR_RE = re.compile(
    r"(?P<user>[^\n]+?) posted a trade:\s*\n+\s*(?P<trade>#[^\n]+)"
)

# A trade line, e.g.
#   #Long $PENG 77.15
#   #Exit NVDA 11.70 for -32% on calls
#   #Exit CRWD at 187.60
#   #Exit partial NVDA $208.66 for over $13 profit per share. (Still have ...).
#   #add Long ALAB at 429.54. New avg: 449.76
#   #Exit half of RIVN for 50% going to leave the rest ...
#   #Exit this add on Long ALAB for a small 3 dollar loss.
# Filler words between the action and the ticker are skipped (options.FILLER);
# the price must not be a percentage ("for 100%" is a stated return, not $100).
TRADE_RE = re.compile(
    r"^#\s*"
    r"(?P<side>[Ll]ong|[Ss]hort|[Ee]xit|[Aa]dd)"   # side / scale-in
    r"(?:\s+(?P<partial>[Pp]artial))?"             # optional "partial" (Exit)
    + options.FILLER +                             # "half of", "this add on Long", ...
    r"\s+\$?(?P<ticker>[A-Z][A-Z.]{0,6})"          # optional $, then TICKER
    # price: not a percentage ("for 100%"), and not part of an "N/M" pair
    # ("CROX 1/2 at $133", spread strikes "97/96") -- the digit/slash lookahead
    # blocks even a backtracked shorter match ("9" out of "97/96").
    r"(?:\s+(?:[Aa]t\s+|@\s*)?\$?(?P<price>\d+(?:\.\d+)?)(?![\d/])(?!\s*%))?"
    r"(?P<notes>.*)$"                              # trailing free-text notes
)

# The channel's add function posts the running average itself:
#   "#add Long ALAB at 429.54. New avg: 449.76" -- that value is authoritative
# (it knows the sizes), so it replaces the tracked entry price outright.
NEW_AVG_RE = re.compile(r"(?i)new\s+avg[.:]?\s*\$?(\d+(?:\.\d+)?)")

# Free-text hints that an exit was partial, e.g. "partial profit swinging the
# rest", "#Exit half of RIVN", "leaving quarter size on", "#Exit this add on
# Long ALAB", "trimming here". Checked against the whole line. Keep-verbs
# (leave/keep/swing/hold) only count when a position word follows within a few
# words, so full-exit commentary like "still bullish long term" or "keeping an
# eye on re-entry" does NOT flip a genuine close into a partial.
PARTIAL_HINT_RE = re.compile(
    r"(?i)\bpartial\w*\b|\bhalf\b|\btrim\w*\b|"
    r"\b(?:this|that|the|my)\s+add\b|"
    r"\b(?:leav\w*|keep\w*|swing\w*|hold\w*)(?:\s+\w+){0,3}?\s+"
    r"(?:rest|half|quarter|some|position|size|runners?|shares?)\b|"
    r"\bstill\s+(?:have|holding|hold|in|long|short)\b"
)

# A stated percentage return, e.g. "for 50%", "for -32%". Used to score an
# exit when entry/exit prices can't be compared.
STATED_PCT_RE = re.compile(r"(?i)\bfor\s+(?:a\s+)?([+-]?\d+(?:\.\d+)?)\s*%")

# Free-text profit/loss sentiment, for exits that describe the result in prose
# instead of a clean price -- e.g. "for a loss at 37.60", "with a scratch",
# "with 5 dollars of profit per share", "around 1 dollar per share". Checked in
# order: scratch, then loss, then an explicit gain word, then a stated
# per-share/dollar amount (a bare amount with no loss word reads as a gain --
# losses are called out with "loss"/"down"/"red").
_SCRATCH_RE = re.compile(r"(?i)\bscratch|\bb/?e\b|break\s*even|breakeven|"
                         r"\beven\b|\bflat\b")
_LOSS_RE = re.compile(r"(?i)\b(?:loss|lost|red)\b|stopped\s+out|took\s+the\s+loss|"
                      r"\bdown\s+\$?\d")
_GAIN_RE = re.compile(r"(?i)\b(?:profit|profits|gain|gains|green|winner?)\b")
_AMOUNT_RE = re.compile(r"(?i)\$\d|\b\d+(?:\.\d+)?\s*(?:dollar|cent|buck)|"
                        r"\bper\s+share\b")


def _note_outcome(text):
    """Classify an exit's free text as 'win' / 'loss' / 'scratch', or None when
    there is no usable signal. Used only when no numeric return was parsed."""
    t = text or ""
    if _SCRATCH_RE.search(t):
        return "scratch"
    if _LOSS_RE.search(t):
        return "loss"
    if _GAIN_RE.search(t) or _AMOUNT_RE.search(t):
        return "win"
    return None


def parse_trade_line(line, ref_date=None):
    """Parse one '#Long/Short/Exit ...' line into a dict, or None if no match.

    An option line (call/put with a strike and expiration date) is recognized
    first and enriched with option fields (``instrument="option"``, ``opt_type``,
    ``strike``, ``expiration``, ``premium``); otherwise the line is parsed as a
    plain-stock trade. An option Exit closes the contract early, with the
    premium received in the price slot. ``ref_date`` is passed through only to
    infer an expiration year written without one.

    Post-parse enrichment (both kinds): an Exit whose free text signals a
    partial ("half", "swinging the rest", ...) gets partial=True; an Add
    carries ``new_avg`` when the channel's add function posted one; an Exit
    stating its return ("for 50%") carries ``stated_pct`` as a fraction.
    """
    trade = None
    opt = options.parse_option(line, ref_date=ref_date)
    spread = options.parse_spread(line, ref_date=ref_date) if opt is None else None
    if opt:
        opt["instrument"] = "option"
        opt["price"] = opt["premium"]  # so existing price-aware helpers still work
        trade = opt
    elif spread:
        spread["price"] = spread["premium"]
        trade = spread
    else:
        m = TRADE_RE.match(line.strip())
        if not m:
            return None
        price = m.group("price")
        trade = {
            "side": m.group("side").capitalize(),   # Long / Short / Exit / Add
            "partial": bool(m.group("partial")),
            "ticker": m.group("ticker").upper().strip("."),
            "price": float(price) if price is not None else None,
            "notes": (m.group("notes") or "").strip(),
            "instrument": "stock",
        }

    if trade["side"] == "Exit":
        if not trade["partial"] and PARTIAL_HINT_RE.search(line):
            trade["partial"] = True
        pm = STATED_PCT_RE.search(line)
        if pm:
            trade["stated_pct"] = float(pm.group(1)) / 100.0
    elif trade["side"] == "Add":
        am = NEW_AVG_RE.search(line)
        if am:
            trade["new_avg"] = float(am.group(1))
    return trade


def parse_message(content, ref_date=None):
    """Return a list of trade dicts parsed from one message's raw content.

    Each dict carries the poster's username plus the parsed trade fields. A
    message with multiple poster/trade pairs yields multiple dicts. ``ref_date``
    is threaded through to option-expiration year inference.
    """
    trades = []
    for m in PAIR_RE.finditer(content or ""):
        trade = parse_trade_line(m.group("trade"), ref_date=ref_date)
        if trade:
            trade["user"] = m.group("user").strip()
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


def fetch_messages(token, channel_id, after_snowflake, max_pages=200):
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


def fetch_after(log, now):
    """Snowflake to fetch messages after.

    First run (empty log): backfill INITIAL_LOOKBACK_DAYS. Afterwards: resume
    from the newest message already logged, so each weekly run only pulls what
    is new (gap-free even if a run is missed) while the running log preserves
    the full history of open positions.
    """
    ids = [int(mid) for mid in log.get("messages", {})]
    if ids:
        return max(ids)
    return snowflake_for(now - timedelta(days=INITIAL_LOOKBACK_DAYS))


def _is_option(t):
    """True if a trade dict describes an option OR spread position (both are
    keyed and resolved through the same contract machinery)."""
    return t.get("instrument") in ("option", "spread") or bool(t.get("expiration"))


def _pos_key(t):
    """Identity of the position a trade acts on.

    Stocks key on (user, ticker) as before. Options/spreads key on the full
    contract so they never clobber -- nor are clobbered by -- a same-ticker
    stock position or a different contract on the same underlying.
    """
    if _is_option(t):
        return (t["user"], t["ticker"], t.get("instrument", "option"),
                t.get("opt_type"), t.get("strike"), t.get("expiration"))
    return (t["user"], t["ticker"])


def _contract_live_on(exp_iso, trade_date):
    """True if a contract with the given ISO expiration is still live (not yet
    expired) on trade_date. Unknown dates count as live."""
    if trade_date is None or not exp_iso:
        return True
    try:
        return date.fromisoformat(exp_iso) >= trade_date
    except (ValueError, TypeError):
        return True


def _trade_date(t):
    """The calendar date a trade was posted, or None."""
    ts = t.get("timestamp")
    if not ts:
        return None
    try:
        return parse_ts(ts).date()
    except (ValueError, TypeError):
        return None


def _fallback_option_key(open_keys, t):
    """Key of the option a plain-stock Exit/Add most plausibly acts on.

    Traders often close (or add to) an option with an informal '#Exit TICKER
    ...' line (real example: '#Exit NVDA 11.70 for -32% on calls'). When such
    a line matches no open stock position but the trader has exactly ONE open
    option contract on that ticker that is still live (its expiration has not
    passed as of the line's date), treat it as acting on that contract. An
    already-expired contract is never matched -- an exit posted after expiry
    refers to something else (typically selling assigned shares), and scoring
    a share price against an option premium produces garbage. Ambiguous
    (two+ live contracts) -> None, and the line is ignored as before.
    """
    when = _trade_date(t)
    cands = [k for k in open_keys
             if len(k) > 2 and k[0] == t["user"] and k[1] == t["ticker"]
             and _contract_live_on(k[5], when)]
    return cands[0] if len(cands) == 1 else None


def _tranche_pct(side, entry_price, t, entry_is_option=False):
    """Signed fractional return of one exit tranche.

    Normally computed from entry vs exit price (inverted for shorts), falling
    back to the trader's own stated return ("for 50%"). For an INFORMAL
    stock-form exit matched to an option position the units of the parsed
    price are unreliable (it may be an underlying price, not a premium), so a
    stated return takes precedence there ('#Exit NVDA 11.70 for -32% on
    calls' scores -32%, not 11.70-vs-premium).
    """
    if entry_is_option and not _is_option(t) \
            and t.get("stated_pct") is not None:
        return t["stated_pct"]
    if entry_price and t.get("price") is not None:
        pct = (t["price"] - entry_price) / entry_price
        return -pct if side == "Short" else pct
    return t.get("stated_pct")


def log_to_trades(log):
    """Flatten the log into a chronologically sorted list of trade dicts.

    Entries store raw ``content`` and are re-parsed each run; older entries
    that stored a pre-parsed ``trades`` list are still supported.
    """
    trades = []
    for mid, entry in log.get("messages", {}).items():
        ref = None
        ts = entry.get("timestamp")
        if ts:
            try:
                ref = parse_ts(ts).date()
            except (ValueError, TypeError):
                ref = None
        parsed = parse_message(entry["content"], ref_date=ref) if "content" in entry \
            else entry.get("trades", [])
        for i, tr in enumerate(parsed):
            t = dict(tr)
            t["message_id"] = mid
            t["timestamp"] = entry.get("timestamp")
            t["index"] = i
            trades.append(t)
    trades.sort(key=lambda t: (int(t["message_id"]), t["index"]))
    return trades


def _note_source(pos, t):
    """Track every message id contributing to a still-open position (open,
    adds, partials) so prune_log can protect all of them, not just the open."""
    ids = pos.setdefault("src_ids", [pos.get("message_id")])
    if t.get("message_id"):
        ids.append(t["message_id"])


def compute_holdings(trades, orphan_exits=None):
    """Return {user: [open trade dicts]} from chronologically ordered trades.

    Long/Short opens (or refreshes) a position; a same-side refresh carries
    over any banked partial tranches (realized P&L must survive a re-post).
    An Add (the channel's add function) scales in: the posted "New avg"
    replaces the entry price when present (it accounts for real sizes);
    otherwise the add price is averaged in equal-weight; the original open
    date is kept. A full Exit closes the position; a partial Exit leaves it
    open but is tallied on it (count and per-tranche returns) so the eventual
    close can score the whole position. Exits/Adds that match no stock
    position fall back to the trader's single LIVE open option on that ticker
    (see _fallback_option_key).

    A full stock-form Exit that matches nothing is appended to
    ``orphan_exits`` when a list is passed -- resolve_expired_options uses
    these to recognize the sale of shares acquired through assignment.
    """
    open_positions = {}  # position key -> opening trade
    for t in trades:
        key = _pos_key(t)
        if t["side"] in ("Long", "Short"):
            prev = open_positions.get(key)
            if prev and prev["side"] == t["side"] \
                    and (prev.get("partials") or prev.get("partial_pcts")):
                t = dict(t)  # same-side refresh: keep banked partial history
                t["partials"] = prev.get("partials", 0)
                t["partial_pcts"] = list(prev.get("partial_pcts", []))
                t["src_ids"] = list(prev.get("src_ids", [])) + \
                    ([t["message_id"]] if t.get("message_id") else [])
            open_positions[key] = t
        elif t["side"] == "Add":
            if key not in open_positions and not _is_option(t):
                key = _fallback_option_key(open_positions, t) or key
            pos = open_positions.get(key)
            if pos is None:
                # Add with no tracked entry (opened before the log window):
                # best effort, treat it as opening a long at the known price.
                t = dict(t)
                t["side"] = "Long"
                if t.get("new_avg") is not None:
                    t["price"] = t["new_avg"]
                open_positions[_pos_key(t)] = t
                continue
            pos = dict(pos)
            if t.get("new_avg") is not None:
                pos["price"] = t["new_avg"]
            elif pos.get("price") is not None and t.get("price") is not None:
                n = pos.get("adds", 1)
                pos["price"] = (pos["price"] * n + t["price"]) / (n + 1)
            if _is_option(pos):
                pos["premium"] = pos["price"]
            pos["adds"] = pos.get("adds", 1) + 1
            _note_source(pos, t)
            open_positions[key] = pos
        elif t["side"] == "Exit":
            if key not in open_positions and not _is_option(t):
                key = _fallback_option_key(open_positions, t) or key
            if t["partial"]:
                pos = open_positions.get(key)
                if pos is not None:
                    pos["partials"] = pos.get("partials", 0) + 1
                    econ = options.economic_side(pos["side"], pos.get("opt_type"))
                    pct = _tranche_pct(econ, pos.get("price"), t,
                                       entry_is_option=_is_option(pos))
                    if pct is not None:
                        pos.setdefault("partial_pcts", []).append(pct)
                    _note_source(pos, t)
            elif open_positions.pop(key, None) is None \
                    and orphan_exits is not None and not _is_option(t):
                orphan_exits.append(t)
    holdings = {}
    for t in open_positions.values():
        holdings.setdefault(t["user"], []).append(t)
    return holdings


def _blank_stats():
    return {"wins": 0, "losses": 0, "pct_sum": 0.0, "pct_n": 0,
            "week_wins": 0, "week_losses": 0,
            "week_pct_sum": 0.0, "week_pct_n": 0}


def _apply_note_outcome(t, stats, week_start):
    """For a full exit with no numeric return, read profit/loss from its free
    text. Stamp ``t["outcome"]`` ('win'/'loss'/'scratch') for the icon, and
    count a win or loss into the weekly tally (a scratch counts as neither)."""
    outcome = _note_outcome(t.get("notes"))
    if not outcome:
        return
    t["outcome"] = outcome
    if outcome == "scratch":
        return
    s = stats.setdefault(t["user"], _blank_stats())
    s["wins" if outcome == "win" else "losses"] += 1
    if week_start is not None and t.get("timestamp") \
            and parse_ts(t["timestamp"]) >= week_start:
        s["week_wins" if outcome == "win" else "week_losses"] += 1


def compute_win_rates(trades, week_start=None):
    """Quasi win rate and return stats per user, one data point per POSITION.

    Entry prices come from Long/Short opens, adjusted by Adds (the channel add
    function's "New avg" is authoritative when posted, otherwise the add price
    is averaged in equal-weight). A partial Exit does not score on its own: its
    tranche return is remembered (computed from entry vs exit price -- premiums
    for options -- or the trader's stated "for 50%"), and when the position
    finally closes, all partials plus the closing exit are assumed EQUAL SIZE
    and combine into a single win/loss at their average return. Positions whose
    exits carry no usable numbers are ignored.

    Per user returns wins/losses (all-time), pct_sum/pct_n (for the
    average-%-per-trade line), and week_wins/week_losses for positions closed
    at or after ``week_start`` (both 0 when it is None). Exit trade dicts are
    annotated in place: each partial with its own ``pct``, the closing exit
    with the combined ``pct``, its ``partials`` count, and ``held_days``.
    """
    entries = {}  # position key -> {"side","price","timestamp","adds",
                  #                  "partials","partial_pcts"}
    stats = {}    # user -> stats dict
    for t in trades:
        key = _pos_key(t)
        if t["side"] in ("Long", "Short"):
            prev = entries.get(key)
            partials, partial_pcts = 0, []
            if prev and (prev["partials"] or prev["partial_pcts"]):
                if prev["side"] == t["side"]:
                    # Same-side refresh: banked partial tranches carry over.
                    partials = prev["partials"]
                    partial_pcts = list(prev["partial_pcts"])
                elif prev["partial_pcts"]:
                    # Side flip ends the old trade: score its banked partials.
                    combined = sum(prev["partial_pcts"]) / len(prev["partial_pcts"])
                    s = stats.setdefault(t["user"], _blank_stats())
                    s["wins" if combined > 0 else "losses"] += 1
                    s["pct_sum"] += combined
                    s["pct_n"] += 1
            entries[key] = {"side": t["side"], "opt_type": t.get("opt_type"),
                            "price": t["price"], "timestamp": t.get("timestamp"),
                            "adds": 1, "partials": partials,
                            "partial_pcts": partial_pcts,
                            "label": options._contract(t) if _is_option(t) else None}
        elif t["side"] == "Add":
            if key not in entries and not _is_option(t):
                key = _fallback_option_key(entries, t) or key
            info = entries.get(key)
            if info is None:
                price = t.get("new_avg", t.get("price"))
                entries[key] = {"side": "Long", "opt_type": t.get("opt_type"),
                                "price": price, "timestamp": t.get("timestamp"),
                                "adds": 1, "partials": 0, "partial_pcts": []}
                continue
            if t.get("new_avg") is not None:
                info["price"] = t["new_avg"]
            elif info["price"] is not None and t.get("price") is not None:
                n = info["adds"]
                info["price"] = (info["price"] * n + t["price"]) / (n + 1)
            info["adds"] += 1
        elif t["side"] == "Exit":
            if key not in entries and not _is_option(t):
                key = _fallback_option_key(entries, t) or key
            info = entries.get(key)
            if info is None:
                # No tracked entry to price against, but a clear profit/loss
                # word still sets the icon and counts as a close this week.
                if not t["partial"]:
                    _apply_note_outcome(t, stats, week_start)
                continue
            econ = options.economic_side(info["side"], info.get("opt_type"))
            pct = _tranche_pct(econ, info["price"], t,
                               entry_is_option=len(key) > 2)
            if pct is not None:
                t["pct"] = pct
            # Stamp the entry onto the exit so "Closed this week" can render the
            # whole round trip (entry → exit) even when the open predates the week
            # or the exit was written as a bare stock-form line.
            t["entry_side"] = info["side"]
            t["entry_price"] = info["price"]
            if info.get("label"):
                t["entry_contract"] = info["label"]
                t["entry_opt_type"] = info.get("opt_type")
            if info["timestamp"] and t.get("timestamp"):
                t["held_days"] = max(0, (parse_ts(t["timestamp"])
                                         - parse_ts(info["timestamp"])).days)
            if t["partial"]:
                info["partials"] += 1
                if pct is not None:
                    info["partial_pcts"].append(pct)
                continue
            # Full exit: one data point for the whole position, all tranches
            # (partials + this close) assumed equal size.
            tranches = info["partial_pcts"] + ([pct] if pct is not None else [])
            if tranches:
                combined = sum(tranches) / len(tranches)
                win = combined > 0
                t["pct"] = combined
                if info["partials"]:
                    t["partials"] = info["partials"]
                s = stats.setdefault(t["user"], _blank_stats())
                s["wins" if win else "losses"] += 1
                s["pct_sum"] += combined
                s["pct_n"] += 1
                if week_start is not None and t.get("timestamp") \
                        and parse_ts(t["timestamp"]) >= week_start:
                    s["week_wins" if win else "week_losses"] += 1
                    s["week_pct_sum"] += combined
                    s["week_pct_n"] += 1
            else:
                # No numeric return -> fall back to the exit's prose.
                _apply_note_outcome(t, stats, week_start)
            entries.pop(key, None)
    return stats


def _consume_orphan_sale(orphan_exits, user, ticker, exp_date):
    """Pop and return the first unmatched full stock exit by ``user`` on
    ``ticker`` posted on/after ``exp_date`` -- i.e. the sale of the shares
    acquired through assignment at that expiration. None if there is none."""
    if not orphan_exits:
        return None
    for i, o in enumerate(orphan_exits):
        if o["user"] == user and o["ticker"] == ticker:
            when = _trade_date(o)
            if when is None or when >= exp_date:
                return orphan_exits.pop(i)
    return None


def resolve_expired_options(holdings, now, spot_close=options.spot_close_on,
                            cache=None, orphan_exits=None):
    """Settle open OPTION positions whose expiration date has passed.

    ``holdings`` (as returned by ``compute_holdings``) is mutated in place: each
    resolvable expired option is removed from the trader's open list, and a
    short put that gets assigned is replaced by a long stock holding at its
    assignment cost basis (strike minus premium) so the acquired shares keep
    being tracked. An option whose spot price can't be fetched -- yfinance or
    the network unavailable, or the contract not yet expired -- is left open.

    Wheel linkage: an assigned short CALL additionally closes the trader's
    tracked long shares of the same ticker (covered call / wheel), realizing
    the share P&L against the effective sale price (strike + premium) so the
    outcome scores as a win or loss instead of dangling.

    ``spot_close(ticker, exp_date)`` is injectable for testing; it defaults to
    the live yfinance lookup and returns None when a price is unavailable.
    ``cache`` (a mutable {"TICKER:YYYY-MM-DD": close} dict, persisted in the
    running log) short-circuits refetching spots for expirations settled on
    earlier runs -- historical closes never change.

    ``orphan_exits`` (from compute_holdings) are full stock exits that matched
    no tracked position: one posted on/after a put's assignment is taken as
    the sale of the assigned shares, so those shares are NOT kept as a holding
    and the sale return (vs the assignment basis) scores as a tranche.

    Earlier partial exits, the settlement itself, and any assigned-share sale
    are combined as equal-size tranches into the outcome's single pct/win --
    the same rule compute_win_rates applies to a regular close.

    Returns ``{user: [resolution, ...]}`` where each resolution is
    ``{"trade": <option>, "spot": <float>, "exp_date": <date>, "outcome": <dict>}``.
    """
    today = now.date()
    resolutions = {}
    for user, positions in list(holdings.items()):
        kept = []
        for t in positions:
            exp = t.get("expiration") if _is_option(t) else None
            exp_date = date.fromisoformat(exp) if exp else None
            if exp_date is None or exp_date > today:
                kept.append(t)  # not an option, or not yet expired
                continue
            cache_key = f"{t['ticker']}:{exp}"
            spot = cache.get(cache_key) if cache is not None else None
            if spot is None:
                spot = spot_close(t["ticker"], exp_date)
            if spot is None:
                kept.append(t)  # price unavailable -> leave open/pending
                continue
            if cache is not None:
                cache[cache_key] = spot
            if t.get("instrument") == "spread":
                # PCS wins above the first strike (theta); CDS/PDS are
                # directional (win when the debit spread finishes ITM).
                outcome = options.resolve_spread(
                    t["strike"], t.get("premium"), spot,
                    spread_type=t.get("spread_type", "PCS"))
            else:
                outcome = options.resolve_option(
                    t["side"], t["opt_type"], t["strike"], t.get("premium"), spot
                )
            # Only a theta put (economic short) ever "assigns"; a directional
            # bought put finishing ITM is "exercised". So status + opt_type is
            # enough -- no need to check the (inverted) Long/Short label. Spreads
            # never assign (no shares change hands).
            assigned_put = (outcome["status"] == "assigned"
                            and t["opt_type"] == options.PUT
                            and t.get("instrument") != "spread")
            # Were the assigned shares already sold by a later unmatched exit?
            sale, sale_pct = None, None
            if assigned_put:
                sale = _consume_orphan_sale(orphan_exits, user, t["ticker"],
                                            exp_date)
                if sale is not None:
                    sale_pct = _tranche_pct("Long", outcome["basis"], sale)
            # Earlier partial exits, the settlement, and any assigned-share
            # sale combine as equal-size tranches -- the same rule a regular
            # close applies (mean return decides the single win/loss).
            partial_pcts = list(t.get("partial_pcts") or [])
            tranches = list(partial_pcts)
            if outcome.get("pct") is not None:
                tranches.append(outcome["pct"])
            if sale_pct is not None:
                tranches.append(sale_pct)
            if tranches:
                combined = sum(tranches) / len(tranches)
                outcome["pct"] = combined
                outcome["win"] = combined > 0
            extras = []
            if partial_pcts:
                n = t.get("partials", len(partial_pcts))
                extras.append(f"{n} earlier partial{'s' if n != 1 else ''}")
            if sale is not None:
                sold = (f"shares later sold at {options._d(sale['price'])}"
                        if sale.get("price") is not None else "shares later sold")
                extras.append(sold)
            if extras:
                if tranches:
                    outcome["summary"] += (f" — net {outcome['pct'] * 100:+.1f}% "
                                           f"incl. {' + '.join(extras)}")
                else:
                    outcome["summary"] += f" — {' + '.join(extras)}"
            resolutions.setdefault(user, []).append(
                {"trade": t, "spot": spot, "exp_date": exp_date, "outcome": outcome}
            )
            # Assigned shares not yet sold become a tracked long stock position.
            if assigned_put and sale is None:
                kept.append({
                    "user": user,
                    "ticker": t["ticker"],
                    "side": "Long",
                    "price": outcome["basis"],
                    "partial": False,
                    "instrument": "stock",
                    "assigned": True,
                    "notes": f"assigned from short {t['strike']:g}p exp {exp}",
                    "timestamp": t.get("timestamp"),
                    "message_id": t.get("message_id"),
                    "index": t.get("index"),
                })
        # Wheel linkage: each assigned short call closes tracked long shares.
        for rec in resolutions.get(user, []):
            t, oc = rec["trade"], rec["outcome"]
            if not (oc["status"] == "assigned" and t["side"] == "Short"
                    and t["opt_type"] == options.CALL):
                continue
            for i, h in enumerate(kept):
                if not _is_option(h) and h["side"] == "Long" \
                        and h["ticker"] == t["ticker"]:
                    shares = kept.pop(i)
                    entry = shares.get("price")
                    if entry:
                        eff = oc["basis"]  # strike + premium received
                        share_pct = (eff - entry) / entry
                        tranches = list(t.get("partial_pcts") or []) + [share_pct]
                        combined = sum(tranches) / len(tranches)
                        oc["win"] = combined > 0
                        oc["pnl"] = eff - entry
                        oc["pct"] = combined
                        oc["summary"] += (f" — shares {share_pct * 100:+.1f}% "
                                          f"from {options._d(entry)} entry")
                    else:
                        oc["summary"] += " — closed the tracked share position"
                    break
        if kept:
            holdings[user] = kept
        else:
            holdings.pop(user, None)
    return resolutions


def prune_log(log, holdings, now):
    """Drop messages older than RETENTION_DAYS, but always keep those that
    opened a position that is still held (so long swings survive). Cached
    settlement spots for expirations past retention are dropped too."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    # Protect every message a still-open position was built from: the open
    # itself plus its adds and partial exits (src_ids), so a long swing's
    # averaged entry and banked partials survive retention.
    keep_ids = {mid
                for trades in holdings.values() for t in trades
                for mid in (t.get("src_ids") or [t.get("message_id")])
                if mid}
    store = log.get("messages", {})
    for mid in list(store.keys()):
        if mid in keep_ids:
            continue
        if parse_ts(store[mid]["timestamp"]) < cutoff:
            del store[mid]
    cache = log.get("spot_cache", {})
    for key in list(cache.keys()):
        try:
            exp = date.fromisoformat(key.split(":", 1)[1])
        except (IndexError, ValueError):
            del cache[key]
            continue
        if exp < cutoff.date():
            del cache[key]


# ---------------------------------------------------------------------------
# Summary building
# ---------------------------------------------------------------------------
def _fmt_date(dt):
    return f"{dt.strftime('%b')} {dt.day}"


def _fmt_price(price):
    return f"{price:g}" if price is not None else ""


def _horizon(t):
    """'day trade' (held 0d) or 'swing Nd' (held >0d), '' if hold unknown."""
    d = t.get("held_days")
    if d is None:
        return ""
    return "day trade" if d == 0 else f"swing {d}d"


def _closed_suffix(t):
    """'(+3.4%, 2 partials, swing 12d)' tail for a closed round trip."""
    bits = []
    if t.get("pct") is not None:
        bits.append(f"{t['pct'] * 100:+.1f}%")
    n = t.get("partials")
    if n:
        bits.append(f"{n} partial{'s' if n != 1 else ''}")
    horizon = _horizon(t)
    if horizon:
        bits.append(horizon)
    return f" ({', '.join(bits)})" if bits else ""


def _closed_icon(t):
    pct = t.get("pct")
    if pct is not None:
        if pct > 0:
            return "✅"
        if pct < 0:
            return "❌"
        return "➖"          # exactly flat
    # No numeric return: use the profit/loss read from the exit's prose.
    return {"win": "✅", "loss": "❌", "scratch": "➖"}.get(t.get("outcome"), "➖")


def _closed_line(t):
    """One line for a position fully CLOSED this week, anchored on the exit
    trade (compute_win_rates stamps it with entry_side/entry_price, so the
    whole round trip renders as `entry → exit` even for a multi-week swing).
    """
    icon = _closed_icon(t)
    notes = f" — {t['notes']}" if t.get("notes") else ""
    suffix = _closed_suffix(t)
    entry_price = t.get("entry_price")
    entry_side = t.get("entry_side", "")
    # A contract label from either the exit itself (structured option/spread
    # exit) or the matched entry (informal bare-stock exit of an option/spread).
    contract = options._contract(t) if _is_option(t) else t.get("entry_contract")
    if contract:
        is_spread = any(f" {k}" in contract for k in ("PCS", "CDS", "PDS"))
        prefix = "Spread " if is_spread else f"{entry_side} "
        exit_p = options._d(t["price"]) if t.get("price") is not None else None
        arrow = f" → {exit_p}" if exit_p else " → Exit"
        if entry_price is not None:
            body = f"{prefix}**{contract}**: {options._d(entry_price)}{arrow}"
        else:
            body = f"Exit **{contract}**" + (f" @ {exit_p}" if exit_p else "")
    else:
        exit_price = _fmt_price(t.get("price"))
        exit_str = f" @ {exit_price}" if exit_price else ""
        if entry_price is not None:
            body = (f"**{t['ticker']}**: {entry_side} @ {_fmt_price(entry_price)} "
                    f"→ Exit{exit_str}")
        else:
            body = f"Exit **{t['ticker']}**{exit_str}"
    return f"- {icon} {body}{suffix}{notes}"


def _trim_suffix(t):
    """' · trimmed 2× (+50%, +100%)' for a still-open position that took
    partial exits, so those trims read as one line on the position instead of
    a separate bullet per partial post."""
    n = t.get("partials", 0)
    if not n:
        return ""
    pcts = t.get("partial_pcts") or []
    if pcts:
        detail = ", ".join(f"{p * 100:+.0f}%" for p in pcts)
        return f" · trimmed {n}× ({detail})"
    return f" · trimmed {n}×"


def _open_line(t, now=None, marks=None):
    """One line describing a trader's still-open position.

    ``marks`` ({ticker: latest close}) appends a mark-to-market for stocks and
    the underlying spot for options; ``now`` drives the expires-soon tag.
    """
    mark = (marks or {}).get(t["ticker"])
    opened = f" _(opened {_fmt_date(parse_ts(t['timestamp']))})_" \
        if t.get("timestamp") else ""
    if _is_option(t):
        tags = f" (spot {mark:g})" if mark is not None else ""
        if now is not None:
            exp = date.fromisoformat(t["expiration"])
            today = now.date()
            if exp <= today:
                tags += " _(awaiting settlement)_"
            elif exp <= today + timedelta(days=7):
                tags += " ⏳ expires this week"
        return f"- {options.format_option_open(t)}{tags}{_trim_suffix(t)}{opened}"
    price = _fmt_price(t["price"])
    price_str = f" @ {price}" if price else ""
    mtm = ""
    if mark is not None and t.get("price"):
        upct = (mark - t["price"]) / t["price"]
        if t["side"] == "Short":
            upct = -upct
        mtm = f" → {mark:g} ({upct * 100:+.1f}%)"
    tags = f" (avg of {t['adds']})" if t.get("adds", 1) > 1 else ""
    if t.get("assigned"):
        tags += " _(assigned)_"
    return f"- {t['side']} **{t['ticker']}**{price_str}{tags}{mtm}{_trim_suffix(t)}{opened}"


def _win_rate_line(stats):
    """Weekly win-rate line for a trader, or None if nothing closed this week.

    Only this week's closed trades are scored -- tracking a longer history was
    more than the channel wanted.
    """
    if not stats:
        return None
    wins, losses = stats.get("week_wins", 0), stats.get("week_losses", 0)
    total = wins + losses
    if total == 0:
        return None
    pct = round(100 * wins / total)
    parts = [f"Win rate this week: **{pct}%** ({wins}W–{losses}L)"]
    if stats.get("week_pct_n"):
        avg = stats["week_pct_sum"] / stats["week_pct_n"] * 100
        parts.append(f"avg {avg:+.1f}%/trade")
    return "_" + " · ".join(parts) + "_"


def build_summary(log, now, spot_close=options.spot_close_on, last_close=None):
    """Build a trader-by-trader summary (Markdown) from the running log.

    Per trader: an all-time/weekly stats line, trades taken this week, options
    that settled this week, and outstanding open trades. A stock position
    opened with Long/Short stays open until a full Exit -- a partial Exit does
    not close it. An option stays open until it is exited early or its
    expiration date passes, at which point it is settled against the
    underlying's closing spot (via ``spot_close``, cached in the log):
    out-of-the-money it expires (a win for the seller, a loss for the buyer);
    in-the-money a short option is assigned and a long option is worth its
    intrinsic value. Settled option wins/losses fold into the quasi win rate.

    ``last_close`` (an injectable ``tickers -> {ticker: close}``, e.g.
    options.last_closes) marks open positions to market; None skips marking,
    keeping direct calls and tests network-free.
    """
    trades = log_to_trades(log)
    week_start = now - timedelta(days=WEEK_DAYS)
    orphan_exits = []
    holdings = compute_holdings(trades, orphan_exits=orphan_exits)
    win_rates = compute_win_rates(trades, week_start=week_start)
    resolutions = resolve_expired_options(
        holdings, now, spot_close, cache=log.setdefault("spot_cache", {}),
        orphan_exits=orphan_exits,
    )

    # Fold settled options into the quasi win rate (all of them, so the rate is
    # stable run-to-run) and collect the ones that settled this week to display.
    settled_by_user = {}
    for user, recs in resolutions.items():
        for rec in recs:
            oc = rec["outcome"]
            this_week = rec["exp_date"] >= week_start.date()
            if oc["win"] is not None:
                s = win_rates.setdefault(user, _blank_stats())
                s["wins" if oc["win"] else "losses"] += 1
                if oc.get("pct") is not None:
                    s["pct_sum"] += oc["pct"]
                    s["pct_n"] += 1
                if this_week:
                    s["week_wins" if oc["win"] else "week_losses"] += 1
                    if oc.get("pct") is not None:
                        s["week_pct_sum"] += oc["pct"]
                        s["week_pct_n"] += 1
            if this_week:
                settled_by_user.setdefault(user, []).append(rec)

    marks = {}
    if last_close is not None:
        open_tickers = {t["ticker"] for held in holdings.values() for t in held}
        if open_tickers:
            marks = last_close(sorted(open_tickers)) or {}

    weekly_by_user = {}
    for t in trades:
        if parse_ts(t["timestamp"]) >= week_start:
            weekly_by_user.setdefault(t["user"], []).append(t)

    if week_start.month == now.month:
        label = f"{_fmt_date(week_start)}–{now.day}, {now.year}"
    else:
        label = f"{_fmt_date(week_start)}–{_fmt_date(now)}, {now.year}"

    lines = [f"# \U0001F4CA Weekly Trade Summary — {label}", ""]

    users = sorted(
        set(weekly_by_user) | set(holdings) | set(settled_by_user), key=str.lower
    )
    if not users:
        lines.append("_No trades this week and no open positions._")
        return "\n".join(lines)

    for user in users:
        lines.append(f"## {user}")

        wr_line = _win_rate_line(win_rates.get(user))
        if wr_line:
            lines.append(wr_line)

        # Closed this week: only positions that FULLY closed this week (a full
        # exit), one round-trip line each. Opens, adds, and partials of a
        # still-open position are not repeated here -- they live under Open
        # trades -- so a position doesn't appear both as "taken" and "open".
        week_trades = weekly_by_user.get(user, [])
        closed = [t for t in week_trades if t["side"] == "Exit" and not t["partial"]]
        lines.append("**Closed this week**")
        if closed:
            lines.extend(_closed_line(t) for t in closed)
        else:
            lines.append("- _no closed trades this week_")

        settled = settled_by_user.get(user, [])
        if settled:
            lines.append("**Options settled this week**")
            lines.extend(
                options.format_option_resolution(r["trade"], r["outcome"], r["spot"])
                for r in sorted(settled, key=lambda r: r["trade"]["ticker"])
            )

        lines.append("**Open trades**")
        open_trades = holdings.get(user, [])
        if open_trades:
            lines.extend(
                _open_line(t, now=now, marks=marks)
                for t in sorted(open_trades, key=lambda x: x["ticker"])
            )
        else:
            lines.append("- _none_")

        lines.append("")

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
        first_run = not log.get("messages")
        after = fetch_after(log, now)
        raw = fetch_messages(token, SOURCE_CHANNEL_ID, after)
        added = merge_messages(log, raw)
        mode = "initial 30-day backfill" if first_run else "weekly incremental"
        print(f"[{mode}] Fetched {len(raw)} messages; "
              f"{added} new trade message(s) logged.")

        holdings = compute_holdings(log_to_trades(log))
        prune_log(log, holdings, now)

    if dry_run:
        # Preview is fully offline: no settlement fetches, no mark-to-market.
        summary = build_summary(log, now, spot_close=lambda ticker, d: None)
    else:
        # Settlement inside build_summary adds to the log's spot cache, so the
        # log is saved after the summary is built -- but ALWAYS saved, even if
        # summary building crashes, so this run's fetched messages are not lost.
        try:
            summary = build_summary(log, now, last_close=options.last_closes)
        finally:
            save_log(log)
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
