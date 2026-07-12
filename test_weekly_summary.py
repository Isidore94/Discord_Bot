#!/usr/bin/env python3
"""Unit tests for the YAGPDB trade-message parser in weekly_summary.py.

The sample messages below are real posts from the trade channel.
"""

import unittest
from datetime import datetime, timezone

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


class SummaryTests(unittest.TestCase):
    def test_build_summary_sections_and_chunking(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"messages": {
            # Long from 3 days ago, still open
            "100": {
                "timestamp": "2026-07-09T00:00:00+00:00",
                "trades": [{"user": "isidore94", "side": "Long",
                            "ticker": "PENG", "price": 77.15, "partial": False,
                            "notes": ""}],
            },
            # Full exit this week -> Closed this week
            "101": {
                "timestamp": "2026-07-11T00:00:00+00:00",
                "trades": [{"user": "00sav00", "side": "Exit",
                            "ticker": "CRWD", "price": 187.60, "partial": False,
                            "notes": ""}],
            },
        }}
        summary = ws.build_summary(log, now)
        self.assertIn("Closed this week", summary)
        self.assertIn("Still holding", summary)
        self.assertIn("CRWD", summary)      # closed
        self.assertIn("PENG", summary)      # still holding
        self.assertIn("isidore94", summary)

        for chunk in ws.chunk_message(summary):
            self.assertLessEqual(len(chunk), ws.CHUNK_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
