# memebot-paper

**Paper-trading simulator only. No wallet, no private key, no real funds, no real transactions.**

A Solana memecoin "sniper" simulator. It reads public market data from GMGN (`gmgn-cli` market/token
commands, read-only API key) and DexScreener, applies safety/activity filters, and records
*simulated* $2 buys and sells with a 3% haircut each way. Nothing is ever bought or sold.

## How it runs (GitHub Actions)
- `.github/workflows/paper-bot.yml` runs back-to-back: when a run finishes successfully, its `relaunch` job
  starts the next one (`gh workflow run`, GITHUB_TOKEN with `actions: write`). A cron every 5 hours is only a
  backup. No relaunch if the run was cancelled or failed, the workflow is disabled, the repo variable
  `AUTO_RESTART` is `off`, or another run is already queued.
- Tests (`pytest`) run first; the bot only starts if they pass.
- Each run trades on paper for ~5h40m (`RUN_MINUTES=340`). In the last 50 minutes it opens no new
  positions; anything still open at the end is paper-closed at the last known price, so no position
  carries over between runs.
- State (cumulative `trades.csv`, `latest_stats.json`, `SUMMARY.md`, per-run `runs/<id>/demo.log` and
  `stats.json`) is committed to the `state` branch every 30 minutes and at the end of each run.
- `concurrency: paper-bot` keeps runs from overlapping.

## Strategy v2 (from 2026-11-19)
- Hard market-cap cap: never buys above `max_mcap` ($5,000) — checked on the GMGN scan, at watch time, on
  every re-check and at the moment of the paper buy (no tolerance band). v1 had `max_mcap` 15000 (+10%).
- Survival wait: a candidate is watched and re-checked every 30s; it is bought only after >= 90s and >= 2
  healthy re-checks, and once the coin is >= 3 minutes old. Each check requires: 5m buys/volume/buy-sell ratio
  still healthy, >= 3 buys in the last minute, 1m buy volume not outweighed by sells, 1m buy rate not collapsed
  vs the 5m average at watch time, price not > 35% below its peak (GMGN `ath_price` + every observed price) and
  not > 12% below the watch price, and the dev hasn't sold (GMGN `dev.creator_token_status`,
  `dev.creator_token_balance`, `stat.creator_hold_rate`; > 20% drop since watch = sold).
- `trades.csv` has a `MARKER` row where v2 started; `SUMMARY.md` shows v2 and all-time stats separately.

## Stop it
`gh workflow disable paper-bot.yml -R bigtbusiness-netizen/memebot-paper` (and `gh run cancel <id>` for
a run in progress). Re-enable with `gh workflow enable`. To keep cron but stop the back-to-back chain:
`gh variable set AUTO_RESTART --body off -R bigtbusiness-netizen/memebot-paper`.

## Local
```
npm i -g gmgn-cli@1.7.2
pip install -r requirements.txt
python -m pytest -q test_demo.py
GMGN_API_KEY=... RUN_MINUTES=60 python demo.py
```
