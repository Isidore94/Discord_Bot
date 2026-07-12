#!/usr/bin/env python3
"""Unit tests for the YAGPDB trade-message parser in weekly_summary.py.

The sample messages below are real posts from the trade channel.
"""

import unittest
from datetime import datetime, timedelta, timezone

import weekly_summary as ws


class ParseTradeLineTests(unittest.TestCase):
    def test_long_with_dollar_ticker(self):
        t = ws.parse_trade_line("#Long $PENG 77.15")
        self.assertEqual(t["side"], "Long")
        self.assertFalse(t["partial"])
        self.assertEqual(t["ticker"], "PENG")
        self.assertEqual(t["price"], 77.15)
        self.assertEqual(t["notes"], "")

    def test_exit_with_trailing_notes(self):
        t = ws.parse_trade_line("#Exit NVDA 11.70 for -32% on calls")
        self.assertEqual(t["side"], "Exit")
        self.assertFalse(t["partial"])
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["price"], 11.70)
        self.assertEqual(t["notes"], "for -32% on calls")

    def test_long_plain_ticker(self):
        t = ws.parse_trade_line("#Long FBIN 52.13")
        self.assertEqual(t["side"], "Long")
        self.assertEqual(t["ticker"], "FBIN")
        self.assertEqual(t["price"], 52.13)

    def test_exit_with_at_symbol(self):
        t = ws.parse_trade_line("#Exit GOOGL @ 368.88")
        self.assertEqual(t["side"], "Exit")
        self.assertEqual(t["ticker"], "GOOGL")
        self.assertEqual(t["price"], 368.88)
        self.assertEqual(t["notes"], "")

    def test_long_with_at_symbol_no_space(self):
        t = ws.parse_trade_line("#Long AAPL @175.50")
        self.assertEqual(t["ticker"], "AAPL")
        self.assertEqual(t["price"], 175.50)

    def test_exit_with_at_keyword(self):
        t = ws.parse_trade_line("#Exit CRWD at 187.60")
        self.assertEqual(t["side"], "Exit")
        self.assertFalse(t["partial"])
        self.assertEqual(t["ticker"], "CRWD")
        self.assertEqual(t["price"], 187.60)
        self.assertEqual(t["notes"], "")

    def test_partial_exit_with_dollar_price_and_notes(self):
        t = ws.parse_trade_line(
            "#Exit partial NVDA $208.66 for over $13 profit per share. "
            "(Still have over 4/5th position on)."
        )
        self.assertEqual(t["side"], "Exit")
        self.assertTrue(t["partial"])
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["price"], 208.66)
        self.assertEqual(
            t["notes"],
            "for over $13 profit per share. (Still have over 4/5th position on).",
        )

    def test_short_side(self):
        t = ws.parse_trade_line("#Short AAPL 190.00")
        self.assertEqual(t["side"], "Short")
        self.assertEqual(t["ticker"], "AAPL")

    def test_missing_price(self):
        t = ws.parse_trade_line("#Long TSLA still watching")
        self.assertEqual(t["ticker"], "TSLA")
        self.assertIsNone(t["price"])
        self.assertEqual(t["notes"], "still watching")

    def test_non_trade_line(self):
        self.assertIsNone(ws.parse_trade_line("just some chatter"))


class ParseMessageTests(unittest.TestCase):
    def test_single_long(self):
        trades = ws.parse_message("isidore94 posted a trade:\n#Long $PENG 77.15")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "isidore94")
        self.assertEqual(trades[0]["ticker"], "PENG")
        self.assertEqual(trades[0]["side"], "Long")
        self.assertEqual(trades[0]["price"], 77.15)

    def test_single_exit_with_notes(self):
        trades = ws.parse_message(
            "mallowmushroom posted a trade:\n#Exit NVDA 11.70 for -32% on calls"
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "mallowmushroom")
        self.assertEqual(trades[0]["side"], "Exit")
        self.assertEqual(trades[0]["ticker"], "NVDA")
        self.assertEqual(trades[0]["notes"], "for -32% on calls")

    def test_two_pairs_in_one_message(self):
        trades = ws.parse_message(
            "isidore94 posted a trade:\n#Long FBIN 52.13\n"
            "00sav00 posted a trade:\n#Exit CRWD at 187.60"
        )
        self.assertEqual(len(trades), 2)

        self.assertEqual(trades[0]["user"], "isidore94")
        self.assertEqual(trades[0]["side"], "Long")
        self.assertEqual(trades[0]["ticker"], "FBIN")
        self.assertEqual(trades[0]["price"], 52.13)

        self.assertEqual(trades[1]["user"], "00sav00")
        self.assertEqual(trades[1]["side"], "Exit")
        self.assertEqual(trades[1]["ticker"], "CRWD")
        self.assertEqual(trades[1]["price"], 187.60)

    def test_partial_exit_message(self):
        trades = ws.parse_message(
            "1ripley posted a trade:\n#Exit partial NVDA $208.66 for over $13 "
            "profit per share. (Still have over 4/5th position on)."
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "1ripley")
        self.assertTrue(trades[0]["partial"])
        self.assertEqual(trades[0]["ticker"], "NVDA")
        self.assertEqual(trades[0]["price"], 208.66)


class HoldingsTests(unittest.TestCase):
    """Position logic: Long/Short opens, full Exit closes, partial Exit keeps open."""

    def _trades(self, rows):
        # rows: (message_id, user, side, ticker, price, partial)
        out = []
        for i, (mid, user, side, ticker, price, partial) in enumerate(rows):
            out.append({
                "message_id": str(mid),
                "index": 0,
                "timestamp": "2026-07-10T00:00:00+00:00",
                "user": user,
                "side": side,
                "ticker": ticker,
                "price": price,
                "partial": partial,
                "notes": "",
            })
        return out

    def test_open_and_full_exit_closes(self):
        trades = self._trades([
            (1, "u", "Long", "PENG", 77.15, False),
            (2, "u", "Exit", "PENG", 90.0, False),
        ])
        holdings = ws.compute_holdings(trades)
        self.assertEqual(holdings, {})

    def test_partial_exit_keeps_position_open(self):
        trades = self._trades([
            (1, "u", "Long", "NVDA", 200.0, False),
            (2, "u", "Exit", "NVDA", 208.66, True),  # partial
        ])
        holdings = ws.compute_holdings(trades)
        self.assertIn("u", holdings)
        self.assertEqual(holdings["u"][0]["ticker"], "NVDA")

    def test_latest_action_wins(self):
        trades = self._trades([
            (1, "u", "Long", "FBIN", 52.13, False),
            (2, "u", "Exit", "FBIN", 60.0, False),   # closed
            (3, "u", "Long", "FBIN", 55.0, False),   # re-opened
        ])
        holdings = ws.compute_holdings(trades)
        self.assertIn("u", holdings)
        self.assertEqual(holdings["u"][0]["price"], 55.0)

    def test_holdings_grouped_per_user(self):
        trades = self._trades([
            (1, "a", "Long", "PENG", 77.15, False),
            (2, "b", "Short", "AAPL", 190.0, False),
        ])
        holdings = ws.compute_holdings(trades)
        self.assertEqual(set(holdings), {"a", "b"})


class WinRateTests(unittest.TestCase):
    def _t(self, rows):
        out = []
        for i, (mid, user, side, ticker, price, partial) in enumerate(rows):
            out.append({"message_id": str(mid), "index": 0,
                        "timestamp": "2026-07-10T00:00:00+00:00", "user": user,
                        "side": side, "ticker": ticker, "price": price,
                        "partial": partial, "notes": ""})
        return out

    def test_long_win_and_loss(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "AAA", 100.0, False),
            (2, "u", "Exit", "AAA", 110.0, False),   # win: exit above entry
            (3, "u", "Long", "BBB", 100.0, False),
            (4, "u", "Exit", "BBB", 90.0, False),    # loss: exit below entry
        ]))
        self.assertEqual(wr["u"], {"wins": 1, "losses": 1})

    def test_short_win_is_exit_below_entry(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Short", "CCC", 100.0, False),
            (2, "u", "Exit", "CCC", 90.0, False),    # win: short, exit below
            (3, "u", "Short", "DDD", 100.0, False),
            (4, "u", "Exit", "DDD", 105.0, False),   # loss: short, exit above
        ]))
        self.assertEqual(wr["u"], {"wins": 1, "losses": 1})

    def test_partial_exits_each_score_against_same_entry(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "NVDA", 200.0, False),
            (2, "u", "Exit", "NVDA", 197.5, True),   # partial loss
            (3, "u", "Exit", "NVDA", 208.66, True),  # partial win, still open
        ]))
        self.assertEqual(wr["u"], {"wins": 1, "losses": 1})

    def test_exit_without_price_is_ignored(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "EEE", 100.0, False),
            (2, "u", "Exit", "EEE", None, False),    # no price -> not scored
        ]))
        self.assertEqual(wr, {})

    def test_win_rate_line_formatting(self):
        self.assertIn("67%", ws._win_rate_line({"wins": 2, "losses": 1}))
        self.assertIsNone(ws._win_rate_line({"wins": 0, "losses": 0}))
        self.assertIsNone(ws._win_rate_line(None))


class SummaryTests(unittest.TestCase):
    def _sample_log(self):
        return {"messages": {
            # isidore94: opened PENG ~3 weeks ago, still open (long swing memory)
            "100": {
                "timestamp": "2026-06-20T00:00:00+00:00",
                "trades": [{"user": "isidore94", "side": "Long",
                            "ticker": "PENG", "price": 77.15, "partial": False,
                            "notes": ""}],
            },
            # isidore94: opened FBIN this week, still open
            "101": {
                "timestamp": "2026-07-09T00:00:00+00:00",
                "trades": [{"user": "isidore94", "side": "Long",
                            "ticker": "FBIN", "price": 52.13, "partial": False,
                            "notes": ""}],
            },
            # 00sav00: full exit this week -> weekly activity, no open trades
            "102": {
                "timestamp": "2026-07-11T00:00:00+00:00",
                "trades": [{"user": "00sav00", "side": "Exit",
                            "ticker": "CRWD", "price": 187.60, "partial": False,
                            "notes": ""}],
            },
            # 1ripley: opened NVDA this week then partially exited -> stays open
            "103": {
                "timestamp": "2026-07-10T00:00:00+00:00",
                "trades": [{"user": "1ripley", "side": "Long",
                            "ticker": "NVDA", "price": 200.0, "partial": False,
                            "notes": ""}],
            },
            "104": {
                "timestamp": "2026-07-11T12:00:00+00:00",
                "trades": [{"user": "1ripley", "side": "Exit",
                            "ticker": "NVDA", "price": 208.66, "partial": True,
                            "notes": "still holding 4/5"}],
            },
        }}

    def test_trader_by_trader_structure(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)

        # Every active trader gets their own section with both headings.
        for user in ("isidore94", "00sav00", "1ripley"):
            self.assertIn(f"## {user}", summary)
        self.assertEqual(summary.count("**Trades taken this week**"), 3)
        self.assertEqual(summary.count("**Open trades**"), 3)

    def test_open_positions_persist_and_partial_stays_open(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        iso = summary.split("## isidore94")[1].split("##")[0]
        # Long swing opened 3 weeks ago is still listed as open.
        self.assertIn("PENG", iso)
        self.assertIn("FBIN", iso)

        rip = summary.split("## 1ripley")[1].split("##")[0]
        # Partial exit does NOT close the position -> NVDA still open.
        self.assertIn("Open trades", rip)
        self.assertIn("NVDA", rip.split("**Open trades**")[1])

    def test_trader_with_only_a_close_has_no_open_trades(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        sav = summary.split("## 00sav00")[1].split("##")[0]
        self.assertIn("Exit **CRWD**", sav)
        self.assertIn("_none_", sav.split("**Open trades**")[1])

    def test_chunking_stays_under_limit(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        for chunk in ws.chunk_message(summary):
            self.assertLessEqual(len(chunk), ws.CHUNK_LIMIT)


class ContentLogTests(unittest.TestCase):
    def test_content_entries_are_reparsed(self):
        # Log stores raw content -> current parser (incl. @-price) is applied.
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"messages": {
            "500": {
                "timestamp": "2026-07-10T00:00:00+00:00",
                "content": "00sav00 posted a trade:\n#Exit GOOGL @ 368.88",
            },
        }}
        trades = ws.log_to_trades(log)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "GOOGL")
        self.assertEqual(trades[0]["price"], 368.88)
        self.assertEqual(trades[0]["notes"], "")

    def test_legacy_trades_entries_still_supported(self):
        log = {"messages": {
            "600": {
                "timestamp": "2026-07-10T00:00:00+00:00",
                "trades": [{"user": "u", "side": "Long", "ticker": "PENG",
                            "price": 77.15, "partial": False, "notes": ""}],
            },
        }}
        trades = ws.log_to_trades(log)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "PENG")


class FetchWindowTests(unittest.TestCase):
    def test_first_run_backfills_initial_window(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        after = ws.fetch_after({"messages": {}}, now)
        expected = ws.snowflake_for(now - timedelta(days=ws.INITIAL_LOOKBACK_DAYS))
        self.assertEqual(after, expected)

    def test_subsequent_run_resumes_from_newest_logged_id(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"messages": {
            "100": {"timestamp": "2026-07-01T00:00:00+00:00", "trades": []},
            "250": {"timestamp": "2026-07-08T00:00:00+00:00", "trades": []},
            "175": {"timestamp": "2026-07-05T00:00:00+00:00", "trades": []},
        }}
        self.assertEqual(ws.fetch_after(log, now), 250)


if __name__ == "__main__":
    unittest.main(verbosity=2)
