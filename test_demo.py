"""Offline test harness for demo.py. No network, no gmgn-cli, no wallet.
Run:  cd /workspace/memebot/gmgn_demo && python3 -m pytest -q test_demo.py
(set DEMO_FILE=other.py to test a different copy)."""
import importlib.util, json, os, subprocess, threading, time, csv
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_FILE = os.environ.get("DEMO_FILE", os.path.join(HERE, "demo.py"))
H = 0.03  # haircut


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ---------------- fake GMGN ----------------
NOW = time.time


def trench_row(mint, **kw):
    row = {"address": mint, "symbol": mint[:5].upper(), "name": mint,
           "created_timestamp": NOW() - 5 * 60, "usd_market_cap": 8000,
           "renounced_mint": 1, "renounced_freeze_account": 1, "rug_ratio": 0, "bundler_trader_amount_rate": 0,
           "suspected_insider_hold_rate": 0, "rat_trader_amount_rate": 0, "entrapment_ratio": 0,
           "is_wash_trading": False, "top_10_holder_rate": 0.2, "top70_sniper_hold_rate": 0.05,
           "creator_balance_rate": 0.02, "dev_team_hold_rate": 0.02, "creator_token_status": "creator_hold",
           "creator_created_count": 1, "creator_created_open_ratio": 0, "twitter_dup": 0,
           "twitter": "x.com/a", "smart_degen_count": 1, "renowned_count": 0, "x_user_follower": 0,
           "holder_count": 100, "net_buy_24h": 10}
    row.update(kw)
    return row


class FakeGM:
    def __init__(self):
        self.rows = []          # candidate trench rows
        self.sec = {}           # mint -> security dict override
        self.info = {}          # mint -> dict(price=, buys=, sells=, vol=, dev_status=, dev_rate=)
        self.raise_on = {}      # (kind, mint) -> exception to raise
        self.calls = []

    def __call__(self, args, weight=1, timeout=None, max_wait=None):
        self.calls.append(args)
        kind = " ".join(args[:2])
        mint = args[args.index("--address") + 1] if "--address" in args else None
        exc = self.raise_on.get((kind, mint)) or self.raise_on.get((kind, None))
        if exc:
            raise exc
        if kind == "market trenches":
            if "--min-created" in args:
                return {"new_creation": [dict(r) for r in self.rows], "near_completion": []}
            return {"new_creation": [{"address": r["address"]} for r in self.rows]}
        if kind == "token security":
            d = {"is_honeypot": None, "honeypot": 0, "can_not_sell": 0, "buy_tax": "0", "sell_tax": "0",
                 "renounced_mint": True, "renounced_freeze_account": True}
            d.update(self.sec.get(mint, {}))
            return d
        if kind == "token info":
            i = {"price": 8e-6, "buys": 30, "sells": 10, "vol": 3000, "dev_status": "creator_hold", "dev_rate": 0.02}
            i.update(self.info.get(mint, {}))
            return {"address": mint, "circulating_supply": "1000000000", "ath_price": i.get("ath", i["price"]),
                    "price": {"price": str(i["price"]), "buys_5m": i["buys"], "sells_5m": i["sells"],
                              "volume_5m": str(i["vol"])},
                    "dev": {"creator_token_status": i["dev_status"]},
                    "stat": {"creator_hold_rate": str(i["dev_rate"])}}
        raise AssertionError(f"unexpected gmgn call {args}")


@pytest.fixture
def demo(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"demo_under_test_{time.time_ns()}", DEMO_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LOG, mod.CSV, mod.STATS, mod.STOP = [str(tmp_path / n) for n in ("demo.log", "trades.csv", "stats.json", "STOP")]
    mod.D = str(tmp_path)
    fake = FakeGM()
    monkeypatch.setattr(mod, "gm", fake)
    monkeypatch.setattr(mod, "sol_usd", lambda: 75.0)

    def no_dex(mint):
        raise mod.ApiError("dexscreener: offline in tests")
    monkeypatch.setattr(mod, "dexscreener_price", no_dex)
    mod.FAKE = fake
    mod.CFG.update({"haircut_pct": 3.0, "trade_usd": 2.0, "max_open": 5, "confirm_wait_s": 45,
                    "confirm_max_drop_pct": 12, "early_stop_pct": -20, "early_stop_window_min": 3,
                    "tp1_pct": 100, "tp1_frac": 0.5, "trail_pct": 30, "stop_pct": -35, "time_stop_min": 45,
                    "min_score": 4.0})
    yield mod
    mod.STOP_EVENT.set()


def logtext(demo):
    return open(demo.LOG).read() if os.path.exists(demo.LOG) else ""


def trades(demo):
    with open(demo.CSV) as fh:
        return list(csv.DictReader(fh))


def stats(demo):
    demo.write_stats()
    return json.load(open(demo.STATS))


def age_pending(demo, mint, s=46):
    demo.PENDING[mint]["t0"] -= s


def buy_one(demo, mint="GOODmint1111", price=8e-6):
    demo.FAKE.rows = [trench_row(mint)]
    demo.FAKE.info[mint] = {"price": price}
    demo.scan_once()
    assert mint in demo.PENDING, logtext(demo)
    age_pending(demo, mint)
    demo.confirm_pending()
    assert mint in demo.POS, logtext(demo)
    return mint


def set_price(demo, mint, price, age_min=None):
    p = demo.POS[mint]
    if age_min is not None:
        p["opened"] = time.time() - age_min * 60
    demo.FAKE.info[mint] = {"price": price}


# ---------------- 1. happy path ----------------
def test_coin_passes_all_filters_and_is_bought(demo):
    mint = buy_one(demo)
    p = demo.POS[mint]
    assert demo.COUNTS["bought"] == 1 and demo.COUNTS["passed_filters"] == 1
    assert approx(p["tokens"], 2.0 / (8e-6 * 1.03))
    assert approx(p["value"], 2.0 * 0.97 / 1.03)
    rows = trades(demo)
    assert rows[0]["action"] == "BUY" and rows[0]["note"].endswith("confirmed")
    assert "PAPER BUY" in logtext(demo)
    assert "ERROR" not in logtext(demo)
    st = stats(demo)
    assert st["counts"]["bought"] == 1 and st["error_count"] == 0
    assert approx(st["unrealized_pnl_usd"], round(2 * 0.97 / 1.03 - 2, 3))


def test_open_position_signature_matches_all_callers(demo):
    import inspect
    src = open(DEMO_FILE).read()
    params = set(inspect.signature(demo.open_position).parameters)
    import re
    for call in re.findall(r"open_position\(([^)]*)\)", src):
        for kw in re.findall(r"(\w+)=", call):
            assert kw in params, f"open_position called with unknown kwarg {kw}"


# ---------------- 2. every reject reason ----------------
SCREEN_CASES = [
    ({"created_timestamp": NOW() - 60}, "age window"),
    ({"created_timestamp": NOW() - 40 * 60}, "age window"),
    ({"usd_market_cap": 1000}, "mcap out of range"),
    ({"usd_market_cap": 50000}, "mcap out of range"),
    ({"renounced_mint": 0}, "mint/freeze not renounced"),
    ({"rug_ratio": 0.5}, "rug ratio high"),
    ({"bundler_trader_amount_rate": 0.5}, "bundler rate high"),
    ({"rat_trader_amount_rate": 0.5}, "insider/rat traders high"),
    ({"entrapment_ratio": 0.5}, "entrapment ratio high"),
    ({"is_wash_trading": True}, "wash trading"),
    ({"top_10_holder_rate": 0.6}, "top10 concentration"),
    ({"top70_sniper_hold_rate": 0.6}, "snipers hold too much"),
    ({"creator_balance_rate": 0.5}, "dev holds too much"),
    ({"creator_token_status": "creator_close"}, "dev already sold"),
    ({"creator_balance_rate": 0}, "dev already sold"),
    ({"creator_created_count": 6, "creator_created_open_ratio": 0}, "serial dev (many launches, none graduated)"),
    ({"creator_created_count": 9, "creator_created_open_ratio": 0.1}, "serial dev (many launches, low graduation)"),
    ({"twitter_dup": 5}, "copycat X link"),
    ({"smart_degen_count": 0, "holder_count": 20, "twitter": ""}, "score"),
]


@pytest.mark.parametrize("kw,expect", SCREEN_CASES)
def test_screen_rejects(demo, kw, expect):
    r, _ = demo.screen(trench_row("M", **kw))
    assert r and r.startswith(expect), r


def test_screen_rejected_coin_is_recorded_not_bought(demo):
    demo.FAKE.rows = [trench_row("BADmint", bundler_trader_amount_rate=0.9)]
    demo.scan_once()
    assert demo.REASONS["BADmint"] == "bundler rate high"
    assert not demo.PENDING and not demo.POS


@pytest.mark.parametrize("setup,expect", [
    (lambda F: F.sec.__setitem__("Q", {"honeypot": 1}), "security: honeypot/cannot sell/tax"),
    (lambda F: F.sec.__setitem__("Q", {"sell_tax": "0.2"}), "security: honeypot/cannot sell/tax"),
    (lambda F: F.sec.__setitem__("Q", {"renounced_mint": False}), "security: authorities not renounced"),
    (lambda F: F.info.__setitem__("Q", {"buys": 3}), "weak live activity (#m buys/sells/volume)"),
    (lambda F: F.info.__setitem__("Q", {"buys": 15, "sells": 11}), "weak live activity (#m buys/sells/volume)"),
    (lambda F: F.info.__setitem__("Q", {"price": 1e-4}), "mcap ran before entry"),
])
def test_queue_rejects(demo, setup, expect):
    setup(demo.FAKE)
    demo.FAKE.rows = [trench_row("Q")]
    demo.scan_once()
    assert demo.REASONS["Q"] == expect
    assert "Q" in demo.TRADED and "Q" not in demo.PENDING and not demo.POS


def test_queue_reject_no_smart_no_pressure(demo):
    # no smart money: score must still pass, so give followers/holders/social
    demo.FAKE.rows = [trench_row("Q", smart_degen_count=0, x_user_follower=10000, holder_count=200)]
    demo.FAKE.info["Q"] = {"buys": 14, "sells": 5, "vol": 1500}
    demo.scan_once()
    assert demo.REASONS["Q"] == "no smart money and buy pressure not strong enough"


def test_strong_pressure_without_smart_money_is_queued(demo):
    demo.FAKE.rows = [trench_row("Q", smart_degen_count=0, x_user_follower=10000, holder_count=200)]
    demo.FAKE.info["Q"] = {"buys": 40, "sells": 10, "vol": 5000}
    demo.scan_once()
    assert "Q" in demo.PENDING


# ---------------- 3. confirm wait ----------------
def queue(demo, mint="C"):
    demo.FAKE.rows = [trench_row(mint)]
    demo.scan_once()
    assert mint in demo.PENDING
    demo.FAKE.rows = []
    return mint


def test_not_bought_before_wait(demo):
    m = queue(demo)
    demo.confirm_pending()
    assert m in demo.PENDING and not demo.POS


@pytest.mark.parametrize("change,expect", [
    ({"price": 8e-6 * 0.80}, "dumped #% during confirm wait"),
    ({"dev_status": "creator_close"}, "dev sold during confirm wait"),
    ({"dev_rate": 0}, "dev sold during confirm wait"),
    ({"buys": 2}, "activity faded during confirm"),
    ({"price": 8e-6 * 2.5}, "mcap left range during confirm"),
    ({"price": 0}, "no price on confirm"),
])
def test_confirm_rejects(demo, change, expect):
    m = queue(demo)
    demo.FAKE.info[m] = change
    age_pending(demo, m)
    demo.confirm_pending()
    assert demo.REASONS[m] == expect, demo.REASONS[m]
    assert not demo.POS and m not in demo.PENDING and demo.COUNTS["confirm_rejects"] == 1


def test_small_dip_during_confirm_still_buys(demo):
    m = queue(demo)
    demo.FAKE.info[m] = {"price": 8e-6 * 0.95}
    age_pending(demo, m)
    demo.confirm_pending()
    assert m in demo.POS


def test_confirm_security_fail(demo):
    m = queue(demo)
    demo.FAKE.sec[m] = {"can_not_sell": 1}
    age_pending(demo, m)
    demo.confirm_pending()
    assert demo.REASONS[m] == "security: honeypot/cannot sell/tax" and not demo.POS


def test_expired_watch_is_never_bought_and_counted_once(demo):
    m = queue(demo)
    age_pending(demo, m, 45 + 200)
    demo.confirm_pending()
    assert not demo.POS and demo.REASONS[m] == "confirm window expired"
    assert demo.COUNTS["confirm_rejects"] == 1


def test_confirm_keeps_pending_when_full(demo):
    m = queue(demo)
    demo.CFG["max_open"] = 1
    demo.POS["other"] = {"symbol": "O", "cost": 2, "tokens": 1, "realized": 0, "opened": time.time(),
                         "entry_mcap": 1, "tp1": False, "last_price": 2, "value": 2}
    age_pending(demo, m)
    demo.confirm_pending()
    assert m in demo.PENDING and m not in demo.POS


def test_confirm_runs_even_when_candidate_fetch_429s(demo):
    m = queue(demo)
    age_pending(demo, m)
    demo.FAKE.raise_on[("market trenches", None)] = demo.ApiError("429 rate limited")
    demo.scan_once()
    assert m in demo.POS
    assert "WARN trenches(candidates)" in logtext(demo)
    assert demo.ERR["count"] == 0


# ---------------- 4. exits + PnL math ----------------
def test_early_stop(demo):
    m = buy_one(demo)
    set_price(demo, m, 8e-6 * 0.75, age_min=1)
    demo.monitor_once()
    assert m not in demo.POS
    c = demo.CLOSED[0]
    exp = 2 * 0.75 * 0.97 / 1.03 - 2
    assert c["exit_reason"].startswith("early dump") and approx(c["pnl_exact"], exp)


def test_no_early_stop_after_window_but_hard_stop_fires(demo):
    m = buy_one(demo)
    set_price(demo, m, 8e-6 * 0.80, age_min=5)  # -27% incl haircut: past early window, above -35%
    demo.monitor_once()
    assert m in demo.POS
    set_price(demo, m, 8e-6 * 0.65, age_min=5)
    demo.monitor_once()
    c = demo.CLOSED[0]
    assert c["exit_reason"].startswith("stop loss")
    assert approx(c["pnl_exact"], 2 * 0.65 * 0.97 / 1.03 - 2)


def test_tp1_then_trailing_stop(demo):
    m = buy_one(demo)
    k = 0.97 / 1.03
    set_price(demo, m, 8e-6 * 2.5, age_min=5)
    demo.monitor_once()
    p = demo.POS[m]
    tp1_pnl = 1.0 * 2.5 * k - 1.0
    assert p["tp1"] and approx(p["realized"], tp1_pnl) and approx(p["cost"], 1.0)
    assert approx(p["tokens"], 0.5 * 2.0 / (8e-6 * 1.03))
    st = stats(demo)
    assert approx(st["realized_pnl_usd"], round(tp1_pnl, 3))
    assert approx(st["unrealized_pnl_usd"], round(1.0 * 2.5 * k - 1.0, 3))
    rows = trades(demo)
    assert rows[-1]["action"] == "SELL50%"
    # new peak 3x, then fall to 2.2x (ratio drops 26.7% -> hold), then 2.0x (-33% -> exit)
    set_price(demo, m, 8e-6 * 3.0); demo.monitor_once()
    set_price(demo, m, 8e-6 * 2.2); demo.monitor_once()
    assert m in demo.POS
    set_price(demo, m, 8e-6 * 2.0); demo.monitor_once()
    assert m not in demo.POS
    c = demo.CLOSED[0]
    total = tp1_pnl + (1.0 * 2.0 * k - 1.0)
    assert c["exit_reason"].startswith("trailing stop") and approx(c["pnl_exact"], total)
    st = stats(demo)
    assert approx(st["realized_pnl_usd"], round(total, 3)) and st["unrealized_pnl_usd"] == 0
    assert st["win_rate"] == 1.0


def test_time_stop_with_stale_price_when_gmgn_down(demo):
    m = buy_one(demo)
    k = 0.97 / 1.03
    set_price(demo, m, 8e-6 * 1.2, age_min=10)
    demo.monitor_once()                     # fresh price 1.2x
    p = demo.POS[m]
    p["opened"] = time.time() - 46 * 60
    p["last_price_ts"] = time.time() - 300  # stale
    demo.FAKE.raise_on[("token info", m)] = demo.ApiError("gmgn down")
    demo.monitor_once()
    c = demo.CLOSED[0]
    assert c["exit_reason"].startswith("time stop") and "(stale price)" in c["exit_reason"]
    assert approx(c["pnl_exact"], 2 * 1.2 * k - 2)


def test_monitor_falls_back_to_dexscreener(demo, monkeypatch):
    m = buy_one(demo)
    demo.FAKE.raise_on[("token info", m)] = TimeoutError("rate limiter wait exceeded")
    monkeypatch.setattr(demo, "dexscreener_price", lambda mint: (8e-6 * 0.5, 4000.0))
    demo.POS[m]["opened"] = time.time() - 600
    demo.monitor_once()
    assert demo.CLOSED and demo.CLOSED[0]["exit_reason"].startswith("stop loss")
    assert demo.ERR["count"] == 0


def test_price_error_is_warn_not_crash(demo):
    m = buy_one(demo)
    demo.FAKE.raise_on[("token info", m)] = demo.ApiError("gmgn down")
    demo.monitor_once()
    assert m in demo.POS and "WARN price" in logtext(demo) and demo.ERR["count"] == 0


# ---------------- 5. GMGN timeouts / 429 at the gm() layer ----------------
def test_gm_timeout_and_429(demo, monkeypatch):
    # use the module's real gm (fixture replaced it): reload a pristine function object
    src_mod = importlib.util.module_from_spec(importlib.util.spec_from_file_location("pristine", DEMO_FILE))
    importlib.util.spec_from_file_location("pristine", DEMO_FILE).loader.exec_module(src_mod)
    src_mod.LOG = demo.LOG
    src_mod.B.rate = 1000; src_mod.B.tokens = 100; src_mod.B.cap = 100

    def boom(args, timeout=None):
        raise subprocess.TimeoutExpired(cmd="gmgn-cli", timeout=1)
    monkeypatch.setattr(src_mod, "run_cli", boom)
    with pytest.raises(src_mod.ApiError):
        src_mod.gm(["token", "info"])
    assert src_mod.API["timeouts"] == 1

    monkeypatch.setattr(src_mod, "run_cli", lambda a, timeout=None: (1, "", "HTTP 429 RATE_LIMIT ~20s remaining"))
    with pytest.raises(src_mod.ApiError):
        src_mod.gm(["token", "info"])
    assert src_mod.API["rate_limited"] == 1
    bu = src_mod.B.blocked_until_ts()
    assert 15 < bu - time.time() <= 24
    # while blocked, a short max_wait raises TimeoutError instead of hanging (monitor path)
    with pytest.raises(TimeoutError):
        src_mod.gm(["token", "info"], max_wait=0.2)

    monkeypatch.setattr(src_mod, "run_cli", lambda a, timeout=None: (0, "not json {", ""))
    src_mod.B.block_until = 0
    with pytest.raises(src_mod.ApiError):
        src_mod.gm(["token", "info"])
    monkeypatch.setattr(src_mod, "run_cli", lambda a, timeout=None: (0, '{"ok": 1}', ""))
    assert src_mod.gm(["token", "info"]) == {"ok": 1}
    # absurd reset times are capped
    assert src_mod.parse_429_wait("reset_at 9999999999999", now=1000) <= 1000 + src_mod.CFG.get("rate_backoff_cap_s", 90)


# ---------------- 6. loud errors ----------------
def test_code_bug_is_loud_error_with_traceback(demo, monkeypatch):
    m = queue(demo)
    age_pending(demo, m)

    def broken(*a, **k):
        raise TypeError("open_position() got an unexpected keyword argument 'extra'")
    monkeypatch.setattr(demo, "open_position", broken)
    demo.confirm_pending()
    log = logtext(demo)
    assert "ERROR confirm" in log and "Traceback" in log
    st = stats(demo)
    assert st["error_count"] == 1 and st["errors"][0]["error"].startswith("TypeError")
    assert m in demo.PENDING  # retried until window expires


def test_errors_list_capped_at_20(demo):
    for i in range(25):
        demo.report_error("x", ValueError(str(i)))
    st = stats(demo)
    assert st["error_count"] == 25 and len(st["errors"]) == 20 and st["errors"][-1]["error"] == "ValueError: 24"


def test_monitor_exception_does_not_kill_loop(demo, monkeypatch):
    m = buy_one(demo)
    calls = {"n": 0}
    real = demo.maybe_exit

    def flaky(mint, stale=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("boom")
        return real(mint, stale)
    monkeypatch.setattr(demo, "maybe_exit", flaky)
    demo.monitor_once()
    assert demo.ERR["count"] == 1 and m in demo.POS
    demo.CFG["price_every_s"] = 0.05
    t = threading.Thread(target=demo.monitor_loop, daemon=True)
    t.start(); time.sleep(0.3)
    assert t.is_alive()
    demo.STOP_EVENT.set(); t.join(2)


def test_heartbeat_survives_exception(demo, monkeypatch):
    demo.CFG["heartbeat_every_s"] = 0.05
    n = {"c": 0}
    real = demo.B.blocked_until_ts

    def flaky():
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("hb boom")
        return real()
    monkeypatch.setattr(demo.B, "blocked_until_ts", flaky)
    t = threading.Thread(target=demo.heartbeat_loop, args=(time.time() + 60,), daemon=True)
    t.start(); time.sleep(0.4)
    assert t.is_alive() and logtext(demo).count("HEARTBEAT") >= 2
    assert demo.ERR["count"] == 1
    demo.STOP_EVENT.set(); t.join(2)


def test_log_never_raises(demo):
    demo.LOG = os.path.dirname(demo.STATS)  # a directory: open() fails
    demo.log("x")  # must not raise


def test_concurrent_stats_and_scan_mutations(demo):
    stop = threading.Event()

    def mutate():
        i = 0
        while not stop.is_set():
            demo.reason(f"m{i}", "x"); demo.PENDING[f"p{i%7}"] = {"x": {}, "score": 1, "t0": time.time()}
            demo.PENDING.pop(f"p{(i+3)%7}", None); i += 1
    t = threading.Thread(target=mutate, daemon=True); t.start()
    for _ in range(200):
        demo.write_stats()
    stop.set(); t.join()
    assert demo.ERR["count"] == 0


# ---------------- 7. full run: main() + final flush ----------------
def test_main_short_run_buys_and_flushes_final_stats(demo):
    demo.CFG.update({"run_minutes": 0.06, "scan_every_s": 0.2, "price_every_s": 0.1, "heartbeat_every_s": 0.5,
                     "confirm_wait_s": 0.3})
    demo.FAKE.rows = [trench_row("RUNmint"), trench_row("SKIPmint", rug_ratio=0.9)]
    t0 = time.time()
    demo.main()
    assert time.time() - t0 < 15
    st = json.load(open(demo.STATS))
    assert st["final"] is True and st["counts"]["bought"] == 1
    assert "skipped_followup" in st and st["skipped_followup"]["checked"] >= 1  # followup really runs now
    log = logtext(demo)
    assert "HEARTBEAT" in log and "demo end." in log and "ERROR" not in log
    assert os.path.exists(demo.STOP)
    assert open(os.path.join(demo.D, "demo.pid")).read() == str(os.getpid())


def test_watchdog_restarts_dead_thread(demo, monkeypatch):
    demo.CFG.update({"run_minutes": 0.03, "scan_every_s": 0.2, "price_every_s": 0.1, "heartbeat_every_s": 0.2})
    monkeypatch.setattr(demo, "heartbeat_loop", lambda end: None)  # dies immediately
    demo.main()
    assert "watchdog: heartbeat thread died" in logtext(demo)


# ---------------- 8. GitHub Actions additions: wind-down + run-end close ----------------
def test_wind_down_only_near_end_of_long_runs(demo):
    demo.CFG.update({"run_minutes": 340, "wind_down_min": 50})
    now = time.time()
    demo.RUN_END[0] = now + 60 * 60
    assert demo.winding_down(now) is False
    demo.RUN_END[0] = now + 49 * 60
    assert demo.winding_down(now) is True
    demo.CFG.update({"run_minutes": 0.06})  # short test runs never wind down
    assert demo.winding_down(now) is False


def test_close_all_at_end_paper_closes_open_positions(demo):
    buy_one(demo)
    assert len(demo.POS) == 1
    demo.close_all_at_end()
    assert len(demo.POS) == 0 and len(demo.CLOSED) == 1
    assert trades(demo)[-1]["reason"].startswith("run end")
