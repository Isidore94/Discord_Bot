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
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 1))

    def test_short_win_is_exit_below_entry(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Short", "CCC", 100.0, False),
            (2, "u", "Exit", "CCC", 90.0, False),    # win: short, exit below
            (3, "u", "Short", "DDD", 100.0, False),
            (4, "u", "Exit", "DDD", 105.0, False),   # loss: short, exit above
        ]))
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 1))

    def test_partials_do_not_score_while_position_is_open(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "NVDA", 200.0, False),
            (2, "u", "Exit", "NVDA", 197.5, True),   # partial, still open
            (3, "u", "Exit", "NVDA", 208.66, True),  # partial, still open
        ]))
        self.assertEqual(wr, {})   # nothing scored until the position closes

    def test_partials_combine_with_close_as_equal_tranches(self):
        trades = self._t([
            (1, "u", "Long", "AAA", 100.0, False),
            (2, "u", "Exit", "AAA", 105.0, True),   # +5% tranche
            (3, "u", "Exit", "AAA", 95.0, True),    # -5% tranche
            (4, "u", "Exit", "AAA", 110.0, False),  # +10% tranche, closes
        ])
        wr = ws.compute_win_rates(trades)
        # One position -> ONE data point: mean(+5, -5, +10)% = +3.33% -> win.
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))
        self.assertAlmostEqual(wr["u"]["pct_sum"], 0.10 / 3)
        self.assertEqual(wr["u"]["pct_n"], 1)
        final = trades[-1]
        self.assertAlmostEqual(final["pct"], 0.10 / 3)
        self.assertEqual(final["partials"], 2)

    def test_losing_tranche_average_scores_one_loss(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "BBB", 100.0, False),
            (2, "u", "Exit", "BBB", 104.0, True),   # +4%
            (3, "u", "Exit", "BBB", 90.0, False),   # -10% -> mean -3% -> loss
        ]))
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (0, 1))

    def test_exit_without_price_is_ignored(self):
        wr = ws.compute_win_rates(self._t([
            (1, "u", "Long", "EEE", 100.0, False),
            (2, "u", "Exit", "EEE", None, False),    # no price -> not scored
        ]))
        self.assertEqual(wr, {})

    def test_win_rate_line_formatting(self):
        self.assertIn("67%", ws._win_rate_line({"week_wins": 2, "week_losses": 1}))
        self.assertIsNone(ws._win_rate_line({"week_wins": 0, "week_losses": 0}))
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
        self.assertEqual(summary.count("**Closed this week**"), 3)
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


class OptionIntegrationTests(unittest.TestCase):
    """Option trades flow through parsing, holdings, and expiration settlement."""

    def _fake_spot(self, mapping):
        """Return a spot_close(ticker, date) backed by a {(ticker, iso): price}."""
        return lambda ticker, d: mapping.get((ticker, d.isoformat()))

    def test_parse_trade_line_recognizes_option(self):
        t = ws.parse_trade_line("#Short put $SPY 400 2026-07-18 @ 3.20",
                                ref_date=datetime(2026, 7, 1).date())
        self.assertEqual(t["instrument"], "option")
        self.assertEqual(t["opt_type"], "put")
        self.assertEqual(t["strike"], 400.0)
        self.assertEqual(t["expiration"], "2026-07-18")
        self.assertEqual(t["premium"], 3.20)
        self.assertEqual(t["price"], 3.20)   # premium mirrored so price helpers work

    def test_option_and_stock_on_same_ticker_are_distinct_holdings(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-06T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long $AAPL 175.00"},
            "2": {"timestamp": "2026-07-06T01:00:00+00:00",
                  "content": "u posted a trade:\n#Long call AAPL 180c 2026-09-18 @ 4.00"},
        }}
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        aapl = holdings["u"]
        self.assertEqual(len(aapl), 2)   # stock did not clobber the option
        self.assertEqual({ws._is_option(t) for t in aapl}, {True, False})

    def test_long_put_above_strike_settles_as_worthless_win(self):
        # #Long put = theta -> worthless above strike is a win.
        holdings = {"u": [{"user": "u", "ticker": "SPY", "side": "Long",
                           "opt_type": "put", "strike": 400.0, "premium": 3.20,
                           "instrument": "option", "expiration": "2026-07-10",
                           "timestamp": "2026-07-06T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": ""}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, self._fake_spot({("SPY", "2026-07-10"): 405.0}))
        self.assertEqual(res["u"][0]["outcome"]["status"], "expired_worthless")
        self.assertTrue(res["u"][0]["outcome"]["win"])
        self.assertNotIn("u", holdings)   # closed out, no longer held

    def test_long_put_below_strike_assigns_and_creates_share_holding(self):
        holdings = {"u": [{"user": "u", "ticker": "TSLA", "side": "Long",
                           "opt_type": "put", "strike": 300.0, "premium": 5.0,
                           "instrument": "option", "expiration": "2026-07-10",
                           "timestamp": "2026-07-06T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": ""}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, self._fake_spot({("TSLA", "2026-07-10"): 260.0}))
        self.assertEqual(res["u"][0]["outcome"]["status"], "assigned")
        # The option is replaced by a long stock position at strike - premium.
        held = holdings["u"]
        self.assertEqual(len(held), 1)
        self.assertFalse(ws._is_option(held[0]))
        self.assertEqual(held[0]["side"], "Long")
        self.assertEqual(held[0]["ticker"], "TSLA")
        self.assertAlmostEqual(held[0]["price"], 295.0)
        self.assertTrue(held[0]["assigned"])

    def test_unexpired_option_is_left_open(self):
        holdings = {"u": [{"user": "u", "ticker": "AAPL", "side": "Long",
                           "opt_type": "put", "strike": 175.0, "premium": 5.5,
                           "instrument": "option", "expiration": "2026-08-15",
                           "timestamp": "2026-07-09T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": ""}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)

        def _boom(ticker, d):  # must not be called for a future expiration
            raise AssertionError("spot lookup called for an unexpired option")

        res = ws.resolve_expired_options(holdings, now, _boom)
        self.assertEqual(res, {})
        self.assertEqual(len(holdings["u"]), 1)   # still held

    def test_build_summary_reports_settled_options_and_folds_win_rate(self):
        log = {"messages": {
            "100": {"timestamp": "2026-07-06T00:00:00+00:00",
                    "content": "isidore94 posted a trade:\n#Long put $SPY 400 7/10 @ 3.20"},
            "101": {"timestamp": "2026-07-06T02:00:00+00:00",
                    "content": "00sav00 posted a trade:\n#Long call NVDA 500c 7/10 for 12.00"},
        }}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        spot = self._fake_spot({("SPY", "2026-07-10"): 405.0,   # theta put win
                                ("NVDA", "2026-07-10"): 480.0})  # long call loss
        summary = ws.build_summary(log, now, spot_close=spot)

        iso = summary.split("## isidore94")[1].split("##")[0]
        self.assertIn("**Options settled this week**", iso)
        self.assertIn("expired worthless — win", iso)
        self.assertIn("100%", iso)   # settled win folded into the quasi win rate

        sav = summary.split("## 00sav00")[1]
        self.assertIn("expired worthless — loss", sav)
        self.assertIn("0%", sav)     # settled loss folded in

    def test_build_summary_without_options_is_unaffected(self):
        # A stock-only log never triggers a spot lookup.
        log = {"messages": {
            "1": {"timestamp": "2026-07-10T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long $PENG 77.15"},
        }}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)

        def _boom(ticker, d):
            raise AssertionError("spot lookup called with no options present")

        summary = ws.build_summary(log, now, spot_close=_boom)
        self.assertNotIn("Options settled", summary)
        self.assertIn("Long **PENG**", summary)


class OptionEarlyExitTests(unittest.TestCase):
    """Options closed before expiration via structured or informal Exit lines."""

    def _log(self, rows):
        # rows: (message_id, iso_timestamp, content)
        return {"messages": {str(mid): {"timestamp": ts, "content": c}
                             for mid, ts, c in rows}}

    def test_structured_exit_parses_as_option(self):
        t = ws.parse_trade_line("#Exit NVDA 500c 8/15 @ 15.00",
                                ref_date=datetime(2026, 7, 1).date())
        self.assertEqual(t["instrument"], "option")
        self.assertEqual(t["side"], "Exit")
        self.assertEqual(t["price"], 15.0)

    def test_structured_exit_closes_the_contract(self):
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "u posted a trade:\n#Long call NVDA 500c 8/15 for 12.00"),
            (2, "2026-07-09T00:00:00+00:00",
             "u posted a trade:\n#Exit NVDA 500c 8/15 @ 15.00"),
        ])
        trades = ws.log_to_trades(log)
        self.assertEqual(ws.compute_holdings(trades), {})
        wr = ws.compute_win_rates(trades)
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))
        self.assertAlmostEqual(wr["u"]["pct_sum"], 0.25)  # 15 vs 12 premium

    def test_theta_put_buyback_below_premium_is_a_win(self):
        # #Long put = theta (sold for premium); buying it back cheaper is a win.
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "u posted a trade:\n#Long put SPY 400p 8/21 @ 3.20"),
            (2, "2026-07-09T00:00:00+00:00",
             "u posted a trade:\n#Exit SPY 400p 8/21 @ 1.10"),
        ])
        wr = ws.compute_win_rates(ws.log_to_trades(log))
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))

    def test_partial_option_exit_keeps_contract_open_unscored(self):
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "u posted a trade:\n#Long call NVDA 500c 8/15 for 12.00"),
            (2, "2026-07-09T00:00:00+00:00",
             "u posted a trade:\n#Exit partial NVDA 500c 8/15 @ 15.00"),
        ])
        trades = ws.log_to_trades(log)
        holdings = ws.compute_holdings(trades)
        held = holdings["u"][0]
        self.assertTrue(ws._is_option(held))
        self.assertEqual(held["partials"], 1)          # tallied on the position
        self.assertAlmostEqual(held["partial_pcts"][0], 0.25)
        self.assertEqual(ws.compute_win_rates(trades), {})  # scores at close

    def test_informal_exit_closes_single_open_option(self):
        # Entry is structured; the exit is a plain '#Exit TICKER price' line.
        # #Long put = theta: sold @ 4.00, bought back @ 2.00 -> a win.
        log = self._log([
            (1, "2026-07-02T00:00:00+00:00",
             "u posted a trade:\n#Long put AMD 160p 9/18 @ 4.00"),
            (2, "2026-07-10T00:00:00+00:00",
             "u posted a trade:\n#Exit AMD at 2.00 cutting it"),
        ])
        trades = ws.log_to_trades(log)
        self.assertEqual(ws.compute_holdings(trades), {})
        wr = ws.compute_win_rates(trades)
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))

    def test_informal_exit_ambiguous_between_two_contracts_is_ignored(self):
        log = self._log([
            (1, "2026-07-02T00:00:00+00:00",
             "u posted a trade:\n#Long put AMD 160p 9/18 @ 4.00"),
            (2, "2026-07-02T01:00:00+00:00",
             "u posted a trade:\n#Long call AMD 180c 9/18 @ 3.00"),
            (3, "2026-07-10T00:00:00+00:00",
             "u posted a trade:\n#Exit AMD at 2.00"),
        ])
        trades = ws.log_to_trades(log)
        holdings = ws.compute_holdings(trades)
        self.assertEqual(len(holdings["u"]), 2)   # both contracts still open
        self.assertEqual(ws.compute_win_rates(trades), {})

    def test_informal_exit_still_prefers_open_stock_position(self):
        # Backward compatibility: with a stock position open on the ticker,
        # a plain exit closes the STOCK, not the option.
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "u posted a trade:\n#Long NVDA 170.00"),
            (2, "2026-07-02T00:00:00+00:00",
             "u posted a trade:\n#Long call NVDA 200c 9/18 @ 5.00"),
            (3, "2026-07-10T00:00:00+00:00",
             "u posted a trade:\n#Exit NVDA at 185.00"),
        ])
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        held = holdings["u"]
        self.assertEqual(len(held), 1)
        self.assertTrue(ws._is_option(held[0]))   # option survived, stock closed


class WheelTests(unittest.TestCase):
    """Covered-call assignment closes tracked shares and realizes their P&L."""

    def _spot(self, mapping):
        return lambda ticker, d: mapping.get((ticker, d.isoformat()))

    def _holdings(self, *trades):
        out = {}
        for t in trades:
            out.setdefault(t["user"], []).append(t)
        return out

    def _stock(self, ticker, price):
        return {"user": "u", "ticker": ticker, "side": "Long", "price": price,
                "partial": False, "instrument": "stock", "notes": "",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "message_id": "1", "index": 0}

    def _short_call(self, ticker, strike, premium, exp):
        return {"user": "u", "ticker": ticker, "side": "Short",
                "opt_type": "call", "strike": strike, "premium": premium,
                "instrument": "option", "expiration": exp, "notes": "",
                "timestamp": "2026-07-06T00:00:00+00:00",
                "message_id": "2", "index": 0}

    def test_covered_call_assignment_closes_shares_and_scores(self):
        holdings = self._holdings(self._stock("MSFT", 430.0),
                                  self._short_call("MSFT", 450.0, 6.0, "2026-07-10"))
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, self._spot({("MSFT", "2026-07-10"): 470.0}))
        oc = res["u"][0]["outcome"]
        self.assertEqual(oc["status"], "assigned")
        self.assertTrue(oc["win"])                       # sold 456 vs 430 entry
        self.assertAlmostEqual(oc["pct"], 26.0 / 430.0)
        self.assertIn("from $430.00 entry", oc["summary"])
        self.assertNotIn("u", holdings)                  # shares closed too

    def test_covered_call_can_lose_vs_basis(self):
        # Called away below the share entry -> scored as a loss.
        holdings = self._holdings(self._stock("MSFT", 460.0),
                                  self._short_call("MSFT", 450.0, 6.0, "2026-07-10"))
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, self._spot({("MSFT", "2026-07-10"): 470.0}))
        self.assertFalse(res["u"][0]["outcome"]["win"])  # 456 < 460

    def test_naked_short_call_assignment_stays_unscored(self):
        holdings = self._holdings(self._short_call("MSFT", 450.0, 6.0, "2026-07-10"))
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, self._spot({("MSFT", "2026-07-10"): 470.0}))
        oc = res["u"][0]["outcome"]
        self.assertEqual(oc["status"], "assigned")
        self.assertIsNone(oc["win"])


class StatsAndMarksTests(unittest.TestCase):
    def test_win_rate_line_is_weekly_with_avg(self):
        line = ws._win_rate_line({"week_wins": 2, "week_losses": 1,
                                  "week_pct_sum": 0.10, "week_pct_n": 3})
        self.assertIn("Win rate this week: **67%** (2W–1L)", line)
        self.assertIn("avg +3.3%/trade", line)

    def test_win_rate_line_none_when_nothing_closed_this_week(self):
        # All-time activity but nothing closed THIS week -> no line.
        self.assertIsNone(ws._win_rate_line(
            {"wins": 9, "losses": 1, "week_wins": 0, "week_losses": 0}))

    def test_win_rate_line_tolerates_minimal_stats(self):
        line = ws._win_rate_line({"week_wins": 2, "week_losses": 1})
        self.assertIn("67%", line)
        self.assertNotIn("avg", line)

    def test_exits_annotated_with_pct_and_held_days(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long AAA 100.00"},
            "2": {"timestamp": "2026-07-09T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit AAA 110.00"},
        }}
        trades = ws.log_to_trades(log)
        ws.compute_win_rates(trades)
        exit_t = [t for t in trades if t["side"] == "Exit"][0]
        self.assertAlmostEqual(exit_t["pct"], 0.10)
        self.assertEqual(exit_t["held_days"], 8)
        self.assertIn("(+10.0%, swing 8d)", ws._closed_line(exit_t))

    def test_marks_annotate_open_positions(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-08T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long $PENG 77.15"},
            "2": {"timestamp": "2026-07-08T01:00:00+00:00",
                  "content": "u posted a trade:\n#Short put SPY 400p 7/17 @ 3.20"},
        }}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(
            log, now, spot_close=lambda tk, d: None,
            last_close=lambda tks: {"PENG": 82.3, "SPY": 402.3})
        self.assertIn("→ 82.3 (+6.7%)", summary)          # stock mark-to-market
        self.assertIn("(spot 402.3)", summary)            # option underlying
        self.assertIn("⏳ expires this week", summary)    # 7/17 within 7 days

    def test_no_last_close_means_no_marks(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-08T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long $PENG 77.15"},
        }}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertNotIn("→", summary.split("**Open trades**")[1])

    def test_short_position_mark_is_inverted(self):
        line = ws._open_line(
            {"user": "u", "ticker": "AAPL", "side": "Short", "price": 200.0,
             "partial": False, "instrument": "stock", "notes": "",
             "timestamp": "2026-07-08T00:00:00+00:00"},
            marks={"AAPL": 190.0})
        self.assertIn("(+5.0%)", line)   # short: price falling is a gain

    def test_expired_but_unsettled_option_flagged(self):
        line = ws._open_line(
            {"user": "u", "ticker": "SPY", "side": "Long", "opt_type": "put",
             "strike": 400.0, "premium": 3.2, "instrument": "option",
             "expiration": "2026-07-10", "notes": "",
             "timestamp": "2026-07-06T00:00:00+00:00"},
            now=datetime(2026, 7, 12, tzinfo=timezone.utc))
        self.assertIn("_(awaiting settlement)_", line)


class AddFunctionTests(unittest.TestCase):
    """The channel's #Add function, real formats from the trades channel."""

    def _log(self, rows):
        return {"messages": {str(mid): {"timestamp": ts, "content": c}
                             for mid, ts, c in rows}}

    def test_add_with_new_avg_replaces_entry_price(self):
        # "#add Long ALAB at 429.54. New avg: 449.76" (real message)
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "p posted a trade:\n#Long ALAB 470.00"),
            (2, "2026-07-09T00:00:00+00:00",
             "p posted a trade:\n#add Long ALAB at 429.54. New avg: 449.76"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["p"][0]
        self.assertAlmostEqual(held["price"], 449.76)   # New avg is authoritative
        self.assertEqual(held["adds"], 2)
        self.assertEqual(held["timestamp"], "2026-07-01T00:00:00+00:00")
        self.assertIn("(avg of 2)", ws._open_line(held))

    def test_add_without_own_price_still_uses_new_avg(self):
        # "#Add Long ALAB. New avg: 438.97" (real message, no add price)
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "p posted a trade:\n#Long ALAB 470.00"),
            (2, "2026-07-09T00:00:00+00:00",
             "p posted a trade:\n#Add Long ALAB. New avg: 438.97"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["p"][0]
        self.assertAlmostEqual(held["price"], 438.97)

    def test_add_without_new_avg_averages_equal_weight(self):
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "p posted a trade:\n#Long PENG 77.15"),
            (2, "2026-07-09T00:00:00+00:00",
             "p posted a trade:\n#Add PENG 80.00"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["p"][0]
        self.assertAlmostEqual(held["price"], 78.575)

    def test_untracked_add_opens_a_long(self):
        log = self._log([
            (1, "2026-07-09T00:00:00+00:00",
             "p posted a trade:\n#add Long ALAB at 429.54. New avg: 449.76"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["p"][0]
        self.assertEqual(held["side"], "Long")
        self.assertAlmostEqual(held["price"], 449.76)

    def test_win_rate_scores_against_new_avg(self):
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "p posted a trade:\n#Long ALAB 470.00"),
            (2, "2026-07-09T00:00:00+00:00",
             "p posted a trade:\n#Add Long ALAB. New avg: 438.97"),
            (3, "2026-07-10T00:00:00+00:00",
             "p posted a trade:\n#Exit ALAB 450.00"),
        ])
        wr = ws.compute_win_rates(ws.log_to_trades(log))
        # 450 vs New avg 438.97 -> win (vs a loss against the original 470)
        self.assertEqual((wr["p"]["wins"], wr["p"]["losses"]), (1, 0))

    def test_repeat_long_refreshes_instead_of_averaging(self):
        # Scale-ins go through #Add; a repeat #Long just refreshes the position.
        log = self._log([
            (1, "2026-06-20T00:00:00+00:00",
             "u posted a trade:\n#Long PENG 77.15"),
            (2, "2026-07-08T00:00:00+00:00",
             "u posted a trade:\n#Long PENG 80.00"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["u"][0]
        self.assertEqual(held["price"], 80.0)
        self.assertEqual(held.get("adds", 1), 1)

    def test_opposite_side_reentry_replaces_position(self):
        log = self._log([
            (1, "2026-07-01T00:00:00+00:00",
             "u posted a trade:\n#Long AAA 100.00"),
            (2, "2026-07-08T00:00:00+00:00",
             "u posted a trade:\n#Short AAA 110.00"),
        ])
        held = ws.compute_holdings(ws.log_to_trades(log))["u"][0]
        self.assertEqual(held["side"], "Short")
        self.assertEqual(held["price"], 110.0)

    def test_add_line_parses_new_avg(self):
        t = ws.parse_trade_line("#add Long ALAB at 429.54. New avg: 449.76")
        self.assertEqual(t["side"], "Add")
        self.assertEqual(t["ticker"], "ALAB")
        self.assertEqual(t["price"], 429.54)
        self.assertEqual(t["new_avg"], 449.76)


class RealWorldExitTests(unittest.TestCase):
    """Free-text partial hints and stated-% returns, from real channel posts."""

    def test_partial_detected_from_notes(self):
        t = ws.parse_trade_line(
            "#Exit NVDA 203.85 for 5.50 partial profit swinging the rest.")
        self.assertTrue(t["partial"])
        self.assertEqual(t["price"], 203.85)
        self.assertNotIn("stated_pct", t)   # "5.50 partial" is $/share, not %

    def test_exit_half_of_with_stated_percent(self):
        t = ws.parse_trade_line(
            "#Exit half of RIVN for 50% going to leave the rest to see if I "
            "can get a free lunch on this lunatic")
        self.assertEqual(t["ticker"], "RIVN")
        self.assertTrue(t["partial"])
        self.assertIsNone(t["price"])          # "50%" is a return, not a price
        self.assertAlmostEqual(t["stated_pct"], 0.50)

    def test_exit_this_add_on_long_is_partial(self):
        t = ws.parse_trade_line(
            "#Exit this add on Long ALAB for a small 3 dollar loss.")
        self.assertEqual(t["ticker"], "ALAB")
        self.assertTrue(t["partial"])

    def test_leaving_size_on_is_partial_with_stated_percent(self):
        t = ws.parse_trade_line(
            "#Exit RIVN for 100% and and leaving quarter size on.")
        self.assertEqual(t["ticker"], "RIVN")
        self.assertTrue(t["partial"])
        self.assertAlmostEqual(t["stated_pct"], 1.0)

    def test_negative_stated_percent(self):
        t = ws.parse_trade_line("#Exit NVDA 11.70 for -32% on calls")
        self.assertFalse(t["partial"])
        self.assertAlmostEqual(t["stated_pct"], -0.32)

    def test_fraction_size_is_not_read_as_price(self):
        # "CROX 1/2 at $133.46" -> "1/2" is the size sold, NOT a $1 price.
        # (Real bug: it produced a fabricated -99% loss.)
        t = ws.parse_trade_line("#exit CROX 1/2 at $133.46 - 2.26 gain")
        self.assertEqual(t["ticker"], "CROX")
        self.assertIsNone(t["price"])   # never $1

    def test_plain_full_exits_stay_full(self):
        for line in ("#Exit CRWD at 187.60",
                     "#Exit GOOGL @ 368.88",
                     "#Exit AMD at 2.00 cutting it",
                     # incidental commentary must not flip a close to partial
                     "#Exit AAPL at 200 still bullish long term",
                     "#Exit CRWD 187.60 keeping an eye on re-entry"):
            self.assertFalse(ws.parse_trade_line(line)["partial"], line)

    def test_ticker_colliding_with_filler_word_still_parses(self):
        t = ws.parse_trade_line("#Long ON 45.00")
        self.assertEqual(t["ticker"], "ON")
        self.assertEqual(t["price"], 45.0)

    def test_stated_percent_scores_priceless_partials(self):
        # RIVN: two price-less partials with stated returns; still open.
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "m posted a trade:\n#Long RIVN 10.00"},
            "2": {"timestamp": "2026-07-09T00:00:00+00:00",
                  "content": ("m posted a trade:\n#Exit half of RIVN for 50% "
                              "going to leave the rest")},
            "3": {"timestamp": "2026-07-09T09:00:00+00:00",
                  "content": ("m posted a trade:\n#Exit RIVN for 100% and "
                              "and leaving quarter size on.")},
        }}
        trades = ws.log_to_trades(log)
        held = ws.compute_holdings(trades)["m"][0]
        self.assertEqual(held["partials"], 2)
        self.assertEqual(held["partial_pcts"], [0.50, 1.0])
        self.assertEqual(ws.compute_win_rates(trades), {})  # still open

        # A final close combines: mean(+50%, +100%, +20%) -> one win.
        log["messages"]["4"] = {
            "timestamp": "2026-07-10T00:00:00+00:00",
            "content": "m posted a trade:\n#Exit RIVN 12.00"}
        trades = ws.log_to_trades(log)
        wr = ws.compute_win_rates(trades)
        self.assertEqual((wr["m"]["wins"], wr["m"]["losses"]), (1, 0))
        self.assertAlmostEqual(wr["m"]["pct_sum"], (0.5 + 1.0 + 0.2) / 3)
        final = [t for t in trades if not t["partial"] and t["side"] == "Exit"][0]
        self.assertEqual(final["partials"], 2)
        self.assertIn("2 partials", ws._closed_line(final))

    def test_expiry_settlement_blends_partial_tranches(self):
        # Long call, partial exit at +25%, remainder expires worthless (-100%):
        # mean(-37.5%) -> the settled outcome flips to a loss.
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long call NVDA 500c 7/10 for 12.00"},
            "2": {"timestamp": "2026-07-08T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit partial NVDA 500c 7/10 @ 15.00"},
        }}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        res = ws.resolve_expired_options(
            holdings, now, lambda tk, d: 480.0)   # OTM at expiry
        oc = res["u"][0]["outcome"]
        self.assertFalse(oc["win"])
        self.assertAlmostEqual(oc["pct"], (0.25 - 1.0) / 2)
        self.assertIn("1 earlier partial", oc["summary"])


class ReviewFixTests(unittest.TestCase):
    """Regressions for the confirmed code-review findings."""

    def test_assigned_share_sale_scores_vs_basis_not_premium(self):
        # #Long put (theta) assigns at basis 295; a later plain '#Exit TSLA 260'
        # is the SHARE sale: it must score (260-295)/295, never 260-vs-premium.
        log = {"messages": {
            "1": {"timestamp": "2026-07-14T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long put TSLA 300p 7/17 @ 5.00"},
            "2": {"timestamp": "2026-07-18T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit TSLA 260"},
        }}
        # The raw replay must not produce the old -5100% garbage point.
        self.assertEqual(ws.compute_win_rates(ws.log_to_trades(log)), {})
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: 260.0)
        self.assertIn("-11.9%", summary)          # (260-295)/295 via settlement
        self.assertNotIn("5100", summary)
        # Shares were sold, so nothing is left open.
        self.assertIn("- _none_", summary.split("**Open trades**")[1])

    def test_exit_after_expiry_never_matches_the_dead_contract(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-06T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long put TSLA 300p 7/10 @ 5.00"},
            "2": {"timestamp": "2026-07-20T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit TSLA 260"},
        }}
        trades = ws.log_to_trades(log)
        holdings = ws.compute_holdings(trades)
        self.assertEqual(len(holdings["u"]), 1)   # the put is still tracked
        self.assertTrue(ws._is_option(holdings["u"][0]))

    def test_stated_pct_beats_price_on_informal_option_exit(self):
        # '#Exit NVDA 11.70 for -32% on calls': 11.70 is not a premium; the
        # stated -32% must win, not (11.70-5)/5 = +134%.
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long call NVDA 200c 9/18 @ 5.00"},
            "2": {"timestamp": "2026-07-09T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit NVDA 11.70 for -32% on calls"},
        }}
        wr = ws.compute_win_rates(ws.log_to_trades(log))
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (0, 1))
        self.assertAlmostEqual(wr["u"]["pct_sum"], -0.32)

    def test_yearless_exit_days_after_expiry_matches_open_contract(self):
        log = {"messages": {
            "1": {"timestamp": "2027-01-10T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long call NVDA 500c 1/16 @ 5.00"},
            "2": {"timestamp": "2027-01-20T00:00:00+00:00",   # 4 days post-expiry
                  "content": "u posted a trade:\n#Exit NVDA 500c 1/16 @ 8.00"},
        }}
        trades = ws.log_to_trades(log)
        self.assertEqual(ws.compute_holdings(trades), {})   # exit matched
        wr = ws.compute_win_rates(trades)
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))

    def test_same_side_reopen_carries_banked_partials(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long AAA 100.00"},
            "2": {"timestamp": "2026-07-02T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit partial AAA 110.00"},
            "3": {"timestamp": "2026-07-03T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long AAA 105.00"},   # refresh
            "4": {"timestamp": "2026-07-04T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit AAA 100.00"},
        }}
        wr = ws.compute_win_rates(ws.log_to_trades(log))
        # One data point: mean(+10%, (100-105)/105) -> small win, not a loss.
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))
        self.assertAlmostEqual(wr["u"]["pct_sum"], (0.10 + (100 - 105) / 105) / 2)

    def test_side_flip_scores_pending_partials(self):
        log = {"messages": {
            "1": {"timestamp": "2026-07-01T00:00:00+00:00",
                  "content": "u posted a trade:\n#Long AAA 100.00"},
            "2": {"timestamp": "2026-07-02T00:00:00+00:00",
                  "content": "u posted a trade:\n#Exit partial AAA 110.00"},
            "3": {"timestamp": "2026-07-03T00:00:00+00:00",
                  "content": "u posted a trade:\n#Short AAA 120.00"},   # flip
        }}
        wr = ws.compute_win_rates(ws.log_to_trades(log))
        # The long trade ended at the flip; its banked +10% partial scores.
        self.assertEqual((wr["u"]["wins"], wr["u"]["losses"]), (1, 0))
        self.assertAlmostEqual(wr["u"]["pct_sum"], 0.10)

    def test_assignment_blends_earlier_partials(self):
        holdings = {"u": [{"user": "u", "ticker": "SPY", "side": "Long",
                           "opt_type": "put", "strike": 400.0, "premium": 3.2,
                           "instrument": "option", "expiration": "2026-07-10",
                           "timestamp": "2026-07-06T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": "",
                           "partials": 1, "partial_pcts": [0.30]}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(holdings, now, lambda tk, d: 390.0)
        oc = res["u"][0]["outcome"]
        self.assertEqual(oc["status"], "assigned")
        self.assertTrue(oc["win"])                 # banked +30% tranche scores
        self.assertAlmostEqual(oc["pct"], 0.30)
        self.assertIn("1 earlier partial", oc["summary"])

    def test_prune_protects_adds_and_partials_of_open_positions(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        old = "2025-01-01T00:00:00+00:00"   # far past RETENTION_DAYS
        log = {"messages": {
            "1": {"timestamp": old,
                  "content": "u posted a trade:\n#Long PENG 77.15"},
            "2": {"timestamp": old,
                  "content": "u posted a trade:\n#Add PENG 80.00"},
            "3": {"timestamp": old,
                  "content": "u posted a trade:\n#Exit partial PENG 90.00"},
        }}
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        ws.prune_log(log, holdings, now)
        # Open + add + partial all survive while the position is held.
        self.assertEqual(set(log["messages"]), {"1", "2", "3"})


class WeeklyGroupingTests(unittest.TestCase):
    """Same-week Long/Short-then-full-Exit round trips render as one line."""

    def _log(self, rows):
        return {"messages": {str(mid): {"timestamp": ts, "content": c}
                             for mid, ts, c in rows}}

    def _closed(self, summary):
        return summary.split("**Closed this week**")[1].split("**Open")[0]

    def test_same_week_round_trip_is_one_closed_line(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Short BMNR 14.35"),
            (2, "2026-07-06T01:00:00+00:00",
             "u posted a trade:\n#Exit BMNR b/e"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        sec = self._closed(ws.build_summary(log, now, spot_close=lambda tk, d: None))
        self.assertEqual(sec.count("BMNR"), 1)   # one round-trip line, not two
        self.assertIn("**BMNR**: Short @ 14.35 → Exit", sec)
        self.assertIn("b/e", sec)

    def test_scored_round_trip_shows_pct_and_day_trade(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Long HPE 48.9"),
            (2, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Exit HPE 49.7"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertIn("**HPE**: Long @ 48.9 → Exit @ 49.7 (+1.6%, day trade)",
                      summary)

    def test_swing_label_for_multi_day_hold(self):
        log = self._log([
            (1, "2026-07-04T00:00:00+00:00",
             "u posted a trade:\n#Long OKTA 131.4"),
            (2, "2026-07-11T00:00:00+00:00",
             "u posted a trade:\n#Exit OKTA 145.25"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertIn("swing 7d", summary)
        self.assertNotIn("day trade", summary)

    def test_exit_of_a_pre_existing_position_is_a_closed_line(self):
        # Opened before the week; the exit still renders the full round trip
        # (entry stamped by compute_win_rates) and is labeled a swing.
        log = self._log([
            (1, "2026-05-01T00:00:00+00:00",
             "u posted a trade:\n#Long VRT 300.00"),
            (2, "2026-07-11T00:00:00+00:00",
             "u posted a trade:\n#Exit VRT 330.00"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        sec = self._closed(ws.build_summary(log, now, spot_close=lambda tk, d: None))
        self.assertIn("**VRT**: Long @ 300 → Exit @ 330", sec)
        self.assertIn("swing", sec)

    def test_open_position_does_not_appear_in_closed_section(self):
        # A position opened this week and still open shows ONLY under Open
        # trades -- never duplicated in "Closed this week" (the DELL complaint).
        log = self._log([
            (1, "2026-07-10T00:00:00+00:00",
             "u posted a trade:\n#Long DLO 15.02"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        closed = self._closed(summary)
        self.assertIn("_no closed trades this week_", closed)
        self.assertNotIn("DLO", closed)
        self.assertIn("Long **DLO** @ 15.02", summary.split("**Open trades**")[1])

    def test_partial_of_open_position_is_not_in_closed_section(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Long AAA 100.00"),
            (2, "2026-07-07T00:00:00+00:00",
             "u posted a trade:\n#Exit partial AAA 110.00"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertIn("_no closed trades this week_", self._closed(summary))
        # The trim is summarized on the open position instead.
        self.assertIn("trimmed 1× (+10%)", summary.split("**Open trades**")[1])

    def test_add_is_not_shown_in_closed_section(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Long ALAB 470.00"),
            (2, "2026-07-07T00:00:00+00:00",
             "u posted a trade:\n#Add Long ALAB. New avg: 450.00"),
            (3, "2026-07-08T00:00:00+00:00",
             "u posted a trade:\n#Exit ALAB 460.00"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        sec = self._closed(ws.build_summary(log, now, spot_close=lambda tk, d: None))
        self.assertNotIn("Add", sec)                         # add not shown here
        self.assertIn("**ALAB**: Long @ 450 → Exit @ 460", sec)  # closed vs new avg

    def test_option_round_trip_is_one_closed_line(self):
        log = self._log([
            (1, "2026-07-09T00:00:00+00:00",
             "u posted a trade:\n#Long call NVDA 500c 8/15 for 12.00"),
            (2, "2026-07-11T00:00:00+00:00",
             "u posted a trade:\n#Exit NVDA 500c 8/15 @ 15.00"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        sec = self._closed(ws.build_summary(log, now, spot_close=lambda tk, d: None))
        self.assertEqual(sec.count("NVDA"), 1)
        self.assertIn("$12.00 → $15.00", sec)
        self.assertIn("+25.0%", sec)


class SpreadIntegrationTests(unittest.TestCase):
    """Multi-leg spreads are tracked and scored as theta/credit plays."""

    def _log(self, rows):
        return {"messages": {str(mid): {"timestamp": ts, "content": c}
                             for mid, ts, c in rows}}

    def test_spread_exited_for_stated_percent_scores_as_win(self):
        log = self._log([
            (1, "2026-07-08T00:00:00+00:00",
             "u posted a trade:\n#Long CRWV via 97/96 (Jul 26) PCS for .25c credit"),
            (2, "2026-07-10T00:00:00+00:00",
             "u posted a trade:\n#Exit CRWV 97/96 for +67%"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertIn("Spread **CRWV 97/96 PCS", summary)
        self.assertIn("+67.0%", summary)
        wr = ws.compute_win_rates(ws.log_to_trades(log), week_start=now - timedelta(days=7))
        self.assertEqual((wr["u"]["week_wins"], wr["u"]["week_losses"]), (1, 0))

    def test_spread_settles_worthless_above_first_strike(self):
        # Held to expiry, spot above the first strike -> a win.
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Long SPY via 400/395 (Jul 10) PCS for .50c credit"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            ws.compute_holdings(ws.log_to_trades(log)), now,
            lambda tk, d: 410.0)   # above 400
        oc = res["u"][0]["outcome"]
        self.assertTrue(oc["win"])
        self.assertIn("worthless", oc["summary"])

    def test_debit_spread_buyback_below_debit_is_a_loss(self):
        # Call debit spread: pay 0.80 debit, sell (buy back) at 0.55 -> loss.
        log = self._log([
            (1, "2026-07-09T00:00:00+00:00",
             "u posted a trade:\n#long RKLB 100/112 cds $0.8"),
            (2, "2026-07-11T00:00:00+00:00",
             "u posted a trade:\n#exit RKLB 100/112 cds @ $0.55"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(log, now, spot_close=lambda tk, d: None)
        self.assertIn("Spread **RKLB 100/112 CDS", summary)
        self.assertIn("-31.2%", summary)   # (0.55-0.80)/0.80, a loss
        wr = ws.compute_win_rates(ws.log_to_trades(log),
                                  week_start=now - timedelta(days=7))
        self.assertEqual((wr["u"]["week_wins"], wr["u"]["week_losses"]), (0, 1))

    def test_pds_settles_bearish_below_first_strike(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Short AKAM PDS 124/122 (Jul 10) for 90c"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        below = ws.resolve_expired_options(dict(holdings), now, lambda tk, d: 118.0)
        above = ws.resolve_expired_options(dict(holdings), now, lambda tk, d: 130.0)
        self.assertTrue(below["u"][0]["outcome"]["win"])    # bearish: below wins
        self.assertFalse(above["u"][0]["outcome"]["win"])

    def test_spread_below_first_strike_settles_as_loss_no_shares(self):
        log = self._log([
            (1, "2026-07-06T00:00:00+00:00",
             "u posted a trade:\n#Long SPY via 400/395 (Jul 10) PCS for .50c credit"),
        ])
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        holdings = ws.compute_holdings(ws.log_to_trades(log))
        res = ws.resolve_expired_options(holdings, now, lambda tk, d: 390.0)
        self.assertFalse(res["u"][0]["outcome"]["win"])
        self.assertNotIn("u", holdings)   # a spread never leaves shares behind


class OptionDirectionTests(unittest.TestCase):
    """#Long put = theta, #Short put = directional (labels inverted for puts)."""

    def _log(self, rows):
        return {"messages": {str(mid): {"timestamp": ts, "content": c}
                             for mid, ts, c in rows}}

    def test_directional_short_put_worthless_is_a_loss(self):
        # #Short put = bought put; expiring above strike loses the premium.
        holdings = {"u": [{"user": "u", "ticker": "SPY", "side": "Short",
                           "opt_type": "put", "strike": 400.0, "premium": 3.20,
                           "instrument": "option", "expiration": "2026-07-10",
                           "timestamp": "2026-07-06T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": ""}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, lambda tk, d: 405.0)   # above strike
        oc = res["u"][0]["outcome"]
        self.assertEqual(oc["status"], "expired_worthless")
        self.assertFalse(oc["win"])

    def test_directional_short_put_itm_is_a_win_not_assignment(self):
        holdings = {"u": [{"user": "u", "ticker": "SPY", "side": "Short",
                           "opt_type": "put", "strike": 400.0, "premium": 3.20,
                           "instrument": "option", "expiration": "2026-07-10",
                           "timestamp": "2026-07-06T00:00:00+00:00",
                           "message_id": "1", "index": 0, "notes": ""}]}
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        res = ws.resolve_expired_options(
            holdings, now, lambda tk, d: 380.0)   # below strike
        oc = res["u"][0]["outcome"]
        self.assertEqual(oc["status"], "exercised")
        self.assertTrue(oc["win"])
        self.assertNotIn("u", holdings)   # no shares assigned to a bought put

    def test_theta_long_put_and_directional_short_put_score_oppositely(self):
        # Same contract, both expiring worthless: theta wins, directional loses.
        base = "u posted a trade:\n#{} put SPY 400p 7/10 @ 3.20"
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        lp = self._log([(1, "2026-07-06T00:00:00+00:00", base.format("Long"))])
        sp = self._log([(1, "2026-07-06T00:00:00+00:00", base.format("Short"))])
        lsum = ws.build_summary(lp, now, spot_close=lambda tk, d: 405.0)
        ssum = ws.build_summary(sp, now, spot_close=lambda tk, d: 405.0)
        self.assertIn("100%", lsum)   # theta worthless -> win
        self.assertIn("0%", ssum)     # directional worthless -> loss


class DateFirstOptionRegressionTests(unittest.TestCase):
    """A date-first option post must never leak its date into a fake stock
    price and produce a bogus mark-to-market percentage (real symptom: a
    channel screenshot showing 'BE @ 7 -> 244.61 (+3394.4%)')."""

    def test_date_first_option_is_not_priced_like_a_stock(self):
        log = {"messages": {
            "1": {"timestamp": "2026-06-30T14:48:35+00:00",
                  "content": "u posted a trade:\n#Long BE 07/10/2026 235.00 P 4.9"},
        }}
        trades = ws.log_to_trades(log)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["instrument"], "option")
        self.assertEqual(t["strike"], 235.0)
        self.assertNotEqual(t.get("price"), 7.0)

        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        summary = ws.build_summary(
            log, now, spot_close=lambda tk, d: None,
            last_close=lambda tks: {"BE": 244.61})
        self.assertNotIn("@ 7", summary)
        self.assertNotIn("3394", summary)
        self.assertIn("BE $235p", summary)


class SpotCacheTests(unittest.TestCase):
    def _option(self, exp):
        return {"user": "u", "ticker": "SPY", "side": "Long", "opt_type": "put",
                "strike": 400.0, "premium": 3.2, "instrument": "option",
                "expiration": exp, "notes": "",
                "timestamp": "2026-07-06T00:00:00+00:00",
                "message_id": "1", "index": 0}

    def test_cached_spot_skips_fetch(self):
        calls = []

        def spot(ticker, d):
            calls.append(ticker)
            return 405.0

        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        cache = {"SPY:2026-07-10": 405.0}
        holdings = {"u": [self._option("2026-07-10")]}
        res = ws.resolve_expired_options(holdings, now, spot, cache=cache)
        self.assertEqual(calls, [])   # served from cache
        self.assertTrue(res["u"][0]["outcome"]["win"])

    def test_fetched_spot_is_written_to_cache(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        cache = {}
        holdings = {"u": [self._option("2026-07-10")]}
        ws.resolve_expired_options(holdings, now, lambda tk, d: 405.0, cache=cache)
        self.assertEqual(cache, {"SPY:2026-07-10": 405.0})

    def test_prune_drops_stale_and_malformed_cache_entries(self):
        now = datetime(2026, 7, 12, tzinfo=timezone.utc)
        log = {"messages": {}, "spot_cache": {
            "SPY:2026-07-10": 405.0,     # fresh -> kept
            "OLD:2020-01-17": 100.0,     # past retention -> dropped
            "garbage": 1.0,              # malformed -> dropped
        }}
        ws.prune_log(log, {}, now)
        self.assertEqual(log["spot_cache"], {"SPY:2026-07-10": 405.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
