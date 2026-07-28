#!/usr/bin/env python3
"""Golden-file regression test for the rendered weekly summary.

Renders a frozen copy of the real trade log at a fixed instant and compares
the result, byte for byte, against golden/summary.md. The unit tests prove
each rule in isolation; this proves the rules still compose into the same
review of the same history -- any change that silently rescoreds old trades
shows up here as a readable diff instead of a surprise in Discord.

When output changes ON PURPOSE (a new rule, a format tweak), regenerate the
expected file and commit it alongside the change, so the diff of summary.md
documents exactly what the change did to real data:

    python3 test_golden_summary.py --regen
"""

import json
import os
import sys
import unittest
from datetime import datetime, timezone

import weekly_summary as ws

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_LOG = os.path.join(HERE, "golden", "trade_log.json")
GOLDEN_SUMMARY = os.path.join(HERE, "golden", "summary.md")

# Frozen: the moment the fixture log was captured. Never advance this --
# ages like "(21d)" in the expected output depend on it.
NOW = datetime(2026, 7, 28, 4, 30, tzinfo=timezone.utc)


def render():
    with open(GOLDEN_LOG, encoding="utf-8") as fh:
        return ws.build_summary(json.load(fh), NOW)


class GoldenSummaryTests(unittest.TestCase):
    maxDiff = None   # show the full diff; that diff is the point of the test

    def test_summary_matches_golden_file(self):
        with open(GOLDEN_SUMMARY, encoding="utf-8") as fh:
            expected = fh.read()
        self.assertEqual(
            render(), expected,
            "\nRendered summary differs from golden/summary.md. If the "
            "change is intentional, run `python3 test_golden_summary.py "
            "--regen` and commit the updated golden file with it.",
        )


if __name__ == "__main__":
    if "--regen" in sys.argv:
        text = render()
        with open(GOLDEN_SUMMARY, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Rewrote {GOLDEN_SUMMARY} "
              f"({len(text)} chars, {len(ws.chunk_message(text))} chunks).")
    else:
        unittest.main(verbosity=2)
