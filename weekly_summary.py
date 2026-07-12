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
TRADE_RE = re.compile(
    r"^#\s*"
    r"(?P<side>[Ll]ong|[Ss]hort|[Ee]xit)"          # side
    r"(?:\s+(?P<partial>[Pp]artial))?"             # optional "partial" (Exit)
    r"\s+\$?(?P<ticker>[A-Z][A-Z.]{0,6})"          # optional $, then TICKER
    r"(?:\s+(?:[Aa]t\s+|@\s*)?\$?(?P<price>\d+(?:\.\d+)?))?"  # optional price (at/@/$)
    r"(?P<notes>.*)$"                              # trailing free-text notes
)


def parse_trade_line(line):
    """Parse one '#Long/Short/Exit ...' line into a dict, or None if no match."""
    m = TRADE_RE.match(line.strip())
    if not m:
        return None
    price = m.group("price")
    return {
        "side": m.group("side").capitalize(),   # Long / Short / Exit
        "partial": bool(m.group("partial")),
        "ticker": m.group("ticker").upper(),
        "price": float(price) if price is not None else None,
        "notes": (m.group("notes") or "").strip(),
    }


def parse_message(content):
    """Return a list of trade dicts parsed from one message's raw content.

    Each dict carries the poster's username plus the parsed trade fields. A
    message with multiple poster/trade pairs yields multiple dicts.
    """
    trades = []
    for m in PAIR_RE.finditer(content or ""):
        trade = parse_trade_line(m.group("trade"))
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
            t["message_id"] = mid
            t["timestamp"] = entry.get("timestamp")
            t["index"] = i
            trades.append(t)
    trades.sort(key=lambda t: (int(t["message_id"]), t["index"]))
    return trades


def compute_holdings(trades):
    """Return {user: [open trade dicts]} from chronologically ordered trades.

    Long/Short opens (or refreshes) a position; a full Exit closes it; a partial
    Exit leaves the position open. Whatever is still open at the end is "held".
    """
    open_positions = {}  # (user, ticker) -> opening trade
    for t in trades:
        key = (t["user"], t["ticker"])
        if t["side"] in ("Long", "Short"):
            open_positions[key] = t
        elif t["side"] == "Exit" and not t["partial"]:
            open_positions.pop(key, None)
        # partial Exit: position stays open
    holdings = {}
    for (user, _ticker), t in open_positions.items():
        holdings.setdefault(user, []).append(t)
    return holdings


def prune_log(log, holdings, now):
    """Drop messages older than RETENTION_DAYS, but always keep those that
    opened a position that is still held (so long swings survive)."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    keep_ids = {t["message_id"] for trades in holdings.values() for t in trades}
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


def _weekly_line(t):
    """One line describing a trade a trader took this week."""
    price = _fmt_price(t["price"])
    price_str = f" @ {price}" if price else ""
    if t["side"] == "Exit":
        icon = "🟡" if t["partial"] else "✅"
        verb = "Partial exit" if t["partial"] else "Exit"
        notes = f" — {t['notes']}" if t["notes"] else ""
        return f"- {icon} {verb} **{t['ticker']}**{price_str}{notes}"
    icon = "🟢" if t["side"] == "Long" else "🔵"
    return f"- {icon} {t['side']} **{t['ticker']}**{price_str}"


def _open_line(t):
    """One line describing a trader's still-open position."""
    price = _fmt_price(t["price"])
    price_str = f" @ {price}" if price else ""
    opened = parse_ts(t["timestamp"])
    return f"- {t['side']} **{t['ticker']}**{price_str} _(opened {_fmt_date(opened)})_"


def build_summary(log, now):
    """Build a trader-by-trader summary (Markdown) from the running log.

    Per trader: trades taken this week, and outstanding open trades. A position
    opened with Long/Short stays open until a full Exit -- a partial Exit does
    not close it -- so trades without an exit remain listed as open.
    """
    trades = log_to_trades(log)
    holdings = compute_holdings(trades)

    week_start = now - timedelta(days=WEEK_DAYS)
    weekly_by_user = {}
    for t in trades:
        if parse_ts(t["timestamp"]) >= week_start:
            weekly_by_user.setdefault(t["user"], []).append(t)

    if week_start.month == now.month:
        label = f"{_fmt_date(week_start)}–{now.day}, {now.year}"
    else:
        label = f"{_fmt_date(week_start)}–{_fmt_date(now)}, {now.year}"

    lines = [f"# \U0001F4CA Weekly Trade Summary — {label}", ""]

    users = sorted(set(weekly_by_user) | set(holdings), key=str.lower)
    if not users:
        lines.append("_No trades this week and no open positions._")
        return "\n".join(lines)

    for user in users:
        lines.append(f"## {user}")

        lines.append("**Trades taken this week**")
        week_trades = weekly_by_user.get(user, [])
        if week_trades:
            lines.extend(_weekly_line(t) for t in week_trades)
        else:
            lines.append("- _no new trades this week_")

        lines.append("**Open trades**")
        open_trades = holdings.get(user, [])
        if open_trades:
            lines.extend(
                _open_line(t) for t in sorted(open_trades, key=lambda x: x["ticker"])
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
