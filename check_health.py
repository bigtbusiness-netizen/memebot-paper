#!/usr/bin/env python3
"""Health check for the GMGN paper demo. Read-only; prints no keys.
Usage: python3 check_health.py"""
import json, os, time

D = os.path.dirname(os.path.abspath(__file__))


def main():
    problems = []
    pid = None
    try:
        pid = int(open(os.path.join(D, "demo.pid")).read().strip())
    except Exception:
        pass
    alive = False
    if pid:
        try:
            alive = "demo.py" in open(f"/proc/{pid}/cmdline").read()
        except Exception:
            alive = False
    try:
        st = json.load(open(os.path.join(D, "stats.json")))
    except Exception as e:
        print(f"stats.json unreadable: {e}")
        return 2
    final = st.get("final")
    print(f"process      : pid {pid} {'ALIVE' if alive else 'NOT RUNNING'}{' (run finished)' if final else ''}")
    print(f"started/ends : {st.get('started')} -> {st.get('ends')} (Asia/Nicosia)")
    now = time.time()
    te = st.get("ticks_epoch") or {}
    def age(k):
        v = te.get(k) or 0
        return int(now - v) if v else None
    hb, mon, scan = age("heartbeat"), age("monitor"), age("scan")
    print(f"heartbeat    : {hb}s ago | monitor {mon}s ago | scan {scan}s ago | stats updated {st.get('updated')}")
    err_lines = 0
    try:
        with open(os.path.join(D, "demo.log")) as fh:
            err_lines = sum(1 for ln in fh if " ERROR " in ln and "ERROR   |" not in ln)
    except Exception:
        pass
    print(f"errors       : {st.get('error_count', 0)} code errors (log ERROR lines: {err_lines}), "
          f"{st.get('api_warnings', 0)} API warnings, api calls {st.get('api', {}).get('calls')} "
          f"rate-limited {st.get('api', {}).get('rate_limited')} timeouts {st.get('api', {}).get('timeouts')}")
    for e in (st.get("errors") or [])[-3:]:
        print(f"   last error: {e.get('time')} {e.get('where')}: {e.get('error')}")
    c = st.get("counts", {})
    print(f"trades       : bought {c.get('bought', 0)} | open {len(st.get('open_positions', []))} | "
          f"closed {len(st.get('closed', []))} | watching {len(st.get('watching', []))} | "
          f"launches seen {c.get('launches_seen', 0)} | evaluated {c.get('candidates_evaluated', 0)}")
    wr = st.get("win_rate")
    print(f"PnL (paper)  : realized ${st.get('realized_pnl_usd', 0):+.2f} | unrealized ${st.get('unrealized_pnl_usd', 0):+.2f} | "
          f"total ${st.get('total_pnl_usd', st.get('realized_pnl_usd', 0) + st.get('unrealized_pnl_usd', 0)):+.2f}"
          f"{f' | win rate {wr:.0%}' if wr is not None else ''}")
    for p in st.get("open_positions", []):
        print(f"   open {p['symbol']}: value ${p['value_usd']:.2f} unreal ${p['unrealized_pnl_usd']:+.2f} held {p['held_min']}m")
    if not final:
        if not alive:
            problems.append("process not running")
        if hb is None or hb > 150:
            problems.append("heartbeat stale")
        if mon is None or mon > 60:
            problems.append("monitor stale")
    if st.get("error_count", 0) or err_lines:
        problems.append("code errors present")
    print("STATUS       : " + ("OK" if not problems else "PROBLEM: " + ", ".join(problems)))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
