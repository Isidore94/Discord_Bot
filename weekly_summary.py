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
    r"(?:\s+(?:[Aa]t\s+|@\s*)?\$?(?P<price>\d+(?:\.\d+)?)(?!\s*%))?"  # price, not N%
    r"(?P<notes>.*)$"                              # trailing free-text notes
)

# The channel's add function posts the running average itself:
#   "#add Long ALAB at 429.54. New avg: 449.76" -- that value is authoritative
# (it knows the sizes), so it replaces the tracked entry price outright.
NEW_AVG_RE = re.compile(r"(?i)new\s+avg[.:]?\s*\$?(\d+(?:\.\d+)?)")

# Free-text hints that an exit was partial, e.g. "partial profit swinging the
# rest", "#Exit half of RIVN", "leaving quarter size on", "#Exit this add on
# Long ALAB", "trimming here". Checked against the whole line.
PARTIAL_HINT_RE = re.compile(
    r"(?i)\b(?:partial\w*|half|trim\w*|leav\w*|keep\w*|swing\w*|still|"
    r"quarter|(?:this|that|the|my)\s+add)\b"
)

# A stated percentage return, e.g. "for 50%", "for -32%". Used to score an
# exit when entry/exit prices can't be compared.
STATED_PCT_RE = re.compile(r"(?i)\bfor\s+(?:a\s+)?([+-]?\d+(?:\.\d+)?)\s*%")


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
    if opt:
        opt["instrument"] = "option"
        opt["price"] = opt["premium"]  # so existing price-aware helpers still work
        trade = opt
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
    """True if a trade dict describes an option position."""
    return t.get("instrument") == "option" or bool(t.get("expiration"))


def _pos_key(t):
    """Identity of the position a trade acts on.

    Stocks key on (user, ticker) as before. Options key on the full contract so
    an option never clobbers -- nor is clobbered by -- a same-ticker stock
    position or a different contract on the same underlying.
    """
    if _is_option(t):
        return (t["user"], t["ticker"], "option",
                t.get("opt_type"), t.get("strike"), t.get("expiration"))
    return (t["user"], t["ticker"])


def _fallback_option_key(open_keys, t):
    """Key of the option a plain-stock Exit/Add most plausibly acts on.

    Traders often close (or add to) an option with an informal '#Exit TICKER
    ...' line (real example: '#Exit NVDA 11.70 for -32% on calls'). When such
    a line matches no open stock position but the trader has exactly ONE open
    option contract on that ticker, treat it as acting on that contract.
    Ambiguous (two+ contracts) -> None, and the line is ignored as before.
    """
    cands = [k for k in open_keys
             if len(k) > 2 and k[0] == t["user"] and k[1] == t["ticker"]]
    return cands[0] if len(cands) == 1 else None


def _tranche_pct(side, entry_price, t):
    """Signed fractional return of one exit tranche.

    Computed from entry vs exit price when both are known (inverted for
    shorts); otherwise the trader's own stated return ("for 50%") is trusted
    as-is. None when neither is available.
    """
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


def compute_holdings(trades):
    """Return {user: [open trade dicts]} from chronologically ordered trades.

    Long/Short opens (or refreshes) a position. An Add (the channel's add
    function) scales in: the posted "New avg" replaces the entry price when
    present (it accounts for real sizes); otherwise the add price is averaged
    in equal-weight; the original open date is kept. A full Exit closes the
    position; a partial Exit leaves it open but is tallied on it (count and
    per-tranche returns) so the eventual close can score the whole position.
    Exits/Adds that match no stock position fall back to the trader's single
    open option on that ticker (see _fallback_option_key).
    """
    open_positions = {}  # position key -> opening trade
    for t in trades:
        key = _pos_key(t)
        if t["side"] in ("Long", "Short"):
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
            open_positions[key] = pos
        elif t["side"] == "Exit":
            if key not in open_positions and not _is_option(t):
                key = _fallback_option_key(open_positions, t) or key
            if t["partial"]:
                pos = open_positions.get(key)
                if pos is not None:
                    pos["partials"] = pos.get("partials", 0) + 1
                    pct = _tranche_pct(pos["side"], pos.get("price"), t)
                    if pct is not None:
                        pos.setdefault("partial_pcts", []).append(pct)
            else:
                open_positions.pop(key, None)
    holdings = {}
    for t in open_positions.values():
        holdings.setdefault(t["user"], []).append(t)
    return holdings


def _blank_stats():
    return {"wins": 0, "losses": 0, "pct_sum": 0.0, "pct_n": 0,
            "week_wins": 0, "week_losses": 0}


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
            entries[key] = {"side": t["side"], "price": t["price"],
                            "timestamp": t.get("timestamp"), "adds": 1,
                            "partials": 0, "partial_pcts": []}
        elif t["side"] == "Add":
            if key not in entries and not _is_option(t):
                key = _fallback_option_key(entries, t) or key
            info = entries.get(key)
            if info is None:
                price = t.get("new_avg", t.get("price"))
                entries[key] = {"side": "Long", "price": price,
                                "timestamp": t.get("timestamp"), "adds": 1,
                                "partials": 0, "partial_pcts": []}
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
                continue
            pct = _tranche_pct(info["side"], info["price"], t)
            if pct is not None:
                t["pct"] = pct
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
            entries.pop(key, None)
    return stats


def resolve_expired_options(holdings, now, spot_close=options.spot_close_on,
                            cache=None):
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
            outcome = options.resolve_option(
                t["side"], t["opt_type"], t["strike"], t.get("premium"), spot
            )
            # Earlier partial exits blend with the settlement as equal-size
            # tranches, the same way a regular close combines with partials.
            partial_pcts = t.get("partial_pcts") or []
            if partial_pcts and outcome.get("pct") is not None:
                tranches = partial_pcts + [outcome["pct"]]
                combined = sum(tranches) / len(tranches)
                outcome["pct"] = combined
                outcome["win"] = combined > 0
                n = t.get("partials", len(partial_pcts))
                outcome["summary"] += (f" — net {combined * 100:+.1f}% incl. "
                                       f"{n} earlier partial{'s' if n != 1 else ''}")
            resolutions.setdefault(user, []).append(
                {"trade": t, "spot": spot, "exp_date": exp_date, "outcome": outcome}
            )
            # An assigned short put becomes a long stock position at the basis.
            if (outcome["status"] == "assigned" and t["side"] == "Short"
                    and t["opt_type"] == options.PUT):
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
                        oc["win"] = eff > entry
                        oc["pnl"] = eff - entry
                        oc["pct"] = (eff - entry) / entry
                        oc["summary"] += (f" — shares {oc['pct'] * 100:+.1f}% "
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
    keep_ids = {t["message_id"] for trades in holdings.values() for t in trades}
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


def _score_suffix(t):
    """' (+3.4%, 2 partials, held 12d)' for a scored exit, '' if unknown.

    On a closing exit the pct is the whole position's equal-tranche average
    and the partials count says how many partial exits fed into it.
    """
    bits = []
    if t.get("pct") is not None:
        bits.append(f"{t['pct'] * 100:+.1f}%")
    n = t.get("partials")
    if n:
        bits.append(f"{n} partial{'s' if n != 1 else ''}")
    if t.get("held_days") is not None:
        bits.append(f"held {t['held_days']}d")
    return f" ({', '.join(bits)})" if bits else ""


def _weekly_line(t):
    """One line describing a trade a trader took this week."""
    if _is_option(t):
        if t["side"] == "Exit":
            icon = "🟡" if t["partial"] else "✅"
            verb = "Partial exit" if t["partial"] else "Exit"
            notes = f" — {t['notes']}" if t.get("notes") else ""
            return (f"- {icon} {options.format_option_open(t, verb=verb)}"
                    f"{_score_suffix(t)}{notes}")
        if t["side"] == "Add":
            return f"- ➕ {options.format_option_open(t)}"
        icon = "🟢" if t["side"] == "Long" else "🔵"
        return f"- {icon} {options.format_option_open(t)}"
    price = _fmt_price(t["price"])
    price_str = f" @ {price}" if price else ""
    if t["side"] == "Exit":
        icon = "🟡" if t["partial"] else "✅"
        verb = "Partial exit" if t["partial"] else "Exit"
        notes = f" — {t['notes']}" if t["notes"] else ""
        return f"- {icon} {verb} **{t['ticker']}**{price_str}{_score_suffix(t)}{notes}"
    if t["side"] == "Add":
        avg = f" → avg {t['new_avg']:g}" if t.get("new_avg") is not None else ""
        return f"- ➕ Add **{t['ticker']}**{price_str}{avg}"
    icon = "🟢" if t["side"] == "Long" else "🔵"
    return f"- {icon} {t['side']} **{t['ticker']}**{price_str}"


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
        return f"- {options.format_option_open(t)}{tags}{opened}"
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
    return f"- {t['side']} **{t['ticker']}**{price_str}{tags}{mtm}{opened}"


def _win_rate_line(stats):
    """'Quasi win rate' line for a trader, or None if nothing was scoreable."""
    if not stats:
        return None
    wins, losses = stats.get("wins", 0), stats.get("losses", 0)
    total = wins + losses
    if total == 0:
        return None
    pct = round(100 * wins / total)
    parts = [f"Quasi win rate: **{pct}%** ({wins}W–{losses}L, {total} scored)"]
    if stats.get("pct_n"):
        avg = stats["pct_sum"] / stats["pct_n"] * 100
        parts.append(f"avg {avg:+.1f}%/trade")
    week_w, week_l = stats.get("week_wins", 0), stats.get("week_losses", 0)
    if week_w + week_l:
        parts.append(f"this week {week_w}W–{week_l}L")
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
    holdings = compute_holdings(trades)
    win_rates = compute_win_rates(trades, week_start=week_start)
    resolutions = resolve_expired_options(
        holdings, now, spot_close, cache=log.setdefault("spot_cache", {})
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
                if this_week:
                    s["week_wins" if oc["win"] else "week_losses"] += 1
                if oc.get("pct") is not None:
                    s["pct_sum"] += oc["pct"]
                    s["pct_n"] += 1
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

        lines.append("**Trades taken this week**")
        week_trades = weekly_by_user.get(user, [])
        if week_trades:
            lines.extend(_weekly_line(t) for t in week_trades)
        else:
            lines.append("- _no new trades this week_")

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

    # Settlement inside build_summary may add to the log's spot cache, so the
    # log is saved after the summary is built (live runs only).
    summary = build_summary(log, now, last_close=options.last_closes)
    if not dry_run:
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
