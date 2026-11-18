# memebot-paper

**Paper-trading simulator only. No wallet, no private key, no real funds, no real transactions.**

A Solana memecoin "sniper" simulator. It reads public market data from GMGN (`gmgn-cli` market/token
commands, read-only API key) and DexScreener, applies safety/activity filters, and records
*simulated* $2 buys and sells with a 3% haircut each way. Nothing is ever bought or sold.

## How it runs (GitHub Actions)
- `.github/workflows/paper-bot.yml` runs on a cron every 5 hours (plus manual `workflow_dispatch`).
- Tests (`pytest`) run first; the bot only starts if they pass.
- Each run trades on paper for ~5h40m (`RUN_MINUTES=340`). In the last 50 minutes it opens no new
  positions; anything still open at the end is paper-closed at the last known price, so no position
  carries over between runs.
- State (cumulative `trades.csv`, `latest_stats.json`, `SUMMARY.md`, per-run `runs/<id>/demo.log` and
  `stats.json`) is committed to the `state` branch every 30 minutes and at the end of each run.
- `concurrency: paper-bot` keeps runs from overlapping.

## Stop it
`gh workflow disable paper-bot.yml -R bigtbusiness-netizen/memebot-paper` (and `gh run cancel <id>` for
a run in progress). Re-enable with `gh workflow enable`.

## Local
```
npm i -g gmgn-cli@1.7.2
pip install -r requirements.txt
python -m pytest -q test_demo.py
GMGN_API_KEY=... RUN_MINUTES=60 python demo.py
```
