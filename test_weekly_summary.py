#!/usr/bin/env python3
"""Unit tests for the YAGPDB trade-message parser in weekly_summary.py.

The sample messages below are real posts from the trade channel, including
every format that a previous parser version missed or mangled.
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

    def test_exit_with_at_symbol(self):
        t = ws.parse_trade_line("#Exit GOOGL @ 368.88")
        self.assertEqual(t["ticker"], "GOOGL")
        self.assertEqual(t["price"], 368.88)

    def test_long_with_at_symbol_no_space(self):
        t = ws.parse_trade_line("#Long AAPL @175.50")
        self.assertEqual(t["ticker"], "AAPL")
        self.assertEqual(t["price"], 175.50)

    def test_exit_with_at_keyword(self):
        t = ws.parse_trade_line("#Exit CRWD at 187.60")
        self.assertEqual(t["ticker"], "CRWD")
        self.assertEqual(t["price"], 187.60)

    def test_partial_exit_flag(self):
        t = ws.parse_trade_line(
            "#Exit partial NVDA $208.66 for over $13 profit per share."
        )
        self.assertTrue(t["partial"])
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["price"], 208.66)

    def test_missing_price(self):
        t = ws.parse_trade_line("#Long TSLA still watching")
        self.assertEqual(t["ticker"], "TSLA")
        self.assertIsNone(t["price"])
        self.assertEqual(t["notes"], "still watching")

    def test_non_trade_line(self):
        self.assertIsNone(ws.parse_trade_line("just some chatter"))

    # --- formats a previous parser version missed or mangled ---------------

    def test_exit_long_direction_word_is_not_the_ticker(self):
        # Used to parse the ticker as "L".
        t = ws.parse_trade_line("#Exit Long MU for break even.")
        self.assertEqual(t["side"], "Exit")
        self.assertEqual(t["ticker"], "MU")
        self.assertFalse(t["partial"])

    def test_exit_long_with_price_later_in_line(self):
        t = ws.parse_trade_line("#Exit Long ALAB starter at 303.53")
        self.assertEqual(t["ticker"], "ALAB")
        self.assertEqual(t["price"], 303.53)

    def test_exit_direction_word_before_price(self):
        t = ws.parse_trade_line("#Exit IBM short $116.40 for profit")
        self.assertEqual(t["ticker"], "IBM")
        self.assertEqual(t["price"], 116.40)

    def test_size_word_before_ticker_is_not_the_ticker(self):
        # "#Add Small add Long DRAM" -- ticker is DRAM, not "SMALL".
        t = ws.parse_trade_line("#Add Small add Long DRAM. New avg: 51.29")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "DRAM")
        self.assertEqual(t["avg"], 51.29)

    def test_add_side_with_avg(self):
        t = ws.parse_trade_line("#Add PENG 79.85 avg is 79.91")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "PENG")
        self.assertEqual(t["price"], 79.85)
        self.assertEqual(t["avg"], 79.91)

    def test_lowercase_add_with_avg_only(self):
        t = ws.parse_trade_line("#add PENG new avg is 77.90")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "PENG")
        self.assertIsNone(t["price"])
        self.assertEqual(t["avg"], 77.90)

    def test_add_with_direction_and_bare_avg(self):
        t = ws.parse_trade_line("#Add Short TSLA 393.41 avg 394.82")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "TSLA")
        self.assertEqual(t["avg"], 394.82)

    def test_sold_option_strike_is_not_price_but_premium_is(self):
        t = ws.parse_trade_line(
            "#Long sold DRAM 40p November 20th expiry for 4.20"
        )
        self.assertEqual(t["side"], "Long")
        self.assertEqual(t["ticker"], "DRAM")
        self.assertTrue(t["option"])
        self.assertTrue(t["sold"])          # "sold" sits before the ticker
        self.assertEqual(t["contract"], "put")
        self.assertEqual(t["price"], 4.20)  # 40p is the strike; 4.20 the premium

    def test_bought_put_captures_premium_not_strike(self):
        t = ws.parse_trade_line("#Short IONQ 43p this week for 3.55")
        self.assertEqual(t["ticker"], "IONQ")
        self.assertTrue(t["option"])
        self.assertFalse(t["sold"])
        self.assertEqual(t["contract"], "put")
        self.assertEqual(t["price"], 3.55)  # bearish via LONG puts, premium 3.55

    def test_percentage_is_not_a_price(self):
        t = ws.parse_trade_line("#Exit QQQ 100% PDS a while ago")
        self.assertEqual(t["ticker"], "QQQ")
        self.assertIsNone(t["price"])

    def test_comma_price(self):
        t = ws.parse_trade_line("#Exit partial BTC 51,000")
        self.assertEqual(t["ticker"], "BTC")
        self.assertEqual(t["price"], 51000.0)
        self.assertTrue(t["partial"])

    def test_fraction_is_partial_and_price_found_after_at(self):
        t = ws.parse_trade_line("#Exit MUU 3/4 out at $750 nice overnight move")
        self.assertEqual(t["ticker"], "MUU")
        self.assertEqual(t["price"], 750.0)
        self.assertTrue(t["partial"])

    def test_exit_the_rest_of_is_a_full_exit(self):
        t = ws.parse_trade_line(
            "#exit the rest of ASTS for 1.10 per share going to go eat breakfast"
        )
        self.assertEqual(t["ticker"], "ASTS")
        self.assertFalse(t["partial"])

    def test_half_in_notes_is_partial(self):
        t = ws.parse_trade_line("#Exit PENG 82.91 half for 3.00 gain.")
        self.assertEqual(t["price"], 82.91)
        self.assertTrue(t["partial"])

    def test_swing_rest_keeps_position_open(self):
        t = ws.parse_trade_line(
            "#Exit AMZN 254.72 for partial 1.00 gain swing rest."
        )
        self.assertTrue(t["partial"])

    def test_trailing_the_rest_keeps_position_open(self):
        t = ws.parse_trade_line(
            "#exit partial SNDK for 13 dollars per share trailing the rest"
        )
        self.assertTrue(t["partial"])

    def test_for_rest_of_position_is_full_exit(self):
        t = ws.parse_trade_line(
            "#Exit PENG 79.91 for breakeven for rest of position."
        )
        self.assertFalse(t["partial"])

    def test_half_in_later_commentary_is_not_partial(self):
        t = ws.parse_trade_line(
            "#Exit GS 1112.48 for 35.91 loss wanted half of earnings "
            "candle preserved."
        )
        self.assertFalse(t["partial"])

    def test_date_fraction_is_not_partial(self):
        t = ws.parse_trade_line(
            "#exit SNDK for 53 dollars per share lol i forgot i put a TP "
            "on the 7/7 low"
        )
        self.assertFalse(t["partial"])

    def test_mixed_case_ticker_normalized(self):
        t = ws.parse_trade_line("#Exit Dell at $418.20, took the loss")
        self.assertEqual(t["ticker"], "DELL")
        self.assertEqual(t["price"], 418.20)


class ParseMessageTests(unittest.TestCase):
    def test_single_long(self):
        trades = ws.parse_message("isidore94 posted a trade:\n#Long $PENG 77.15")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "isidore94")
        self.assertEqual(trades[0]["ticker"], "PENG")
        self.assertEqual(trades[0]["price"], 77.15)

    def test_two_pairs_in_one_message(self):
        trades = ws.parse_message(
            "isidore94 posted a trade:\n#Long FBIN 52.13\n"
            "00sav00 posted a trade:\n#Exit CRWD at 187.60"
        )
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["user"], "isidore94")
        self.assertEqual(trades[0]["ticker"], "FBIN")
        self.assertEqual(trades[1]["user"], "00sav00")
        self.assertEqual(trades[1]["ticker"], "CRWD")

    def test_two_tags_in_one_block(self):
        trades = ws.parse_message(
            "mallowmushroom posted a trade:\n"
            "#Short COIN 157.51\n#Add Short TSLA 393.41 avg 394.82"
        )
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["ticker"], "COIN")
        self.assertEqual(trades[0]["side"], "Short")
        self.assertEqual(trades[1]["ticker"], "TSLA")
        self.assertEqual(trades[1]["side"], "Add")
        self.assertEqual(trades[1]["user"], "mallowmushroom")

    def test_leading_text_before_tag(self):
        # Previously dropped: the tag has to be found mid-line.
        trades = ws.parse_message(
            "opreme posted a trade:\n"
            "Swing port trade: #Long AVGO Sept 500c for 8.8 Stop 5 Target 23-30"
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["user"], "opreme")
        self.assertEqual(trades[0]["side"], "Long")
        self.assertEqual(trades[0]["ticker"], "AVGO")

    def test_tag_after_chatter_sentences(self):
        trades = ws.parse_message(
            "isidore94 posted a trade:\n"
            "alr im out for the day. got a membership at a golf course and off "
            "to play my first 18 at it. Will see ya'll tmr. "
            "#Exit EA for a 40 cent loss per share."
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "Exit")
        self.assertEqual(trades[0]["ticker"], "EA")


class EpisodeTests(unittest.TestCase):
    """Position logic: Long/Short opens, full Exit closes, partial keeps open."""

    def _trades(self, rows):
        # rows: (message_id, user, side, ticker, price, partial[, extra])
        out = []
        for row in rows:
            mid, user, side, ticker, price, partial = row[:6]
            extra = row[6] if len(row) > 6 else {}
            t = {"message_id": str(mid), "index": 0,
                 "timestamp": f"2026-07-{10 + int(mid) // 100:02d}T00:00:00+00:00",
                 "user": user, "side": side, "ticker": ticker, "price": price,
                 "partial": partial, "avg": None, "notes": ""}
            t.update(extra)
            out.append(t)
        return out

    def test_open_and_full_exit_closes(self):
        closed, open_map, orphans = ws.compute_episodes(self._trades([
            (1, "u", "Long", "PENG", 77.15, False),
            (2, "u", "Exit", "PENG", 90.0, False),
        ]))
        self.assertEqual(open_map, {})
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["entry_price"], 77.15)
        self.assertEqual(closed[0]["exits"][0]["price"], 90.0)

    def test_partial_exit_keeps_position_open(self):
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Long", "NVDA", 200.0, False),
            (2, "u", "Exit", "NVDA", 208.66, True),
        ]))
        self.assertEqual(closed, [])
        self.assertIn(("u", "NVDA"), open_map)
        self.assertEqual(len(open_map[("u", "NVDA")]["exits"]), 1)

    def test_partials_fold_into_one_closed_episode(self):
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Short", "SNDK", 1683.0, False),
            (2, "u", "Exit", "SNDK", None, True),
            (3, "u", "Exit", "SNDK", None, False),
        ]))
        self.assertEqual(open_map, {})
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(closed[0]["exits"]), 2)

    def test_correction_before_exit_updates_entry(self):
        # "#Short IBM $217" twice, then "#Short IBM $118 (corrected)".
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Short", "IBM", 217.0, False),
            (2, "u", "Short", "IBM", 217.0, False),
            (3, "u", "Short", "IBM", 118.0, False),
            (4, "u", "Exit", "IBM", 116.4, False),
        ]))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["entry_price"], 118.0)
        self.assertEqual(ws.score_episode(closed[0])[0], "win")

    def test_reentry_after_partial_closes_old_episode(self):
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Long", "MUU", 676.79, False),
            (2, "u", "Exit", "MUU", 750.0, True),
            (3, "u", "Long", "MUU", 28.05, False),
        ]))
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["entry_price"], 676.79)
        self.assertEqual(open_map[("u", "MUU")]["entry_price"], 28.05)

    def test_add_updates_avg_entry(self):
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Long", "PENG", 77.15, False),
            (2, "u", "Add", "PENG", 79.85, False, {"avg": 79.91}),
        ]))
        ep = open_map[("u", "PENG")]
        self.assertEqual(ep["entry_price"], 79.91)
        self.assertEqual(ep["adds"], 1)

    def test_long_with_add_note_folds_into_position(self):
        closed, open_map, _ = ws.compute_episodes(self._trades([
            (1, "u", "Long", "NVDA", 200.0, False),
            (2, "u", "Exit", "NVDA", 208.0, True),
            (3, "u", "Long", "NVDA", None, False,
             {"notes": "add new avg $195.05", "avg": 195.05}),
        ]))
        self.assertEqual(closed, [])
        ep = open_map[("u", "NVDA")]
        self.assertEqual(ep["entry_price"], 195.05)
        self.assertEqual(ep["adds"], 1)

    def test_exit_without_entry_is_an_orphan(self):
        closed, open_map, orphans = ws.compute_episodes(self._trades([
            (1, "u", "Exit", "TGT", None, False),
        ]))
        self.assertEqual(closed, [])
        self.assertEqual(open_map, {})
        self.assertEqual(len(orphans), 1)

    def test_holdings_grouped_per_user(self):
        holdings = ws.compute_holdings(self._trades([
            (1, "a", "Long", "PENG", 77.15, False),
            (2, "b", "Short", "AAPL", 190.0, False),
        ]))
        self.assertEqual(set(holdings), {"a", "b"})


class ScoringTests(unittest.TestCase):
    def _ep(self, side, entry, exits):
        return {"user": "u", "ticker": "T", "side": side, "entry_price": entry,
                "adds": 0, "opened_ts": "2026-07-10T00:00:00+00:00",
                "message_id": "1", "closed_ts": "2026-07-10T01:00:00+00:00",
                "exits": [{"price": p, "notes": n, "partial": False,
                           "timestamp": "2026-07-10T01:00:00+00:00"}
                          for p, n in exits]}

    def test_long_win_and_loss_by_price(self):
        self.assertEqual(
            ws.score_episode(self._ep("Long", 100.0, [(110.0, "")]))[0], "win")
        self.assertEqual(
            ws.score_episode(self._ep("Long", 100.0, [(90.0, "")]))[0], "loss")

    def test_short_win_is_exit_below_entry(self):
        result, pct = ws.score_episode(self._ep("Short", 100.0, [(90.0, "")]))
        self.assertEqual(result, "win")
        self.assertAlmostEqual(pct, 10.0)

    def test_equal_prices_scratch(self):
        self.assertEqual(
            ws.score_episode(self._ep("Short", 459.77, [(459.77, "")]))[0],
            "scratch")

    def test_near_entry_exit_is_scratch_not_loss(self):
        # "#Long DLO 15.02" -> "#Exit DLO 15 scratch": -0.13% is a scratch.
        self.assertEqual(
            ws.score_episode(self._ep("Long", 15.02, [(15.0, "scratch")]))[0],
            "scratch")

    def test_partials_averaged(self):
        result, pct = ws.score_episode(
            self._ep("Long", 100.0, [(120.0, ""), (100.0, "")]))
        self.assertEqual(result, "win")
        self.assertAlmostEqual(pct, 10.0)

    def test_implausible_exit_price_falls_back_to_notes(self):
        # "#Short CRCL 6066" (typo) then "#Exit CRCL 60.83": price unusable.
        self.assertEqual(
            ws.score_episode(self._ep("Short", 6066.0, [(60.83, "")]))[0],
            None)
        # Option premium exit vs a share-price entry, saved by the notes.
        self.assertEqual(
            ws.score_episode(
                self._ep("Long", 16.0, [(2.76, "calls for 167% gain")]))[0],
            "win")

    def test_notes_classification(self):
        self.assertEqual(ws.classify_notes("for a scratch. not interested"),
                         "scratch")
        self.assertEqual(ws.classify_notes("for breakeven for rest"), "scratch")
        self.assertEqual(ws.classify_notes("b/e"), "scratch")
        self.assertEqual(ws.classify_notes("for a 40 cent loss per share"),
                         "loss")
        self.assertEqual(ws.classify_notes("swing stopped out starter"), "loss")
        self.assertEqual(ws.classify_notes("for 53 dollars per share lol"),
                         "win")
        self.assertEqual(ws.classify_notes("for .50 gain on puts"), "win")
        self.assertEqual(ws.classify_notes("~8$ avg gain"), "win")
        self.assertIsNone(ws.classify_notes("not a place I want to be short"))
        self.assertIsNone(ws.classify_notes(""))

    def test_win_rate_line(self):
        line = ws._win_rate_line(["win", "win", "loss", "scratch"])
        self.assertIn("67%", line)
        self.assertIn("2W–1L", line)
        self.assertIn("1 scratch", line)
        self.assertIn("2 scratches",
                      ws._win_rate_line(["win", "scratch", "scratch"]))
        self.assertIsNone(ws._win_rate_line(["scratch"]))
        self.assertIsNone(ws._win_rate_line([]))


class OptionScoringTests(unittest.TestCase):
    """Options track a premium, so P&L follows the contract, not the tag."""

    def _episode(self, log_rows):
        log = {"messages": {
            str(mid): {"timestamp": ts, "content": content}
            for mid, ts, content in log_rows
        }}
        closed, open_map, orphans = ws.compute_episodes(ws.log_to_trades(log))
        return closed, open_map, orphans

    def test_bearish_via_puts_is_a_win_when_premium_rises(self):
        # The trade the whole options fix started from: short IWM via long puts,
        # bought at 1.93, sold at 2.32 -- a WIN even though "exit > entry".
        closed, _, _ = self._episode([
            (1, "2026-07-21T13:30:00+00:00",
             "isidore94 posted a trade:\n#Short IWM via 295p for 1.93 expires today"),
            (2, "2026-07-21T14:01:00+00:00",
             "isidore94 posted a trade:\n#exit IWM @ 2.32 SPY strength"),
        ])
        self.assertEqual(len(closed), 1)
        result, pct = ws.score_episode(closed[0])
        self.assertEqual(result, "win")
        self.assertGreater(pct, 0)
        self.assertIn("puts", ws._closed_line(closed[0]))

    def test_sold_option_wins_when_premium_falls(self):
        # Wrote a put for 4.20, bought it back at 2.00 -> keep the difference.
        closed, _, _ = self._episode([
            (1, "2026-07-21T13:30:00+00:00",
             "u posted a trade:\n#Long sold DRAM 40p Nov 20th expiry for 4.20"),
            (2, "2026-07-21T14:30:00+00:00",
             "u posted a trade:\n#Exit DRAM 40p at 2.00"),
        ])
        result, pct = ws.score_episode(closed[0])
        self.assertEqual(result, "win")
        self.assertIn("sold puts", ws._closed_line(closed[0]))

    def test_bought_call_loses_when_premium_falls(self):
        closed, _, _ = self._episode([
            (1, "2026-07-21T13:30:00+00:00",
             "u posted a trade:\n#Long AVGO Sept 500c for 8.8"),
            (2, "2026-07-22T14:30:00+00:00",
             "u posted a trade:\n#Exit AVGO 500c at 5.0"),
        ])
        result, _ = ws.score_episode(closed[0])
        self.assertEqual(result, "loss")

    def test_spread_is_scored_from_notes_not_premium(self):
        # A spread's credit/debit direction is ambiguous; trust the stated P&L.
        closed, _, _ = self._episode([
            (1, "2026-07-20T13:30:00+00:00",
             "leebero posted a trade:\n#Long BE 250/245 PCS for 1.00"),
            (2, "2026-07-21T14:30:00+00:00",
             "leebero posted a trade:\n#Exit BE PCS for 20% gain"),
        ])
        ep = closed[0]
        self.assertTrue(ep["spread"])
        result, pct = ws.score_episode(ep)
        self.assertEqual(result, "win")   # from "20% gain", not price math
        self.assertIsNone(pct)            # no premium direction used
        self.assertIn("spread", ws._closed_line(ep))

    def test_assignment_is_a_stock_position_not_an_option(self):
        # Being assigned on puts converts them to stock at the avg price.
        t = ws.parse_trade_line("#Long MSFT +assigned puts new avg $398.55")
        self.assertFalse(t["option"])
        self.assertIsNone(t["contract"])
        self.assertEqual(t["avg"], 398.55)

    def test_stock_mentioning_a_strike_stays_a_stock(self):
        # "took assignment of my July 65p" must not turn a stock long optiony.
        closed, open_map, _ = self._episode([
            (1, "2026-07-18T03:00:00+00:00",
             "isidore94 posted a trade:\n#long DRAM 60.84 took assignment of my July 65p"),
        ])
        ep = open_map[("isidore94", "DRAM")]
        self.assertFalse(ep["option"])
        self.assertEqual(ep["entry_price"], 60.84)


class SummaryTests(unittest.TestCase):
    def _log(self, rows):
        # rows: (message_id, timestamp, content)
        return {"messages": {
            str(mid): {"timestamp": ts, "content": content}
            for mid, ts, content in rows
        }}

    def _sample_log(self):
        return self._log([
            # isidore94: SNDK day trade with a partial -> ONE closed line
            (100, "2026-07-10T15:00:00+00:00",
             "isidore94 posted a trade:\n#Short SNDK 1683"),
            (101, "2026-07-10T16:30:00+00:00",
             "isidore94 posted a trade:\n"
             "#exit partial SNDK for 13 dollars per share trailing the rest"),
            (102, "2026-07-10T17:45:00+00:00",
             "isidore94 posted a trade:\n"
             "#Exit SNDK the rest for 19 dollars per share"),
            # isidore94: PENG swing opened three weeks ago, still open
            (50, "2026-06-20T00:00:00+00:00",
             "isidore94 posted a trade:\n#Long $PENG 77.15"),
            # 00sav00: full exit this week -> weekly activity, no open trades
            (103, "2026-07-11T00:00:00+00:00",
             "00sav00 posted a trade:\n#Exit CRWD at 187.60"),
            # 1ripley: opened NVDA this week then partially exited -> open
            (104, "2026-07-10T00:00:00+00:00",
             "1ripley posted a trade:\n#Long NVDA 200"),
            (105, "2026-07-11T12:00:00+00:00",
             "1ripley posted a trade:\n#Exit partial NVDA $208.66"),
        ])

    def test_trader_by_trader_structure(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        for user in ("isidore94", "00sav00", "1ripley"):
            self.assertIn(f"## {user}", summary)
        self.assertEqual(summary.count("**Closed this week**"), 3)
        self.assertEqual(summary.count("**Open trades**"), 3)

    def test_round_trip_is_a_single_line(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        iso = summary.split("## isidore94")[1].split("## ")[0]
        sndk_lines = [l for l in iso.splitlines() if "SNDK" in l]
        self.assertEqual(len(sndk_lines), 1)
        line = sndk_lines[0]
        self.assertIn("Short @ 1683", line)
        self.assertIn("1 partial", line)
        self.assertIn("day trade", line)
        self.assertIn("✅", line)  # "dollars per share" notes -> win

    def test_open_positions_persist_and_partial_stays_open(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        iso = summary.split("## isidore94")[1].split("## ")[0]
        self.assertIn("PENG", iso.split("**Open trades**")[1])

        rip = summary.split("## 1ripley")[1].split("## ")[0]
        open_part = rip.split("**Open trades**")[1]
        self.assertIn("NVDA", open_part)
        self.assertIn("1 partial taken", open_part)

    def test_orphan_exit_is_listed_not_dropped(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
        sav = summary.split("## 00sav00")[1].split("## ")[0]
        self.assertIn("**CRWD** Exit @ 187.6", sav)
        self.assertIn("_none_", sav.split("**Open trades**")[1])

    def test_chunking_stays_under_limit(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(self._sample_log(), now)
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
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["ticker"], "PENG")
        self.assertIsNone(trades[0]["avg"])


class FetchWindowTests(unittest.TestCase):
    def test_first_run_backfills_initial_window(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        after = ws.fetch_after({"messages": {}}, now)
        expected = ws.snowflake_for(now - timedelta(days=ws.INITIAL_LOOKBACK_DAYS))
        self.assertEqual(after, expected)

    def test_parser_upgrade_triggers_re_backfill(self):
        # A log written by an older parser refetches the lookback window so
        # messages the old parser skipped can be recovered (dedup by id).
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"messages": {
            "250": {"timestamp": "2026-07-08T00:00:00+00:00", "trades": []},
        }}
        expected = ws.snowflake_for(now - timedelta(days=ws.INITIAL_LOOKBACK_DAYS))
        self.assertEqual(ws.fetch_after(log, now), expected)

    def test_subsequent_run_resumes_from_newest_logged_id(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"parser_version": ws.PARSER_VERSION, "messages": {
            "100": {"timestamp": "2026-07-01T00:00:00+00:00", "trades": []},
            "250": {"timestamp": "2026-07-08T00:00:00+00:00", "trades": []},
            "175": {"timestamp": "2026-07-05T00:00:00+00:00", "trades": []},
        }}
        self.assertEqual(ws.fetch_after(log, now), 250)


if __name__ == "__main__":
    unittest.main(verbosity=2)
