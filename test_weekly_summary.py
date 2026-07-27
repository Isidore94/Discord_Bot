#!/usr/bin/env python3
"""Unit tests for the trade parser and weekly review in weekly_summary.py.

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
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["notes"], "for -32% on calls")
        self.assertEqual(t["outcome"], "loss")

    def test_exit_with_at_symbol(self):
        t = ws.parse_trade_line("#Exit GOOGL @ 368.88")
        self.assertEqual(t["ticker"], "GOOGL")
        self.assertEqual(t["price"], 368.88)
        self.assertEqual(t["notes"], "")

    def test_long_with_at_symbol_no_space(self):
        t = ws.parse_trade_line("#Long AAPL @175.50")
        self.assertEqual(t["ticker"], "AAPL")
        self.assertEqual(t["price"], 175.50)

    def test_exit_with_at_keyword(self):
        t = ws.parse_trade_line("#Exit CRWD at 187.60")
        self.assertEqual(t["price"], 187.60)
        self.assertEqual(t["notes"], "")

    def test_partial_exit_with_dollar_price_and_notes(self):
        t = ws.parse_trade_line(
            "#Exit partial NVDA $208.66 for over $13 profit per share. "
            "(Still have over 4/5th position on)."
        )
        self.assertTrue(t["partial"])
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["price"], 208.66)
        self.assertEqual(t["outcome"], "win")

    def test_short_side(self):
        t = ws.parse_trade_line("#Short AAPL 190.00")
        self.assertEqual(t["side"], "Short")

    def test_lowercase_side(self):
        t = ws.parse_trade_line("#short PLTR 122.46")
        self.assertEqual(t["side"], "Short")
        self.assertEqual(t["ticker"], "PLTR")

    def test_missing_price(self):
        t = ws.parse_trade_line("#Long TSLA still watching")
        self.assertIsNone(t["price"])

    def test_non_trade_line(self):
        self.assertIsNone(ws.parse_trade_line("just some chatter"))
        self.assertEqual(ws.parse_trade_lines("just some chatter"), [])

    def test_add_side(self):
        t = ws.parse_trade_line("#Add Short TSLA 393.41 avg 394.82")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "TSLA")
        self.assertEqual(t["price"], 393.41)

    def test_direction_word_before_ticker_is_not_the_ticker(self):
        # "#Exit Long ARM ..." used to parse as ticker "L" + notes "ong ARM".
        t = ws.parse_trade_line("#Exit Long ARM 351.56 - holding last 1/4")
        self.assertEqual(t["ticker"], "ARM")
        self.assertEqual(t["price"], 351.56)

    def test_trim_before_the_ticker_is_not_the_ticker(self):
        # "#exit Trim long ERX 97.85" used to parse as ticker "T" with the real
        # ticker left in the notes, inventing a phantom "T" position.
        t = ws.parse_trade_line("#exit Trim long ERX 97.85")
        self.assertEqual(t["ticker"], "ERX")
        self.assertEqual(t["price"], 97.85)
        self.assertTrue(t["partial"])

    def test_trim_with_a_fraction_before_the_ticker(self):
        t = ws.parse_trade_line("#exit Trim 1/4 Long PENG 70.90")
        self.assertEqual(t["ticker"], "PENG")
        self.assertEqual(t["price"], 70.90)

    def test_direction_word_after_ticker_is_not_the_price(self):
        t = ws.parse_trade_line("#Exit COST short $925.10 for profit")
        self.assertEqual(t["ticker"], "COST")
        self.assertEqual(t["price"], 925.10)
        self.assertEqual(t["outcome"], "win")

    def test_multiple_tickers_on_one_line(self):
        trades = ws.parse_trade_lines("#Exit FTNT, DELL for a scratch.")
        self.assertEqual([t["ticker"] for t in trades], ["FTNT", "DELL"])
        self.assertTrue(all(t["outcome"] == "flat" for t in trades))

    def test_fraction_is_not_a_price(self):
        t = ws.parse_trade_line("#Exit TSLA 1/2 out swing at 17.55")
        self.assertIsNone(t["price"])

    def test_date_is_not_a_price(self):
        t = ws.parse_trade_line("#Exit TE 7/17 10C .80 for loss")
        self.assertIsNone(t["price"])
        self.assertEqual(t["outcome"], "loss")

    def test_partial_written_after_the_price_is_still_a_partial(self):
        for line in (
            "#Exit HIMS 29.24 partial for .50c gain",
            "#Exit VRT shares trimming 5.5 half left",
            "#Exit TSLA 1/2 out swing at 17.55",
            "#Exit partial NVDA $208.66 (Still have over 4/5th position on).",
        ):
            self.assertTrue(ws.parse_trade_line(line)["partial"], line)

    def test_a_full_exit_is_not_mistaken_for_a_partial(self):
        for line in (
            "#Exit SNDK the rest for a lot per share",
            "#Exit ORCL 165.16 for 4.29 gain on runner. Fully out.",
        ):
            self.assertFalse(ws.parse_trade_line(line)["partial"], line)


class OptionParsingTests(unittest.TestCase):
    """Strikes, expiries and contract counts must never be read as fills."""

    def test_strike_is_dropped_and_premium_used(self):
        t = ws.parse_trade_line("#Long NVDA 200p July 17th for 17.10")
        self.assertTrue(t["option"])
        self.assertEqual(t["price"], 17.10)   # the premium, not the 200 strike

    def test_contract_count_dropped_in_favour_of_at_premium(self):
        t = ws.parse_trade_line("#Short QQQ 100 21 AUG 26 680/675 PUT @1.79")
        self.assertTrue(t["option"])
        self.assertEqual(t["price"], 1.79)

    def test_spread_strikes_flag_an_option(self):
        t = ws.parse_trade_line("#Short QQQ 7 Aug 680/670 for 3.45")
        self.assertTrue(t["option"])
        self.assertEqual(t["price"], 3.45)

    def test_cents_note_is_not_an_option(self):
        # ".50c" is fifty cents of gain, not a 50 strike.
        t = ws.parse_trade_line("#Exit HIMS 29.24 partial for .50c gain")
        self.assertFalse(t["option"])
        self.assertEqual(t["price"], 29.24)

    def test_put_as_a_verb_is_not_an_option(self):
        # "i put a TP on the low" is prose; flagging it flips the direction
        # the implied exit price is derived in.
        t = ws.parse_trade_line(
            "#exit SNDK for 53 dollars per share lol i put a TP on the 7/7 low"
        )
        self.assertFalse(t["option"])

    def test_exit_for_amount_is_not_read_as_an_option_fill(self):
        # On an exit "for 3.10" is as likely the gain as the fill, so it is
        # not trusted as a price.
        t = ws.parse_trade_line("#exit QQQ 700p/712c for 3.10 per contract")
        self.assertTrue(t["option"])
        self.assertIsNone(t["price"])


class GainTests(unittest.TestCase):
    """"Took a dollar per contract" is what the trade made, not what it closed at."""

    def test_gain_stated_in_the_notes(self):
        t = ws.parse_trade_line("#Exit ORCL 130p August 8th for 2 dollars per contract")
        self.assertEqual(t["gain"], 2.0)
        self.assertIsNone(t["price"])
        self.assertEqual(t["outcome"], "win")   # taking an amount is a win

    def test_gain_captured_as_the_price(self):
        t = ws.parse_trade_line("#Exit APP 3.80 per share lets see what bulls do")
        self.assertEqual(t["gain"], 3.80)
        self.assertIsNone(t["price"])           # 3.80 was never a fill

    def test_cents_are_converted_to_dollars(self):
        t = ws.parse_trade_line("#exit $QRVO with 50 cents per share")
        self.assertEqual(t["gain"], 0.50)

    def test_a_stated_loss_amount_stays_a_loss(self):
        t = ws.parse_trade_line("#exit QQQ 694p for a loss of 1.34 per contract")
        self.assertEqual(t["gain"], 1.34)
        self.assertEqual(t["outcome"], "loss")

    def test_bare_for_amount_is_kept_as_a_candidate_fill(self):
        # 00sav00 posts exits as "#Exit XLP for $1.85" with no fill captured.
        t = ws.parse_trade_line("#Exit XLP for $1.85")
        self.assertIsNone(t["price"])
        self.assertEqual(t["candidate_price"], 1.85)

    def test_a_candidate_fill_is_used_only_when_it_fits_the_entry(self):
        near = self._score(1.50, "for $1.85")
        self.assertEqual(near["outcome"], "win")
        self.assertEqual(near["exit_price"], 1.85)
        # 3.49 against a 391 entry is a gain, not a fill -- no price is taken
        # from it, even though the trade still counts as a win by convention.
        far = self._score(391.0, "for 3.49")
        self.assertIsNone(far["exit_price"])
        self.assertIsNone(far["pct"])

    def _score(self, entry, notes):
        exit_trade = ws.parse_trade_line(f"#Exit AAA {notes}")
        j = {"user": "u", "ticker": "AAA", "side": "Long", "entry_price": entry,
             "opened": "2026-07-10T00:00:00+00:00", "option": False,
             "credit": False, "swept": False, "superseded": False, "adds": 0, "exits": [exit_trade], "closed": True,
             "closed_at": "2026-07-11T00:00:00+00:00", "message_ids": []}
        return ws.score_journey(j)

    def test_a_posted_fill_beats_a_gain_in_the_notes(self):
        t = ws.parse_trade_line("#Exit RDDT 205.61 for 4.00 profit per share.")
        self.assertEqual(t["price"], 205.61)
        self.assertIsNone(t["gain"])


class BulkExitTests(unittest.TestCase):
    """'Exit all DTs at MOC except X Y' closes the day's trades in one line."""

    def test_parses_the_except_list(self):
        b = ws.parse_bulk_exit(
            "#Exit all DTs at moc except FUTU CRML and remainder of AMD "
            "(too quick to post but still have 1/2 open of the 570c 1.38 +28%)"
        )
        self.assertEqual(b["excluding"], ["AMD", "CRML", "FUTU"])

    def test_parses_the_form_posted_without_a_hash(self):
        b = ws.parse_bulk_exit(
            "Exited all DTs at MOC except META BE and remainder of AAPL CRML DAL"
        )
        self.assertEqual(b["excluding"], ["AAPL", "BE", "CRML", "DAL", "META"])

    def test_a_normal_exit_is_not_a_sweep(self):
        self.assertIsNone(ws.parse_bulk_exit("#Exit NBIS all out +43%"))

    def test_a_vague_exit_all_is_not_a_sweep(self):
        # No MOC/DT/except signal -- too vague to close positions on.
        self.assertIsNone(ws.parse_bulk_exit("#Exit all"))

    def test_sweep_closes_the_days_trades_and_spares_the_except_list(self):
        log = {"messages": {
            # Two day trades plus a swing opened a week earlier.
            "1": {"timestamp": "2026-05-14T00:00:00+00:00",
                  "content": "opreme posted a trade:\n#Long SWING 100"},
            "2": {"timestamp": "2026-05-21T14:00:00+00:00",
                  "content": "opreme posted a trade:\n#Long AAPL 200\n#Long META 300"},
            "3": {"timestamp": "2026-05-21T20:00:00+00:00",
                  "content": "opreme posted a trade:\n"
                             "#Exit all DTs at moc except META"},
        }}
        journeys = ws.build_journeys(ws.log_to_trades(log))
        state = {j["ticker"]: j["closed"] for j in journeys}
        self.assertTrue(state["AAPL"])     # swept
        self.assertFalse(state["META"])    # named after "except"
        self.assertFalse(state["SWING"])   # opened a week before, not a day trade

    def test_a_swept_position_closes_unscored(self):
        log = {"messages": {
            "1": {"timestamp": "2026-05-21T14:00:00+00:00",
                  "content": "opreme posted a trade:\n#Long AAPL 200"},
            "2": {"timestamp": "2026-05-21T20:00:00+00:00",
                  "content": "opreme posted a trade:\n#Exit all DTs at moc"},
        }}
        j = ws.build_journeys(ws.log_to_trades(log))[0]
        self.assertTrue(j["closed"])
        # No per-ticker result was posted, so none is invented.
        self.assertEqual(j["result"]["outcome"], "unknown")


class OutcomeTests(unittest.TestCase):
    def _classify(self, notes):
        return ws._classify_notes(notes)[0]

    def test_explicit_words(self):
        self.assertEqual(self._classify("for profit"), "win")
        self.assertEqual(self._classify("for 1.17 gain on 2x DT"), "win")
        self.assertEqual(self._classify("for a 9c loss at 12:50EST"), "loss")
        self.assertEqual(self._classify("swing stopped out at 2.44"), "loss")
        self.assertEqual(self._classify("took the L pos"), "loss")

    def test_scratch_beats_everything(self):
        self.assertEqual(self._classify("for a scratch."), "flat")
        self.assertEqual(self._classify("for breakeven"), "flat")
        self.assertEqual(self._classify("b/e"), "flat")

    def test_loss_beats_a_stray_profit_word(self):
        self.assertEqual(
            self._classify("for a 22 dollar loss on shares, no profit here"),
            "loss",
        )

    def test_percentages(self):
        self.assertEqual(self._classify("(+53%)"), "win")
        self.assertEqual(self._classify("1/4 left +60%"), "win")
        self.assertEqual(self._classify("strangle for 50%"), "win")
        self.assertEqual(self._classify("PCS for 140% loss."), "loss")

    def test_a_bare_percentage_is_a_gain(self):
        # Channel convention: losses are always said out loud, so a percentage
        # standing on its own is what the trade made.
        self.assertEqual(self._classify("puts .02 99%"), "win")
        self.assertEqual(self._classify("other half 61%"), "win")
        # ...unless the line does call it a loss.
        self.assertEqual(self._classify("calls 2.05 -53%"), "loss")
        self.assertEqual(self._classify("stopped .45"), "loss")

    def test_no_signal(self):
        self.assertIsNone(self._classify(""))
        self.assertIsNone(self._classify("shares trimming 5.5 half left"))


class ParseMessageTests(unittest.TestCase):
    def test_single_long(self):
        trades = ws.parse_message("isidore94 posted a trade:\n#Long $PENG 77.15")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "isidore94")
        self.assertEqual(trades[0]["ticker"], "PENG")

    def test_two_headers_in_one_message(self):
        trades = ws.parse_message(
            "isidore94 posted a trade:\n#Long FBIN 52.13\n"
            "00sav00 posted a trade:\n#Exit CRWD at 187.60"
        )
        self.assertEqual(
            [(t["user"], t["ticker"]) for t in trades],
            [("isidore94", "FBIN"), ("00sav00", "CRWD")],
        )

    def test_several_trade_lines_under_one_header(self):
        # The old parser kept only the first line and dropped the rest.
        trades = ws.parse_message(
            "isidore94 posted a trade:\n#Long IONQ 61.40\n#Long CUZ 28.55"
        )
        self.assertEqual([t["ticker"] for t in trades], ["IONQ", "CUZ"])
        self.assertTrue(all(t["user"] == "isidore94" for t in trades))

    def test_lines_without_a_header_are_ignored(self):
        self.assertEqual(ws.parse_message("#Long AAPL 100"), [])


class JourneyTests(unittest.TestCase):
    """Entry -> adds -> partials -> exit collapses into a single journey."""

    def _trades(self, rows):
        # rows: (message_id, user, side, ticker, price, partial, notes)
        out = []
        for mid, user, side, ticker, price, partial, notes in rows:
            outcome, text = (ws._classify_notes(notes)[:2] if side == "Exit"
                             else (None, ""))
            out.append({
                "message_id": str(mid), "index": 0,
                "timestamp": f"2026-07-{10 + int(mid):02d}T00:00:00+00:00",
                "user": user, "side": side, "ticker": ticker, "price": price,
                "partial": partial, "notes": notes, "option": False,
                "gain": None, "outcome": outcome, "result_text": text,
            })
        return out

    def test_round_trip_is_one_journey(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "PENG", 77.15, False, ""),
            (2, "u", "Exit", "PENG", 90.0, False, ""),
        ]))
        self.assertEqual(len(journeys), 1)
        self.assertTrue(journeys[0]["closed"])
        self.assertEqual(journeys[0]["result"]["outcome"], "win")

    def test_partial_exit_keeps_position_open(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "NVDA", 200.0, False, ""),
            (2, "u", "Exit", "NVDA", 208.66, True, "for profit"),
        ]))
        self.assertEqual(len(journeys), 1)
        self.assertFalse(journeys[0]["closed"])
        self.assertEqual(journeys[0]["result"]["outcome"], "open")

    def test_a_reentry_that_says_it_is_an_add_scales_in(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "MU", 900.0, False, ""),
            (2, "u", "Long", "MU", 940.0, False, "adding here, new avg 920"),
            (3, "u", "Add", "MU", 950.0, False, ""),
        ]))
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0]["adds"], 2)
        self.assertEqual(journeys[0]["entry_price"], 900.0)

    def test_a_reentry_that_does_not_closes_the_earlier_position(self):
        # Neither an add nor a trim, so the first position is gone even though
        # its exit was never posted.
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "SBUX", None, False, "1.00"),
            (2, "u", "Long", "SBUX", None, False, ".28 lotto"),
        ]))
        self.assertEqual(len(journeys), 2)
        self.assertTrue(journeys[0]["closed"])
        self.assertTrue(journeys[0]["superseded"])
        self.assertFalse(journeys[1]["closed"])

    def test_a_superseded_position_claims_no_result(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "SBUX", None, False, "1.00"),
            (2, "u", "Long", "SBUX", None, False, ".28 lotto"),
        ]))
        self.assertEqual(journeys[0]["result"]["outcome"], "unknown")
        self.assertIn("replaced", journeys[0]["result"]["text"])

    def test_reopening_after_a_close_is_a_new_journey(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Long", "FBIN", 52.13, False, ""),
            (2, "u", "Exit", "FBIN", 60.0, False, ""),
            (3, "u", "Long", "FBIN", 55.0, False, ""),
        ]))
        self.assertEqual(len(journeys), 2)
        self.assertTrue(journeys[0]["closed"])
        self.assertFalse(journeys[1]["closed"])

    def test_exit_without_a_logged_entry_still_appears(self):
        journeys = ws.build_journeys(self._trades([
            (1, "u", "Exit", "QRVO", 88.0, False, "for profit"),
        ]))
        self.assertEqual(len(journeys), 1)
        self.assertTrue(journeys[0]["closed"])
        self.assertIsNone(journeys[0]["opened"])
        self.assertEqual(journeys[0]["result"]["outcome"], "win")

    def test_holdings_are_the_open_journeys(self):
        journeys = ws.build_journeys(self._trades([
            (1, "a", "Long", "PENG", 77.15, False, ""),
            (2, "b", "Short", "AAPL", 190.0, False, ""),
            (3, "b", "Exit", "AAPL", 180.0, False, ""),
        ]))
        self.assertEqual(set(ws.compute_holdings(journeys)), {"a"})


class ScoringTests(unittest.TestCase):
    def _journey(self, side, entry, exits, option=False, closed=True):
        j = {
            "user": "u", "ticker": "T", "side": side, "entry_price": entry,
            "opened": "2026-07-10T00:00:00+00:00", "option": option,
            "credit": False, "swept": False, "superseded": False, "adds": 0,
            "exits": [], "closed": closed, "closed_at":
                "2026-07-11T00:00:00+00:00" if closed else None,
            "message_ids": [],
        }
        for price, notes in exits:
            gain, price = ws._extract_gain(price, notes)
            candidate = None
            if price is None and gain is None:
                m = (ws.PREMIUM_RE.search(notes) or ws.PREMIUM_FOR_RE.search(notes)
                     or ws.STOP_PRICE_RE.search(notes))
                candidate = float(m.group(1)) if m else None
            outcome, text, firm = ws._classify_notes(notes)
            if outcome is None and gain is not None:
                outcome, text = "win", ws._trim_note(notes)
            j["exits"].append({
                "price": price, "gain": gain, "candidate_price": candidate,
                "firm": firm, "partial": False, "notes": notes,
                "outcome": outcome, "result_text": text, "option": option,
            })
        j["result"] = ws.score_journey(j)
        return j

    def test_long_win_and_loss_from_prices(self):
        self.assertEqual(
            self._journey("Long", 100.0, [(110.0, "")])["result"]["outcome"],
            "win",
        )
        self.assertEqual(
            self._journey("Long", 100.0, [(90.0, "")])["result"]["outcome"],
            "loss",
        )

    def test_short_win_is_exit_below_entry(self):
        self.assertEqual(
            self._journey("Short", 100.0, [(90.0, "")])["result"]["outcome"],
            "win",
        )

    def test_stated_result_overrides_arithmetic(self):
        # Posted numbers can be premium vs share price; the trader's words win.
        j = self._journey("Long", 100.0, [(110.0, "for a loss")])
        self.assertEqual(j["result"]["outcome"], "loss")

    def test_incomparable_prices_are_not_scored(self):
        # 200 strike in, 11.70 premium out -- not a 94% loss.
        j = self._journey("Long", 200.0, [(11.70, "")])
        self.assertIsNone(j["result"]["pct"])
        self.assertIsNone(j["result"]["exit_price"])

    def test_options_score_on_premium_direction(self):
        # The trader is long the contract either way, so premium down is a
        # loss even on a position posted as a Short.
        self.assertEqual(
            self._journey("Long", 6.24, [(5.0, "")], option=True)["result"]["outcome"],
            "loss",
        )
        self.assertEqual(
            self._journey("Short", 5.0, [(6.24, "")], option=True)["result"]["outcome"],
            "win",
        )

    def test_credit_spreads_are_left_unscored(self):
        # A sold spread moves the other way, and the abbreviations are too
        # ambiguous to bet a trader's record on.
        j = self._journey("Long", 0.36, [(0.50, "PCS closed")], option=True)
        j["credit"] = True
        j["result"] = ws.score_journey(j)
        self.assertIsNone(j["result"]["pct"])

    def test_last_word_decides_when_exits_disagree(self):
        j = self._journey("Long", 100.0, [(101.0, "for profit"),
                                          (99.0, "for a loss")])
        self.assertEqual(j["result"]["outcome"], "loss")

    def test_open_journey_is_open(self):
        j = self._journey("Long", 100.0, [], closed=False)
        self.assertEqual(j["result"]["outcome"], "open")

    def test_gain_implies_the_exit_price(self):
        # Bought the contract at 14, took a dollar -> exited at 15.
        j = self._journey("Long", 14.0, [(None, "for 1 dollar per contract")],
                          option=True)
        self.assertEqual(j["result"]["outcome"], "win")
        self.assertEqual(j["result"]["exit_price"], 15.0)
        self.assertTrue(j["result"]["implied"])
        self.assertAlmostEqual(j["result"]["pct"], 100 / 14, places=4)

    def test_a_short_covers_lower_on_a_gain(self):
        j = self._journey("Short", 404.25, [(None, "for 3.80 per share")])
        self.assertEqual(j["result"]["exit_price"], 400.45)
        self.assertEqual(j["result"]["outcome"], "win")

    def test_an_option_loses_premium_on_a_losing_trade(self):
        j = self._journey("Short", 5.0,
                          [(None, "for a loss of 1.34 per contract")],
                          option=True)
        self.assertEqual(j["result"]["outcome"], "loss")
        self.assertEqual(j["result"]["exit_price"], 3.66)

    def test_mismatched_units_do_not_imply_an_absurd_exit(self):
        # A $25/share gain against a $0.25 option premium is two different
        # instruments, not a -10000% trade with a negative exit price.
        j = self._journey("Long", 0.25, [(None, "PCS for full loss 25 per share")],
                          option=True)
        self.assertEqual(j["result"]["outcome"], "loss")
        self.assertFalse(j["result"]["implied"])
        self.assertIsNone(j["result"]["exit_price"])

    def test_a_stop_out_is_judged_on_the_amount_not_the_word(self):
        # Contracts: stopped below what was paid is a loss, above it is a win.
        self.assertEqual(
            self._journey("Long", 1.00, [(None, "stopped .45")],
                          option=True)["result"]["outcome"], "loss")
        self.assertEqual(
            self._journey("Long", 0.20, [(None, "stopped .45")],
                          option=True)["result"]["outcome"], "win")
        # Shares: same logic, and a short covered lower is a winner.
        self.assertEqual(
            self._journey("Long", 100.0, [(None, "stopped out at 90")]
                          )["result"]["outcome"], "loss")
        self.assertEqual(
            self._journey("Short", 290.37, [(None, "trailing stopped 285")]
                          )["result"]["outcome"], "win")

    def test_a_stop_out_with_no_entry_price_falls_back_to_the_word(self):
        j = self._journey("Long", None, [(None, "stopped .10")], option=True)
        self.assertEqual(j["result"]["outcome"], "loss")

    def test_a_stated_loss_still_beats_the_prices(self):
        # "stopped" defers to the amount; "for a loss" does not.
        j = self._journey("Long", 100.0, [(110.0, "stopped out for a loss")])
        self.assertEqual(j["result"]["outcome"], "loss")

    def test_a_flat_exit_reads_as_a_scratch_not_minus_zero_percent(self):
        j = self._journey("Short", 110.19, [(110.19, "for breakeven.")])
        self.assertEqual(j["result"]["outcome"], "flat")
        self.assertEqual(j["result"]["text"], "scratch")

    def test_a_trim_keeps_later_exits_on_the_same_journey(self):
        trades = ws.log_to_trades({"messages": {
            "1": {"timestamp": "2026-07-24T00:00:00+00:00",
                  "content": "u posted a trade:\n#Short HIMS 29.74"},
            "2": {"timestamp": "2026-07-24T01:00:00+00:00",
                  "content": "u posted a trade:\n"
                             "#Exit HIMS 29.24 partial for .50c gain"},
            "3": {"timestamp": "2026-07-24T02:00:00+00:00",
                  "content": "u posted a trade:\n#Exit $HIMS 28.74 for 1.00 gain"},
        }})
        journeys = ws.build_journeys(trades)
        self.assertEqual(len(journeys), 1)
        self.assertTrue(journeys[0]["closed"])
        self.assertEqual(journeys[0]["result"]["outcome"], "win")


class SummaryTests(unittest.TestCase):
    NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)

    def _sample_log(self):
        def msg(mid, ts, content):
            return str(mid), {"timestamp": ts, "content": content}

        return {"messages": dict([
            # isidore94: PENG opened 3 weeks ago, closed for a win this week.
            msg(100, "2026-06-20T00:00:00+00:00",
                "isidore94 posted a trade:\n#Long $PENG 77.15"),
            msg(105, "2026-07-08T00:00:00+00:00",
                "isidore94 posted a trade:\n#Exit PENG 90.00 for profit"),
            # isidore94: FBIN opened this week, still open.
            msg(101, "2026-07-09T00:00:00+00:00",
                "isidore94 posted a trade:\n#Long FBIN 52.13"),
            # 00sav00: exit with no logged entry.
            msg(102, "2026-07-11T00:00:00+00:00",
                "00sav00 posted a trade:\n#Exit CRWD at 187.60 for a loss"),
            # 1ripley: opened then partially exited -> stays open.
            msg(103, "2026-07-10T00:00:00+00:00",
                "1ripley posted a trade:\n#Long NVDA 200.00"),
            msg(104, "2026-07-11T12:00:00+00:00",
                "1ripley posted a trade:\n"
                "#Exit partial NVDA $208.66 still holding 4/5"),
        ])}

    def test_one_line_per_trade(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        # Four journeys in the log -> four bullet lines, no more.
        self.assertEqual(summary.count("\n- "), 4)

    def test_win_loss_and_open_icons(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        iso = summary.split("## isidore94")[1].split("\n## ")[0]
        self.assertIn(f"{ws.ICON_WIN} **PENG**", iso)
        self.assertIn(f"{ws.ICON_OPEN} **FBIN**", iso)

        sav = summary.split("## 00sav00")[1].split("\n## ")[0]
        self.assertIn(f"{ws.ICON_LOSS} **CRWD**", sav)

        rip = summary.split("## 1ripley")[1].split("\n## ")[0]
        self.assertIn(f"{ws.ICON_OPEN} **NVDA**", rip)
        self.assertIn("1 partial taken", rip)

    def test_trade_opened_before_the_week_shows_its_whole_journey(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        # PENG was opened Jun 20 and closed Jul 8: the entry price is on the
        # line even though the entry itself is outside the weekly window.
        self.assertIn("**PENG** Long 77.15 → 90", summary)
        self.assertIn("Jun 20→Jul 8", summary)

    def test_open_position_shows_how_long_it_has_been_held(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        self.assertIn("open since Jul 9 (3d)", summary)

    def test_per_trader_records(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        self.assertIn("This week: 1W–0L–0S (100%)", summary)   # isidore94
        self.assertIn("This week: 0W–1L–0S (0%)", summary)     # 00sav00
        self.assertIn("1 open", summary)

    def test_community_header(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        self.assertIn("Community this week:", summary)
        self.assertIn("2 trade(s) closed by 2 trader(s)", summary)

    def test_trimmed_positions_are_counted_as_unresolved(self):
        # 1ripley's real pattern: full exits on winners, trims left open. The
        # win rate cannot show a loss, so the exposure has to be visible.
        log = {"messages": {
            "1": {"timestamp": "2026-07-08T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long AAA 100"},
            "2": {"timestamp": "2026-07-09T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit AAA 110 for profit"},
            "3": {"timestamp": "2026-07-08T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long BBB 100"},
            "4": {"timestamp": "2026-07-09T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit BBB 90 partial, holding rest"},
        }}
        summary = ws.build_summary(log, self.NOW)
        self.assertIn("This week: 1W–0L–0S (100%)", summary)
        self.assertIn("1 open, 1 trimmed", summary)

    def test_untouched_open_positions_collapse_to_one_line(self):
        old = (self.NOW - timedelta(days=60)).isoformat()
        tickers = [f"AA{chr(ord('A') + i)}" for i in range(20)]
        messages = {
            str(i): {"timestamp": old,
                     "content": f"u posted a trade:\n#Long {t} 100"}
            for i, t in enumerate(tickers)
        }
        # One position touched this week keeps its own line.
        messages["99"] = {"timestamp": "2026-07-10T00:00:00+00:00",
                          "content": "u posted a trade:\n#Long ZZZ 50"}
        summary = ws.build_summary({"messages": messages}, self.NOW)
        self.assertIn("- 🟠 **ZZZ**", summary)
        self.assertIn("Also carrying 20:", summary)
        # Only MAX_TICKERS get named; the rest are a count.
        self.assertIn(f"+{20 - ws.MAX_TICKERS} more", summary)
        self.assertEqual(summary.count("\n- 🟠"), 1)

    def test_a_stale_position_is_flagged(self):
        opened = self.NOW - timedelta(days=ws.STALE_DAYS + 5)
        log = {"messages": {"1": {
            "timestamp": opened.isoformat(),
            "content": "u posted a trade:\n#Long AAA 100",
        }}}
        summary = ws.build_summary(log, self.NOW)
        self.assertIn(f"{ws.STALE_DAYS + 5}d {ws.ICON_STALE}", summary)

    def test_a_trader_who_did_not_trade_collapses_to_one_line(self):
        log = {"messages": {
            # Opened well before the week and untouched since.
            "1": {"timestamp": "2026-06-01T00:00:00+00:00",
                  "content": "quiet posted a trade:\n#Long AAA 100"},
            # Someone who did trade this week keeps a full section.
            "2": {"timestamp": "2026-07-10T00:00:00+00:00",
                  "content": "busy posted a trade:\n#Long BBB 50"},
        }}
        summary = ws.build_summary(log, self.NOW)
        self.assertIn("## busy", summary)
        self.assertNotIn("## quiet", summary)
        self.assertIn("Carrying — nothing traded this week", summary)
        self.assertIn("**quiet**", summary)
        self.assertIn("AAA (41d", summary)

    def test_a_trader_who_only_opened_this_week_keeps_a_section(self):
        log = {"messages": {"1": {
            "timestamp": "2026-07-10T00:00:00+00:00",
            "content": "u posted a trade:\n#Long AAA 100",
        }}}
        summary = ws.build_summary(log, self.NOW)
        self.assertIn("## u", summary)
        self.assertNotIn("Carrying", summary)

    def test_empty_log(self):
        summary = ws.build_summary({"messages": {}}, self.NOW)
        self.assertIn("No trades closed this week", summary)

    def test_chunking_stays_under_limit(self):
        summary = ws.build_summary(self._sample_log(), self.NOW)
        for chunk in ws.chunk_message(summary):
            self.assertLessEqual(len(chunk), ws.CHUNK_LIMIT)


class ContentLogTests(unittest.TestCase):
    def test_content_entries_are_reparsed(self):
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

    def test_legacy_trades_entries_still_supported(self):
        log = {"messages": {
            "600": {
                "timestamp": "2026-07-10T00:00:00+00:00",
                "trades": [{"user": "u", "side": "Long", "ticker": "PENG",
                            "price": 77.15, "partial": False, "notes": ""}],
            },
        }}
        trades = ws.log_to_trades(log)
        self.assertEqual(trades[0]["ticker"], "PENG")
        self.assertFalse(trades[0]["option"])       # defaulted for old entries


class FetchWindowTests(unittest.TestCase):
    NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)

    def test_first_run_backfills_the_history_window(self):
        after = ws.fetch_after({"messages": {}}, self.NOW)
        self.assertEqual(after, ws.snowflake_for(ws.history_start(self.NOW)))

    def test_a_log_that_does_not_reach_back_far_enough_backfills(self):
        # A log built by an older, shorter lookback has no coverage marker, so
        # the bot refills the full history window instead of only moving
        # forward from the newest message it happens to hold.
        log = {"messages": {"250": {"timestamp": "2026-07-08T00:00:00+00:00",
                                    "content": ""}}}
        self.assertTrue(ws.needs_backfill(log, self.NOW))
        self.assertEqual(ws.fetch_after(log, self.NOW),
                         ws.snowflake_for(ws.history_start(self.NOW)))

    def test_covered_log_resumes_from_newest_logged_id(self):
        log = {
            "covered_since": (self.NOW - timedelta(days=120)).isoformat(),
            "messages": {
                "100": {"timestamp": "2026-07-01T00:00:00+00:00", "content": ""},
                "250": {"timestamp": "2026-07-08T00:00:00+00:00", "content": ""},
                "175": {"timestamp": "2026-07-05T00:00:00+00:00", "content": ""},
            },
        }
        self.assertFalse(ws.needs_backfill(log, self.NOW))
        self.assertEqual(ws.fetch_after(log, self.NOW), 250)


class PruneTests(unittest.TestCase):
    def test_open_position_survives_retention(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        old = (now - timedelta(days=ws.RETENTION_DAYS + 10)).isoformat()
        log = {"messages": {
            "1": {"timestamp": old,
                  "content": "u posted a trade:\n#Long PENG 77.15"},
            "2": {"timestamp": old,
                  "content": "u posted a trade:\n#Long CRWD 187.60"},
            "3": {"timestamp": old,
                  "content": "u posted a trade:\n#Exit CRWD 190.00"},
        }}
        ws.prune_log(log, ws.build_journeys(ws.log_to_trades(log)), now)
        self.assertIn("1", log["messages"])         # PENG still open
        self.assertNotIn("2", log["messages"])      # CRWD round trip is done
        self.assertNotIn("3", log["messages"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
