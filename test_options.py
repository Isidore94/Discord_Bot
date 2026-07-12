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

    def test_exit_option_line(self):
        t = o.parse_option("#Exit NVDA 500c 8/15 @ 15.00", self.REF)
        self.assertEqual(t["side"], "Exit")
        self.assertFalse(t["partial"])
        self.assertEqual(t["opt_type"], o.CALL)
        self.assertEqual(t["strike"], 500.0)
        self.assertEqual(t["expiration"], "2026-08-15")
        self.assertEqual(t["premium"], 15.0)

    def test_exit_partial_option_line(self):
        t = o.parse_option("#Exit partial put SPY 400 2026-07-18 @ 1.10", self.REF)
        self.assertEqual(t["side"], "Exit")
        self.assertTrue(t["partial"])
        self.assertEqual(t["opt_type"], o.PUT)

    def test_informal_stock_exit_is_not_an_option(self):
        # No expiration date -> stock-style exit even if it mentions calls.
        self.assertIsNone(
            o.parse_option("#Exit NVDA 11.70 for -32% on calls", self.REF))

    def test_date_first_form_with_space_before_suffix(self):
        # Real channel message: date BEFORE strike, space before the P/C
        # suffix. Previously fell through to the stock parser and read the
        # date's leading digits ("07") as a bogus $7 share price.
        t = o.parse_option("#Long BE 07/10/2026 235.00 P 4.9", self.REF)
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["ticker"], "BE")
        self.assertEqual(t["strike"], 235.0)
        self.assertEqual(t["expiration"], "2026-07-10")
        self.assertEqual(t["premium"], 4.9)

    def test_date_first_form_no_space_before_suffix_with_notes(self):
        t = o.parse_option(
            "#Long DELL 07/17/2026 385.00p 3.35 this am", self.REF)
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["ticker"], "DELL")
        self.assertEqual(t["strike"], 385.0)
        self.assertEqual(t["premium"], 3.35)
        self.assertEqual(t["notes"], "this am")

    def test_date_first_form_with_filler_and_dollar_signs(self):
        t = o.parse_option("#long NBIS sold 7/2/26 $200p $2.4", self.REF)
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["ticker"], "NBIS")
        self.assertEqual(t["strike"], 200.0)
        self.assertEqual(t["expiration"], "2026-07-02")
        self.assertEqual(t["premium"], 2.4)

    def test_date_first_form_two_digit_year_no_decimal_strike(self):
        t = o.parse_option("#Long MRVL 6/26/26 250 P 1.6 earlier", self.REF)
        self.assertEqual(t["strike"], 250.0)
        self.assertEqual(t["expiration"], "2026-06-26")
        self.assertEqual(t["notes"], "earlier")

    def test_strike_first_form_with_slang_filler_before_date(self):
        # Real channel message: "lottos" (slang) sits between the strike and
        # the date; previously broke the strike-first regex entirely and let
        # 704 leak through as a fake $704 QQQ share price.
        t = o.parse_option("#Short QQQ 704p lottos 6/26 3.45", self.REF)
        self.assertEqual(t["opt_type"], o.PUT)
        self.assertEqual(t["ticker"], "QQQ")
        self.assertEqual(t["strike"], 704.0)
        self.assertEqual(t["expiration"], "2026-06-26")
        self.assertEqual(t["premium"], 3.45)

    def test_multi_leg_spreads_are_not_parsed_as_single_leg_options(self):
        # Spreads are out of scope; these must fall through to the stock
        # parser rather than being misread as a single-leg option.
        for line in (
            "#Short AKAM next weeks PDS 124/122 for 90c",
            "#Long CRWV via 97/96 (Jul 26) for .25c credit.",
            "#Short SPY lotto PDS 746/745 for 37c, I believe we will fill "
            "the gap from yesterday",
        ):
            self.assertIsNone(o.parse_option(line, self.REF), line)


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

    # --- Fractional return on premium (pct) ---
    def test_pct_on_premium(self):
        self.assertAlmostEqual(
            o.resolve_option("Short", "put", 400, 3.2, 405)["pct"], 1.0)
        self.assertAlmostEqual(
            o.resolve_option("Long", "call", 500, 12.0, 480)["pct"], -1.0)
        self.assertAlmostEqual(
            o.resolve_option("Long", "call", 500, 12.0, 530)["pct"], 1.5)  # 18/12
        self.assertIsNone(
            o.resolve_option("Short", "put", 400, 3.2, 390)["pct"])  # assigned
        self.assertIsNone(
            o.resolve_option("Short", "put", 400, None, 405)["pct"])  # no premium

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

    def test_last_closes_empty_input_is_networkless(self):
        self.assertEqual(o.last_closes([]), {})


class FormatTests(unittest.TestCase):
    def test_open_line_shows_contract_and_premium(self):
        t = o.parse_option("#Short put SPY 400p 2026-07-18 @ 3.20", date(2026, 7, 1))
        line = o.format_option_open(t)
        self.assertIn("Short put", line)
        self.assertIn("SPY $400p exp 2026-07-18", line)
        self.assertIn("$3.20", line)

    def test_open_line_verb_override(self):
        t = o.parse_option("#Exit NVDA 500c 8/15 @ 15.00", date(2026, 7, 1))
        line = o.format_option_open(t, verb="Partial exit")
        self.assertTrue(line.startswith("Partial exit call"))

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
