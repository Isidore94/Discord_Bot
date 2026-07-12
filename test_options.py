#!/usr/bin/env python3
"""Unit tests for options.py: parsing, the expiration resolution matrix, and
the failure-tolerant spot lookup. No network is touched -- the resolution
matrix is a pure function and the spot lookup is only exercised for the
future/no-fetch path.
"""

import unittest
from datetime import date

import options as o


class ParseOptionTests(unittest.TestCase):
    REF = date(2026, 7, 12)

    def test_short_put_word_iso_date_at_premium(self):
        t = o.parse_option("#Short put $SPY 400 2026-07-18 @ 3.20", self.REF)
        self.assertEqual(t["side"], "Short")
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["ticker"], "SPY")
        self.assertEqual(t["strike"], 400.0)
        self.assertEqual(t["expiration"], "2026-07-18")
        self.assertEqual(t["premium"], 3.20)

    def test_long_call_suffix_us_date_for_premium(self):
        t = o.parse_option("#Long call NVDA 500c 8/15 for 12.00", self.REF)
        self.assertEqual(t["side"], "Long")
        self.assertEqual(t["opt_type"], o.CALL)
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["strike"], 500.0)
        self.assertEqual(t["expiration"], "2026-08-15")
        self.assertEqual(t["premium"], 12.0)

    def test_long_put_suffix_and_exp_label_full_date(self):
        t = o.parse_option("#Long put AAPL 175p exp 8/15/2026 5.50", self.REF)
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["strike"], 175.0)
        self.assertEqual(t["expiration"], "2026-08-15")
        self.assertEqual(t["premium"], 5.50)

    def test_two_digit_year(self):
        t = o.parse_option("#Short call TSLA 250c 7/18/26 4.00", self.REF)
        self.assertEqual(t["expiration"], "2026-07-18")

    def test_premium_optional(self):
        t = o.parse_option("#Short put SPY 400p 12/19", self.REF)
        self.assertIsNone(t["premium"])
        self.assertEqual(t["expiration"], "2026-12-19")

    def test_year_inference_rolls_forward(self):
        # An expiry month/day already past in the reference year -> next year.
        t = o.parse_option("#Short put SPY 400p 1/16", date(2026, 12, 1))
        self.assertEqual(t["expiration"], "2027-01-16")

    def test_plain_stock_line_is_not_an_option(self):
        self.assertIsNone(o.parse_option("#Long $PENG 77.15", self.REF))

    def test_exit_line_is_not_an_option(self):
        self.assertIsNone(o.parse_option("#Exit CRWD at 187.60", self.REF))

    def test_missing_type_marker_is_not_an_option(self):
        # Strike + date but no call/put word or c/p suffix -> ambiguous, not ours.
        self.assertIsNone(o.parse_option("#Long AAPL 175 8/15", self.REF))

    def test_notes_preserved(self):
        t = o.parse_option("#Long call NVDA 500c 8/15 @ 12.00 swing idea", self.REF)
        self.assertEqual(t["notes"], "swing idea")


class ParseExpTests(unittest.TestCase):
    def test_invalid_date_returns_none(self):
        self.assertIsNone(o._parse_exp("13/40", date(2026, 1, 1)))

    def test_iso_and_us_equivalent(self):
        self.assertEqual(o._parse_exp("2026-07-18"), date(2026, 7, 18))
        self.assertEqual(o._parse_exp("7/18/2026"), date(2026, 7, 18))


class IsItmTests(unittest.TestCase):
    def test_call_itm_above_strike(self):
        self.assertTrue(o.is_itm(o.CALL, 100, 101))
        self.assertFalse(o.is_itm(o.CALL, 100, 100))   # ATM -> OTM
        self.assertFalse(o.is_itm(o.CALL, 100, 99))

    def test_put_itm_below_strike(self):
        self.assertTrue(o.is_itm(o.PUT, 100, 99))
        self.assertFalse(o.is_itm(o.PUT, 100, 100))    # ATM -> OTM
        self.assertFalse(o.is_itm(o.PUT, 100, 101))


class ResolveMatrixTests(unittest.TestCase):
    """The four single-leg cases at expiration, each ITM and OTM."""

    # --- Short put: the user's headline "theta trade trying to expire" ---
    def test_short_put_above_strike_is_worthless_win(self):
        r = o.resolve_option("Short", "put", 400, 3.20, 405)
        self.assertEqual(r["status"], "expired_worthless")
        self.assertTrue(r["win"])
        self.assertAlmostEqual(r["pnl"], 3.20)

    def test_short_put_at_strike_is_worthless_win(self):
        # Pinned exactly at the strike -> treated as expiring worthless.
        r = o.resolve_option("Short", "put", 400, 3.20, 400)
        self.assertEqual(r["status"], "expired_worthless")
        self.assertTrue(r["win"])

    def test_short_put_below_strike_is_assigned_at_strike_minus_premium(self):
        r = o.resolve_option("Short", "put", 400, 3.20, 390)
        self.assertEqual(r["status"], "assigned")
        self.assertIsNone(r["win"])          # becomes a share position, not scored
        self.assertAlmostEqual(r["basis"], 396.80)  # strike - premium
        self.assertIn("assigned", r["summary"])

    # --- Long call ---
    def test_long_call_itm_scores_on_intrinsic_vs_premium(self):
        win = o.resolve_option("Long", "call", 500, 12.0, 530)
        self.assertEqual(win["status"], "exercised")
        self.assertTrue(win["win"])
        self.assertAlmostEqual(win["pnl"], 18.0)   # (530-500) - 12
        loss = o.resolve_option("Long", "call", 500, 40.0, 530)
        self.assertFalse(loss["win"])              # intrinsic 30 < 40 premium

    def test_long_call_otm_is_worthless_loss(self):
        r = o.resolve_option("Long", "call", 500, 12.0, 480)
        self.assertEqual(r["status"], "expired_worthless")
        self.assertFalse(r["win"])
        self.assertAlmostEqual(r["pnl"], -12.0)

    # --- Short call ---
    def test_short_call_otm_is_worthless_win(self):
        r = o.resolve_option("Short", "call", 250, 4.0, 240)
        self.assertEqual(r["status"], "expired_worthless")
        self.assertTrue(r["win"])
        self.assertAlmostEqual(r["pnl"], 4.0)

    def test_short_call_itm_is_assigned_called_away(self):
        r = o.resolve_option("Short", "call", 250, 4.0, 270)
        self.assertEqual(r["status"], "assigned")
        self.assertIsNone(r["win"])
        self.assertAlmostEqual(r["basis"], 254.0)  # strike + premium
        self.assertIn("called away", r["summary"])

    # --- Long put ---
    def test_long_put_itm_scores_on_intrinsic_vs_premium(self):
        r = o.resolve_option("Long", "put", 175, 5.5, 160)
        self.assertEqual(r["status"], "exercised")
        self.assertTrue(r["win"])
        self.assertAlmostEqual(r["pnl"], 9.5)      # (175-160) - 5.5

    def test_long_put_otm_is_worthless_loss(self):
        r = o.resolve_option("Long", "put", 175, 5.5, 180)
        self.assertEqual(r["status"], "expired_worthless")
        self.assertFalse(r["win"])

    # --- Missing premium is tolerated ---
    def test_missing_premium_still_classifies(self):
        worthless = o.resolve_option("Short", "put", 400, None, 405)
        self.assertEqual(worthless["status"], "expired_worthless")
        self.assertTrue(worthless["win"])
        self.assertIsNone(worthless["pnl"])

        assigned = o.resolve_option("Short", "put", 400, None, 390)
        self.assertEqual(assigned["status"], "assigned")
        self.assertEqual(assigned["basis"], 400)   # no premium to subtract


class SpotCloseTests(unittest.TestCase):
    def test_future_expiration_returns_none_without_fetching(self):
        # today < exp_date -> short-circuits before importing yfinance.
        self.assertIsNone(
            o.spot_close_on("AAPL", date(2026, 8, 15), today=date(2026, 7, 12))
        )


class FormatTests(unittest.TestCase):
    def test_open_line_shows_contract_and_premium(self):
        t = o.parse_option("#Short put SPY 400p 2026-07-18 @ 3.20", date(2026, 7, 1))
        line = o.format_option_open(t)
        self.assertIn("Short put", line)
        self.assertIn("SPY $400p exp 2026-07-18", line)
        self.assertIn("$3.20", line)

    def test_resolution_line_marks_win_and_loss(self):
        t = o.parse_option("#Short put SPY 400p 2026-07-18 @ 3.20", date(2026, 7, 1))
        win = o.format_option_resolution(t, o.resolve_option("Short", "put", 400, 3.2, 405), 405)
        self.assertIn("✅", win)
        self.assertIn("spot $405.00", win)
        loss = o.format_option_resolution(
            t, o.resolve_option("Long", "put", 400, 3.2, 405), 405)
        self.assertIn("❌", loss)


if __name__ == "__main__":
    unittest.main(verbosity=2)
