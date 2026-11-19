#!/usr/bin/env python3
"""Paper P&L from trades.csv (all runs). Prints Markdown: all-time, plus per strategy version.
A row with action=MARKER (symbol = version, e.g. v2) marks where a new version started; every BUY after
it, and the SELLs of those buys, count toward that version. History rows are never rewritten."""
import csv, sys, time
path = sys.argv[1] if len(sys.argv) > 1 else "trades.csv"
try:
    rows = list(csv.DictReader(open(path)))
except FileNotFoundError:
    rows = []


def stats(rs):
    buys = closes = 0
    pnl_by_mint, realized = {}, 0.0
    for r in rs:
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
    return {"buys": buys, "closes": closes, "realized": realized, "wins": wins, "n": len(pnl_by_mint)}


# tag each row with the version active when its BUY happened
version, buy_version, by_ver, markers = "v1", {}, {}, []
for r in rows:
    a = r.get("action", "")
    if a == "MARKER":
        version = r.get("symbol") or version
        markers.append((version, r.get("time")))
        continue
    if a == "BUY":
        buy_version[r.get("mint")] = version
    v = buy_version.get(r.get("mint"), version)
    by_ver.setdefault(v, []).append(r)


def lines(title, s):
    out = [f"## {title}", f"- paper buys: {s['buys']}  | fully closed: {s['closes']}",
           f"- realized paper P&L: ${s['realized']:+.2f} (stake $2/trade)"]
    if s["n"]:
        out.append(f"- winners: {s['wins']}/{s['n']} ({s['wins']/s['n']*100:.0f}%)")
    return out


trade_rows = [r for r in rows if r.get("action") != "MARKER"]
print("# Paper bot summary (simulated only)\n")
print(f"- updated: {time.strftime('%Y-%m-%d %H:%M %Z')}")
for v, t in markers:
    print(f"- {v} started: {t}")
print()
latest = markers[-1][0] if markers else None
if latest:
    print("\n".join(lines(f"{latest} (current version, since {markers[-1][1]})", stats(by_ver.get(latest, [])))))
    print()
print("\n".join(lines("All-time (all versions)", stats(trade_rows))))
print()
for v in sorted(by_ver):
    if v != latest:
        print("\n".join(lines(f"{v} only", stats(by_ver[v]))))
        print()
print("Last 15 trade rows:\n")
print("| time | ver | action | symbol | reason | usd | pnl |\n|---|---|---|---|---|---|---|")
for r in trade_rows[-15:]:
    v = buy_version.get(r.get("mint"), "")
    print(f"| {r.get('time')} | {v} | {r.get('action')} | {r.get('symbol')} | {(r.get('reason') or '')[:40]} | "
          f"{r.get('usd_value')} | {r.get('pnl_usd')} |")
