#!/usr/bin/env bash
# Copy bot state into the `state` branch worktree and push. Never prints secrets.
set -u
cd "${GITHUB_WORKSPACE:-.}"
W=state-wt
[ -d "$W" ] || { echo "save_state: no worktree"; exit 0; }
RUN_DIR="$W/runs/${RUN_TAG:-local}"
mkdir -p "$RUN_DIR"
[ -f demo.log ]   && cp -f demo.log "$RUN_DIR/demo.log"
[ -f stats.json ] && cp -f stats.json "$RUN_DIR/stats.json" && cp -f stats.json "$W/latest_stats.json"
[ -f trades.csv ] && cp -f trades.csv "$W/trades.csv"
python3 scripts/summarize.py trades.csv > "$W/SUMMARY.md" 2>/dev/null || true
cd "$W"
git add -A
if git diff --cached --quiet; then echo "save_state: nothing new"; exit 0; fi
# secret scan of what is about to be committed (prints nothing but a verdict)
if [ -n "${GMGN_API_KEY:-}" ] && git diff --cached | grep -qF -- "$GMGN_API_KEY"; then
  echo "save_state: LEAK DETECTED, refusing to commit"; git reset -q; exit 1
fi
if git diff --cached | grep -qE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----'; then
  echo "save_state: private key pattern detected, refusing to commit"; git reset -q; exit 1
fi
git commit -q -m "state: ${RUN_TAG:-local} $(date '+%Y-%m-%d %H:%M %Z')"
for i in 1 2 3; do
  if git push -q origin HEAD:state; then echo "save_state: pushed"; exit 0; fi
  git pull -q --rebase origin state || true
  sleep 5
done
echo "save_state: push failed"
