#!/usr/bin/env python3
"""Weekly Discord trade-summary bot.

Fetches recent trade posts from a YAGPDB-fed Discord channel via the REST API
(v10, plain HTTP -- no gateway), parses them, keeps a persistent running log so
weekly/monthly swings are never forgotten, and posts a trader-by-trader summary
back to a target channel.

Trades are grouped into "episodes": a Long/Short (plus any Adds) opens a
position, partial exits attach to it, and a full exit closes it. The summary
renders one line per closed episode instead of one line per message, so a
day trade with three partials is a single line, not four.

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

INITIAL_LOOKBACK_DAYS = 30  # first run (empty log) backfills this many days
WEEK_DAYS = 7               # "this week" window for the per-trader breakdown
RETENTION_DAYS = 400        # prune log entries older than this (open positions kept)

# Bump when the parser learns to read messages it previously skipped: the next
# run re-fetches the full lookback window (dedup by message id) so trades that
# never made it into the log are recovered.
PARSER_VERSION = 3

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
# A single Discord message can contain several "<user> posted a trade:" blocks.
# Everything between one poster line and the next belongs to that poster, and
# the "#..." tag may sit anywhere in the block ("Swing port trade: #Long AVGO",
# "off to golf. #Exit EA for a 40 cent loss"), not only at line start.
POSTER_RE = re.compile(r"^(?P<user>[^\n]+?) posted a trade:", re.M)

TAG_RE = re.compile(r"#\s*(?P<side>long|short|exit|add)\b", re.I)

# Words that may sit between the side and the ticker: "#Exit Long MU",
# "#Exit partial NVDA", "#Long sold DRAM 40p", "#exit the rest of ASTS".
# Direction/partial words match any case; the plain fillers are matched
# lowercase-only so an all-caps ticker (e.g. ALL) is never eaten.
_FILLER_RES = (
    re.compile(r"^\s+(?:[Ll]ong|[Ss]hort|[Pp]artial)\b"),
    re.compile(r"^\s+(?:the|rest|of|my|all|remaining|last|sold|bought|dca)\b"),
    # Size / qualifier words that can precede the ticker ("#Add Small add Long
    # DRAM"). Matched in lower/Title case only so an all-caps ticker (e.g. the
    # real ticker "A") is never eaten.
    re.compile(r"^\s+(?:[Ss]mall|[Ll]arge|[Bb]ig|[Tt]iny|[Ss]tarter"
               r"|[Mm]ore|[Aa]dd|[Ss]ome|[Ss]izing)\b"),
)
TICKER_RE = re.compile(r"^\s+\$?(?P<ticker>[A-Z][A-Za-z.]{0,6})\b")

# A price directly after the ticker: "#Long NOW $109", "#Exit CRWD at 187.60",
# "#Long AAPL @175.50", "#Exit IBM short $116.40", "#Exit BTC 51,000".
# The trailing lookahead rejects option strikes ("43p"), percentages ("100%")
# and fractions ("3/4 out"), which are position notes rather than prices.
_PRICE_BODY = r"(?P<price>\d+(?:,\d{3})*(?:\.\d+)?)(?![\dA-Za-z%/])"
PRICE_RE = re.compile(
    r"^\s+(?:(?:[Aa]t|[Ll]ong|[Ss]hort)\s+|@\s*)?\$?" + _PRICE_BODY
)
# Fallback for exits whose price appears later in the line:
# "#Exit MUU 3/4 out at $750", "#Exit Long ALAB starter at 303.53".
NOTES_PRICE_RE = re.compile(r"(?:\b[Aa]t\s+|@\s*)\$?" + _PRICE_BODY)

# "avg is 79.91", "new avg $195.05", "#Add Short TSLA 393.41 avg 394.82"
AVG_RE = re.compile(r"\bavg(?:\s+is)?:?\s*\$?(?P<avg>\d+(?:,\d{3})*(?:\.\d+)?)",
                    re.I)

# Options. The number a trader tracks on an option is the PREMIUM, not the
# stock price, so P&L follows the contract (long the premium unless it was
# sold/written), NOT the directional Long/Short tag: a bearish "Short IWM via
# 295p" is a LONG put and wins when the premium RISES. A strike token
# ("295p", "500c", "$300p") or an option word marks the trade; a leg ratio
# ("97/96", "75/80") or PCS/PDS/CDS/credit/debit marks a spread, whose
# debit-vs-credit direction is too ambiguous to price -- spreads are scored
# from the trader's stated result instead.
OPTION_STRIKE_RE = re.compile(r"\$?\d+(?:\.\d+)?(?P<pc>[pc])\b", re.I)
OPTIONISH_RE = re.compile(r"\bvia\b|\bputs?\b|\bcalls?\b|\bpremium\b", re.I)
# Spread markers: an explicit type, or a two-leg strike ratio. A ratio only
# counts when both legs are 2-4 digit strikes (or carry a p/c) so a date like
# "7/2" or "6/18" is not mistaken for legs.
SPREAD_KW_RE = re.compile(r"\b(?:pcs|pds|cds|ccs|spread|credit|debit)\b", re.I)
LEG_RATIO_RE = re.compile(r"\b\d+[pc]/\d+[pc]?\b|\b\d{2,4}/\d{2,4}\b", re.I)
PUT_RE = re.compile(r"\bputs?\b", re.I)
CALL_RE = re.compile(r"\bcalls?\b", re.I)
SOLD_RE = re.compile(r"\b(?:sold|wrote|writing)\b", re.I)
# Assignment converts an option into stock ("#Long MSFT +assigned puts new avg
# $398.55"), so the position is a stock long, not an option.
ASSIGNED_RE = re.compile(r"\bassign(?:ed|ment)?\b", re.I)
# A number that is really a contract expiry day ("21 AUG 26"), not a price.
MONTH_AFTER_RE = re.compile(
    r"^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
# Option entry premium: after the strike, introduced by for/at/@/$ (first such
# hit, so "at 5.8" wins over a later "stops at 2"/"target 13-15"), else bare.
_PREM_KW_RE = re.compile(r"(?:\bfor\b|\bat\b|@)\s*\$?" + _PRICE_BODY, re.I)
_PREM_DOLLAR_RE = re.compile(r"\$" + _PRICE_BODY)
_PREM_BARE_RE = re.compile(r"^\s*" + _PRICE_BODY)

# Partial-vs-full exit cues in the free text. "swinging/trailing the rest"
# means the rest stays ON (partial); a bare "the rest"/"remaining" means this
# exit closes what was left (full).
KEEP_REST_RE = re.compile(
    r"\b(?:swing\w*|trail\w*|hold\w*|keep\w*|rid\w*)\b(?:\s+\S+){0,3}\s+rest\b",
    re.I)
FINAL_WORD_RE = re.compile(r"\b(?:rest|remaining|all\s+out|everything|last)\b",
                           re.I)
PARTIAL_WORD_RE = re.compile(r"\b(?:half|partial\w*|trim\w*)\b", re.I)
FRACTION_RE = re.compile(r"\b([1-9])/([1-9])\b")
# Partial cues only count near the start of the notes ("half for 3.00 gain",
# "3/4 out at $750") -- later mentions are usually commentary, not sizing
# ("wanted half of earnings candle preserved", "TP on the 7/7 low").
PARTIAL_CUE_WINDOW = 20

# Outcome words used when an exit has no usable price.
SCRATCH_RE = re.compile(
    r"\bscratch\w*\b|\bbreak\s*even\b|\bbreakeven\b|\bb/e\b|\bflat\b", re.I)
LOSS_RE = re.compile(
    r"\bloss\b|\blost\b|\bstop(?:ped)?\s+out\b|\bhit\s+stop\b"
    r"|-\s*\d+(?:\.\d+)?\s*%", re.I)
WIN_RE = re.compile(
    r"\bprofit\w*\b|\bgain\w*\b|\bwin\b|\bwinner\b|\bgreen\b"
    r"|\+\s*\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?\s*(?:dollars?|cents?|bucks?)(?:\s+of\s+profit)?"
    r"\s+per\s+share", re.I)


def _to_float(text):
    return float(text.replace(",", ""))


def _contract_type(text, is_spread):
    """'put' / 'call' / 'spread' / None from an option trade's text."""
    if is_spread:
        return "spread"
    m = OPTION_STRIKE_RE.search(text)
    if m:
        return "put" if m.group("pc").lower() == "p" else "call"
    if PUT_RE.search(text):
        return "put"
    if CALL_RE.search(text):
        return "call"
    return None


def _option_premium(text):
    """Pull the premium out of an option entry's text (after the strike)."""
    for rx in (_PREM_KW_RE, _PREM_DOLLAR_RE, _PREM_BARE_RE):
        m = rx.search(text)
        if m:
            return _to_float(m.group("price"))
    return None


def _exit_is_partial(explicit_partial, notes):
    if explicit_partial:
        return True
    if KEEP_REST_RE.search(notes):
        return True
    if FINAL_WORD_RE.search(notes):
        return False
    head = notes[:PARTIAL_CUE_WINDOW]
    if PARTIAL_WORD_RE.search(head):
        return True
    # "3/4 out", "1/2 off" -- but not a date like "the 7/7 low".
    return any(int(num) < int(den) for num, den in FRACTION_RE.findall(head))


def parse_trade_line(line):
    """Parse one '#Long/Short/Exit/Add ...' tag into a dict, or None."""
    m = TAG_RE.search(line)
    if not m:
        return None
    return _parse_tag(line, m)


def _parse_tag(text, tag_match):
    """Parse a trade starting at a TAG_RE match; text after it is the body."""
    side = tag_match.group("side").capitalize()
    rest = text[tag_match.end():].split("\n", 1)[0]
    body = rest  # full body incl. words the filler loop will strip ("sold")

    partial = False
    while True:
        for filler in _FILLER_RES:
            fm = filler.match(rest)
            if fm:
                if "partial" in fm.group(0).lower():
                    partial = True
                rest = rest[fm.end():]
                break
        else:
            break

    tm = TICKER_RE.match(rest)
    if not tm:
        return None
    ticker = tm.group("ticker").upper()
    rest = rest[tm.end():]

    price = None
    pm = PRICE_RE.match(rest)
    if pm and MONTH_AFTER_RE.match(rest[pm.end():]):
        pm = None  # "21 AUG 26" -- that number is an expiry day, not a price
    if pm:
        price = _to_float(pm.group("price"))
        rest = rest[pm.end():]
    notes = rest.strip()

    # Options are only classified on entries; on an exit the flag is unused
    # (episodes inherit it from their entry) and a stray "40c gain" note would
    # trip false positives. A bare price right after the ticker means a stock
    # trade even if a strike is mentioned in passing ("#Long DRAM 60.84 took
    # assignment of my July 65p") -- options lead with a strike/date/"via", not
    # a plain price -- so only look for an option when none was matched there.
    is_option = is_spread = sold = False
    contract = None
    strike_m = None
    if side != "Exit" and pm is None and not ASSIGNED_RE.search(notes):
        strike_m = OPTION_STRIKE_RE.search(notes)
        is_spread = bool(SPREAD_KW_RE.search(notes)) or (
            bool(LEG_RATIO_RE.search(notes)) and strike_m is None)
        is_option = is_spread or bool(strike_m) or bool(OPTIONISH_RE.search(notes))
        if is_option:
            contract = _contract_type(notes, is_spread)
            # Scan the full body: "sold" may sit before the ticker
            # ("#Long sold DRAM 40p"), where the filler loop strips it.
            sold = bool(SOLD_RE.search(body))

    if side == "Exit" and price is None:
        nm = NOTES_PRICE_RE.search(notes)
        if nm:
            price = _to_float(nm.group("price"))
    elif is_option and not is_spread and price is None:
        # Option entry premium (skip spreads: their credit/debit is ambiguous).
        after = notes[strike_m.end():] if strike_m else notes
        price = _option_premium(after)

    am = AVG_RE.search(notes)
    avg = _to_float(am.group("avg")) if am else None

    return {
        "side": side,                       # Long / Short / Exit / Add
        "partial": _exit_is_partial(partial, notes) if side == "Exit" else False,
        "ticker": ticker,
        "price": price,
        "avg": avg,
        "notes": notes,
        "option": is_option,
        "spread": is_spread,
        "sold": sold,
        "contract": contract,
    }


def parse_message(content):
    """Return a list of trade dicts parsed from one message's raw content.

    Each dict carries the poster's username plus the parsed trade fields. A
    message with multiple poster blocks (or several #tags in one block) yields
    multiple dicts.
    """
    content = content or ""
    trades = []
    posters = list(POSTER_RE.finditer(content))
    for i, pm in enumerate(posters):
        user = pm.group("user").strip()
        end = posters[i + 1].start() if i + 1 < len(posters) else len(content)
        block = content[pm.end():end]
        for tm in TAG_RE.finditer(block):
            trade = _parse_tag(block, tm)
            if trade:
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

    First run (empty log) or a parser upgrade: backfill INITIAL_LOOKBACK_DAYS
    (the merge dedups by id, and a smarter parser can recover messages that
    were previously skipped and therefore never stored). Otherwise: resume
    from the newest message already logged, so each weekly run only pulls what
    is new (gap-free even if a run is missed) while the running log preserves
    the full history of open positions.
    """
    if log.get("parser_version") != PARSER_VERSION:
        return snowflake_for(now - timedelta(days=INITIAL_LOOKBACK_DAYS))
    ids = [int(mid) for mid in log.get("messages", {})]
    if ids:
        return max(ids)
    return snowflake_for(now - timedelta(days=INITIAL_LOOKBACK_DAYS))


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
            t.setdefault("avg", None)
            t.setdefault("partial", False)
            t.setdefault("notes", "")
            t.setdefault("option", False)
            t.setdefault("spread", False)
            t.setdefault("sold", False)
            t.setdefault("contract", None)
            t["message_id"] = mid
            t["timestamp"] = entry.get("timestamp")
            t["index"] = i
            trades.append(t)
    trades.sort(key=lambda t: (int(t["message_id"]), t["index"]))
    return trades


# ---------------------------------------------------------------------------
# Episodes: entry (+ adds) -> partial exits -> final exit
# ---------------------------------------------------------------------------
def _new_episode(t):
    return {
        "user": t["user"],
        "ticker": t["ticker"],
        "side": t["side"],
        "entry_price": t["avg"] if t["avg"] is not None else t["price"],
        "adds": 0,
        "opened_ts": t["timestamp"],
        "message_id": t["message_id"],
        "exits": [],
        "closed_ts": None,
        "option": t.get("option", False),
        "spread": t.get("spread", False),
        "sold": t.get("sold", False),
        "contract": t.get("contract"),
    }


def _fold_add(ep, t):
    ep["adds"] += 1
    if t["avg"] is not None:
        ep["entry_price"] = t["avg"]
    elif ep["entry_price"] is None:
        ep["entry_price"] = t["price"]


def compute_episodes(trades):
    """Group chronological trades into per-position episodes.

    Returns (closed, open_map, orphan_exits):
      closed       -- list of finished episodes, in close order
      open_map     -- {(user, ticker): episode} for still-open positions
      orphan_exits -- exit trades with no matching open position
    """
    open_map = {}
    closed = []
    orphans = []
    for t in trades:
        key = (t["user"], t["ticker"])
        side = t["side"]
        ep = open_map.get(key)
        if side in ("Long", "Short"):
            if ep is None:
                open_map[key] = _new_episode(t)
            elif t["avg"] is not None or \
                    re.search(r"\b(?:add\w*|dca)\b", t["notes"], re.I):
                _fold_add(ep, t)  # "#Long NVDA add new avg $195.05"
            elif not ep["exits"]:
                # Correction / repost before any exit ("#Short IBM $118
                # (corrected earlier wrong entry)"): update in place, and adopt
                # the newer post's instrument so a stale option flag from an
                # earlier same-ticker trade can't mis-score a later stock entry.
                ep["side"] = side
                if t["price"] is not None:
                    ep["entry_price"] = t["price"]
                for k in ("option", "spread", "sold", "contract"):
                    ep[k] = t.get(k)
            else:
                # Re-entry while partially exited: assume the remainder was
                # closed off-log; finish the old episode and start fresh.
                ep["closed_ts"] = t["timestamp"]
                closed.append(ep)
                open_map[key] = _new_episode(t)
        elif side == "Add":
            if ep is None:
                open_map[key] = _new_episode(dict(t, side="Long"))
                open_map[key]["adds"] = 1
            else:
                _fold_add(ep, t)
        elif side == "Exit":
            if ep is None:
                orphans.append(t)
                continue
            ep["exits"].append({
                "price": t["price"],
                "notes": t["notes"],
                "partial": t["partial"],
                "timestamp": t["timestamp"],
            })
            if not t["partial"]:
                ep["closed_ts"] = t["timestamp"]
                closed.append(ep)
                del open_map[key]
    return closed, open_map, orphans


def compute_holdings(trades):
    """Return {user: [open episode dicts]} from chronologically ordered trades."""
    _closed, open_map, _orphans = compute_episodes(trades)
    holdings = {}
    for ep in open_map.values():
        holdings.setdefault(ep["user"], []).append(ep)
    return holdings


def classify_notes(text):
    """'win' / 'loss' / 'scratch' / None from an exit's free text."""
    if not text:
        return None
    if SCRATCH_RE.search(text):
        return "scratch"
    if LOSS_RE.search(text):
        return "loss"
    if WIN_RE.search(text):
        return "win"
    return None


def score_episode(ep):
    """Return (result, pct) for a finished episode.

    result is 'win' / 'loss' / 'scratch' / None (unscoreable); pct is the
    signed percent move from entry to the average exit price, or None when the
    call came from the exit notes instead of prices. Exit prices wildly out of
    proportion to the entry (mismatched stock-vs-premium legs, per-share P&L
    amounts, typos) are ignored rather than trusted.

    Direction depends on the instrument: a long stock or a bought option wins
    when the price/premium rises; a short stock or a sold/written option wins
    when it falls. Spreads (credit vs debit is ambiguous from the text) skip
    the price math and fall through to the trader's stated result.
    """
    entry = ep["entry_price"]
    option = ep.get("option") and not ep.get("spread")
    if entry and not ep.get("spread"):
        sane = [e["price"] for e in ep["exits"]
                if e["price"] is not None and 0.2 <= e["price"] / entry <= 5]
        if sane:
            avg_exit = sum(sane) / len(sane)
            pct = (avg_exit - entry) / entry * 100
            if ep.get("sold") if option else ep["side"] == "Short":
                pct = -pct  # falls to profit: short stock / written option
            if abs(pct) < 0.15:  # a hair off entry is a scratch, not a W/L
                return "scratch", pct
            return ("win" if pct > 0 else "loss"), pct
    text = " ".join(e["notes"] for e in ep["exits"])
    return classify_notes(text), None


def prune_log(log, holdings, now):
    """Drop messages older than RETENTION_DAYS, but always keep those that
    opened a position that is still held (so long swings survive)."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    keep_ids = {ep["message_id"] for eps in holdings.values() for ep in eps}
    store = log.get("messages", {})
    for mid in list(store.keys()):
        if mid in keep_ids:
            continue
        if parse_ts(store[mid]["timestamp"]) < cutoff:
            del store[mid]


# ---------------------------------------------------------------------------
# Summary building
# ---------------------------------------------------------------------------
_RESULT_ICONS = {"win": "✅", "loss": "❌", "scratch": "➖"}


def _fmt_date(dt):
    return f"{dt.strftime('%b')} {dt.day}"


def _fmt_price(price):
    return f"{price:g}" if price is not None else ""


def _plural(n, word):
    if n == 1:
        return f"1 {word}"
    return f"{n} {word}{'es' if word.endswith(('s', 'sh', 'ch', 'x')) else 's'}"


def _duration_label(ep):
    days = (parse_ts(ep["closed_ts"]).date()
            - parse_ts(ep["opened_ts"]).date()).days
    return "day trade" if days == 0 else f"swing {days}d"


def _option_label(ep):
    """A short contract tag ('puts', 'sold calls', 'spread') or None, so the
    tracked premium isn't mistaken for a stock price."""
    if not ep.get("option"):
        return None
    if ep.get("spread"):
        return "spread"
    contract = ep.get("contract") or "option"
    word = contract + "s" if contract in ("put", "call") else contract
    return f"sold {word}" if ep.get("sold") else word


def _closed_line(ep):
    """One line for a full round trip: entry -> partials -> final exit."""
    result, pct = score_episode(ep)
    icon = _RESULT_ICONS.get(result, "⚪")
    final = ep["exits"][-1] if ep["exits"] else None

    entry = _fmt_price(ep["entry_price"])
    entry_str = f" @ {entry}" if entry else ""
    exit_price = _fmt_price(final["price"]) if final else ""
    exit_str = f" @ {exit_price}" if exit_price else ""

    bits = []
    if pct is not None:
        bits.append(f"{pct:+.1f}%")
    elif result:
        bits.append(result)
    opt = _option_label(ep)
    if opt:
        bits.append(opt)
    partials = max(len(ep["exits"]) - 1, 0)
    if partials:
        bits.append(_plural(partials, "partial"))
    if ep["adds"]:
        bits.append(f"avg of {ep['adds'] + 1}")
    bits.append(_duration_label(ep))

    notes = (final["notes"] if final else "").strip()
    note_str = f" — {notes}" if notes else ""
    return (f"- {icon} **{ep['ticker']}** {ep['side']}{entry_str}"
            f" → Exit{exit_str} ({', '.join(bits)}){note_str}")


def _orphan_line(t):
    """An exit with no logged entry -- shown standalone rather than dropped."""
    result = classify_notes(t["notes"])
    icon = _RESULT_ICONS.get(result, "⚪")
    price = _fmt_price(t["price"])
    price_str = f" @ {price}" if price else ""
    note_str = f" — {t['notes']}" if t["notes"] else ""
    return f"- {icon} **{t['ticker']}** Exit{price_str}{note_str}"


def _open_line(ep):
    """One line describing a trader's still-open position."""
    icon = "\U0001F7E2" if ep["side"] == "Long" else "\U0001F535"
    price = _fmt_price(ep["entry_price"])
    price_str = f" @ {price}" if price else ""
    detail = [f"opened {_fmt_date(parse_ts(ep['opened_ts']))}"]
    opt = _option_label(ep)
    if opt:
        detail.append(opt)
    if ep["adds"]:
        detail.append(f"avg of {ep['adds'] + 1}")
    if ep["exits"]:
        detail.append(_plural(len(ep["exits"]), "partial") + " taken")
    return (f"- {icon} {ep['side']} **{ep['ticker']}**{price_str}"
            f" _({' · '.join(detail)})_")


def _win_rate_line(results):
    """'Quasi win rate' line from this week's episode results, or None."""
    wins = results.count("win")
    losses = results.count("loss")
    scratches = results.count("scratch")
    if wins + losses == 0:
        return None
    pct = round(100 * wins / (wins + losses))
    scratch_str = f", {_plural(scratches, 'scratch')}" if scratches else ""
    return (f"_Quasi win rate: **{pct}%** "
            f"({wins}W–{losses}L{scratch_str})_")


def build_summary(log, now):
    """Build a trader-by-trader summary (Markdown) from the running log.

    Per trader: round trips closed this week (one line each -- entry, partial
    count, final exit, result), then outstanding open positions. A position
    opened with Long/Short stays open until a full Exit -- partial exits do
    not close it -- so trades without an exit remain listed as open.
    """
    trades = log_to_trades(log)
    closed, open_map, orphans = compute_episodes(trades)
    week_start = now - timedelta(days=WEEK_DAYS)

    weekly = {}  # user -> [(sort_ts, line, result)]
    for ep in closed:
        if parse_ts(ep["closed_ts"]) >= week_start:
            result, _pct = score_episode(ep)
            weekly.setdefault(ep["user"], []).append(
                (ep["closed_ts"], _closed_line(ep), result))
    for t in orphans:
        if parse_ts(t["timestamp"]) >= week_start:
            weekly.setdefault(t["user"], []).append(
                (t["timestamp"], _orphan_line(t), classify_notes(t["notes"])))

    open_by_user = {}
    for ep in open_map.values():
        open_by_user.setdefault(ep["user"], []).append(ep)

    if week_start.month == now.month:
        label = f"{_fmt_date(week_start)}–{now.day}, {now.year}"
    else:
        label = f"{_fmt_date(week_start)}–{_fmt_date(now)}, {now.year}"

    lines = [f"# \U0001F4CA Weekly Trade Summary — {label}", ""]

    users = sorted(set(weekly) | set(open_by_user), key=str.lower)
    if not users:
        lines.append("_No trades this week and no open positions._")
        return "\n".join(lines)

    for user in users:
        lines.append(f"## {user}")

        rows = sorted(weekly.get(user, []), key=lambda r: r[0])
        wr_line = _win_rate_line([r[2] for r in rows])
        if wr_line:
            lines.append(wr_line)

        lines.append("**Closed this week**")
        if rows:
            lines.extend(r[1] for r in rows)
        else:
            lines.append("- _no trades closed this week_")

        lines.append("**Open trades**")
        open_eps = open_by_user.get(user, [])
        if open_eps:
            lines.extend(
                _open_line(ep)
                for ep in sorted(open_eps, key=lambda e: e["ticker"])
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
        reparse = log.get("parser_version") != PARSER_VERSION
        after = fetch_after(log, now)
        raw = fetch_messages(token, SOURCE_CHANNEL_ID, after)
        added = merge_messages(log, raw)
        log["parser_version"] = PARSER_VERSION
        mode = ("initial 30-day backfill" if first_run
                else "parser-upgrade re-backfill" if reparse
                else "weekly incremental")
        print(f"[{mode}] Fetched {len(raw)} messages; "
              f"{added} new trade message(s) logged.")

        holdings = compute_holdings(log_to_trades(log))
        prune_log(log, holdings, now)
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
