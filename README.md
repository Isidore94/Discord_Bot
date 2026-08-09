# Discord_Bot

Two independent GitHub Actions post to Discord.

## Weekly avail check — active

`.github/workflows/avail-check.yml` posts a weekly availability poll every
Sunday at 14:00 UTC via `DISCORD_WEBHOOK_URL`.

## Weekly trade summary — disabled

`.github/workflows/weekly-trade-summary.yml` reads the trade channel, keeps a
running log in `trade_log.json`, and posts a weekly recap. **The schedule is
disabled** — we no longer participate in that Discord, so nothing posts on its
own. The cron trigger is commented out in the workflow, and a manual
`workflow_dispatch` run defaults to preview-only (`--no-post`), writing the
summary to the job summary instead of Discord.

The code, the log, and the tests (`test_weekly_summary.py`,
`test_golden_summary.py`) are all left in place. To re-enable: uncomment the
`schedule:` block in the workflow and flip the `post` input default back to
`true`.
