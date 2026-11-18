#!/usr/bin/env python3
"""Cumulative paper P&L from trades.csv (all runs). Prints Markdown."""
import csv, sys, time
path = sys.argv[1] if len(sys.argv) > 1 else "trades.csv"
buys = closes = wins = 0
pnl_by_mint, realized = {}, 0.0
try:
    rows = list(csv.DictReader(open(path)))
except FileNotFoundError:
    rows = []
for r in rows:
    a = r.get("action", "")
    if a == "BUY":
        buys += 1
    elif a.startswith("SELL"):
        try:
            p = float(r.get("pnl_usd") or 0)
        except ValueError:
            p = 0.0
        realized += p
        pnl_by_mint[r["mint"]] = pnl_by_mint.get(r["mint"], 0.0) + p
        if a == "SELL":
            closes += 1
wins = sum(1 for v in pnl_by_mint.values() if v > 0)
print("# Paper bot summary (all runs, simulated only)\n")
print(f"- updated: {time.strftime('%Y-%m-%d %H:%M %Z')}")
print(f"- paper buys: {buys}  | fully closed: {closes}")
print(f"- realized paper P&L: ${realized:+.2f} (stake $2/trade)")
if pnl_by_mint:
    print(f"- winners: {wins}/{len(pnl_by_mint)} ({wins/len(pnl_by_mint)*100:.0f}%)")
print("\nLast 15 trade rows:\n")
print("| time | action | symbol | reason | usd | pnl |\n|---|---|---|---|---|---|")
for r in rows[-15:]:
    print(f"| {r.get('time')} | {r.get('action')} | {r.get('symbol')} | {(r.get('reason') or '')[:40]} | "
          f"{r.get('usd_value')} | {r.get('pnl_usd')} |")
