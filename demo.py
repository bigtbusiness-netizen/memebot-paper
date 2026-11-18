#!/usr/bin/env python3
"""GMGN-powered paper sniper demo. No wallet, no transactions. Never prints any API key."""
import json, os, subprocess, time, csv, threading, re, signal, traceback, shutil
from datetime import datetime
import requests

D = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(D, "config.json")))
LOG, CSV, STATS, STOP = [os.path.join(D, f) for f in ("demo.log", "trades.csv", "stats.json", "STOP")]
GM = os.environ.get("GMGN_CLI") or shutil.which("gmgn-cli") or "/home/box/.local/bin/gmgn-cli"
if os.environ.get("RUN_MINUTES"):  # e.g. GitHub Actions sets this to fit the 6h job limit
    CFG["run_minutes"] = float(os.environ["RUN_MINUTES"])
START = time.time()
RUN_END = [0.0]  # set in main()


def ts(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t or time.time()))


LOG_LOCK = threading.Lock()
def log(m):
    """Append one line to demo.log. Never raises (a failed log write must not kill a thread)."""
    line = f"{ts()} {m}\n"
    try:
        with LOG_LOCK:
            with open(LOG, "a") as fh:
                fh.write(line)
    except Exception as e:  # last resort: stderr goes to demo.out
        try:
            import sys
            sys.stderr.write(f"LOG WRITE FAILED ({e!r}): {line}")
        except Exception:
            pass


def redact(msg):
    # never echo keys, pems, or long tokens (do not eat file paths)
    return re.sub(r"(?<![./])[A-Za-z0-9_+=]{32,}", "<redacted>", str(msg))[:300]


# ---------- loud error tracking ----------
class ApiError(RuntimeError):
    """Expected external failure (GMGN/DexScreener down, 429, timeout, bad response)."""


ERRORS = []          # last 20 code errors
ERR = {"count": 0, "api_warnings": 0}
ERR_LOCK = threading.Lock()


def is_api_error(e):
    return isinstance(e, (ApiError, TimeoutError, requests.RequestException))


def report_error(where, e):
    """Code bugs -> 'ERROR' line + traceback + stats.json errors list. API hiccups -> 'WARN' line."""
    if is_api_error(e):
        with ERR_LOCK:
            ERR["api_warnings"] += 1
        log(f"WARN {where}: {type(e).__name__}: {redact(e)}")
        return
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    tb_lines = [redact(x) for x in tb.strip().splitlines()][-12:]
    with ERR_LOCK:
        ERR["count"] += 1
        ERRORS.append({"time": ts(), "where": where, "error": f"{type(e).__name__}: {redact(e)}",
                       "traceback": tb_lines})
        del ERRORS[:-20]
    log(f"ERROR {where}: {type(e).__name__}: {redact(e)}")
    for ln in tb_lines:
        log(f"ERROR   | {ln}")


def num(v, d=0.0):
    try:
        return float(v if v is not None else d)
    except Exception:
        return d


# ---------- rate limiter: never sleep while holding the lock ----------
class Bucket:
    def __init__(self, rate, cap, backoff_cap):
        self.rate, self.cap, self.tokens, self.t = rate, cap, float(cap), time.time()
        self.block_until = 0.0
        self.cv = threading.Condition()
        self.backoff_cap = backoff_cap

    def take(self, w, max_wait=None):
        deadline = time.time() + max_wait if max_wait is not None else None
        with self.cv:
            while True:
                now = time.time()
                if deadline is not None and now >= deadline:
                    raise TimeoutError("rate limiter wait exceeded")
                self.tokens = min(self.cap, self.tokens + max(0.0, now - self.t) * self.rate)
                self.t = now
                if now < self.block_until:
                    wait = self.block_until - now
                elif self.tokens < w:
                    wait = (w - self.tokens) / self.rate if self.rate > 0 else self.backoff_cap
                else:
                    self.tokens -= w
                    return
                wait = min(max(0.0, wait), self.backoff_cap)
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - now))
                if wait <= 0:
                    raise TimeoutError("rate limiter wait exceeded")
                self.cv.wait(timeout=wait)

    def backoff(self, until):
        cap_until = time.time() + self.backoff_cap
        with self.cv:
            self.block_until = max(self.block_until, min(until, cap_until))
            self.cv.notify_all()

    def blocked_until_ts(self):
        with self.cv:
            return self.block_until if self.block_until > time.time() else 0.0


B = Bucket(CFG["rate_weight_per_s"], CFG.get("rate_capacity", 2), CFG.get("rate_backoff_cap_s", 90))
API = {"calls": 0, "errors": 0, "rate_limited": 0, "timeouts": 0, "last_error": "", "last_ok": 0}
API_LOCK = threading.Lock()
CLI_TIMEOUT = int(CFG.get("cli_timeout_s", 15))


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_cli(args, timeout=None):
    """Run gmgn-cli in its own process group so a hung Node process can be hard-killed."""
    timeout = CLI_TIMEOUT if timeout is None else timeout
    p = subprocess.Popen(
        [GM] + list(args) + ["--raw"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, (out or "").strip(), (err or "").strip()
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            p.communicate(timeout=3)
        except Exception:
            _kill_group(p)
        raise


def parse_429_wait(msg, now=None):
    """Parse a 429 message; always cap the pause so a bad parse cannot freeze the bot for hours."""
    now = now or time.time()
    cap = CFG.get("rate_backoff_cap_s", 90)
    m2 = re.search(r"~(\d+)s remaining", msg)
    if m2:
        return now + min(int(m2.group(1)) + 3, cap)
    m = re.search(r"reset_at\D*(\d{10,13})", msg)
    if m:
        raw = int(m.group(1))
        if raw > 10_000_000_000:  # milliseconds
            raw = raw / 1000.0
        return max(now, min(raw + 2, now + cap))
    m3 = re.search(r"resets at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", msg)
    if m3:
        try:
            # message times are local (Asia/Nicosia) in previous 429s
            t = datetime.strptime(m3.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            return max(now, min(t + 2, now + cap))
        except Exception:
            pass
    return now + min(35, cap)


def gm(args, weight=1, timeout=None, max_wait=None):
    B.take(weight, max_wait=max_wait)
    with API_LOCK:
        API["calls"] += 1
    try:
        rc, out, err = run_cli(args, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        msg = f"cli timeout after {timeout or CLI_TIMEOUT}s: {' '.join(args[:6])}"
        with API_LOCK:
            API["errors"] += 1
            API["timeouts"] += 1
            API["last_error"] = msg
        raise ApiError(msg) from e
    if rc != 0 or not out.startswith(("{", "[")):
        msg = redact(err or out or f"exit code {rc}")
        with API_LOCK:
            API["errors"] += 1
            API["last_error"] = msg
        if "429" in msg or "RATE_LIMIT" in msg:
            with API_LOCK:
                API["rate_limited"] += 1
            until = parse_429_wait(err or out)
            B.backoff(until)
            log(f"GMGN rate limited; pausing until {ts(until)}")
        raise ApiError(msg)
    try:
        j = json.loads(out)
    except ValueError as e:
        with API_LOCK:
            API["errors"] += 1
            API["last_error"] = "bad json from gmgn-cli"
        raise ApiError("bad json from gmgn-cli") from e
    with API_LOCK:
        API["last_ok"] = time.time()
    return j


_sol = {"v": None, "t": 0}
def sol_usd():
    if _sol["v"] and time.time() - _sol["t"] < 120:
        return _sol["v"]
    try:
        j = requests.get(
            "https://lite-api.jup.ag/price/v3",
            params={"ids": "So11111111111111111111111111111111111111112"},
            timeout=8,
        ).json()
        _sol.update(v=float(j["So11111111111111111111111111111111111111112"]["usdPrice"]), t=time.time())
    except Exception:
        _sol["t"] = time.time()  # don't retry on every stats write while Jupiter is down
    return _sol["v"]


def dexscreener_price(mint):
    """Public fallback so exits can still fire when gmgn-cli is down or rate-limited."""
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8)
        r.raise_for_status()
        pairs = (r.json() or {}).get("pairs") or []
    except (requests.RequestException, ValueError) as e:
        raise ApiError(f"dexscreener: {e}") from e
    if not pairs:
        raise ApiError("dexscreener: no pairs")
    # prefer the highest-liquidity Solana pair
    sol = [p for p in pairs if str(p.get("chainId", "")).lower() in ("solana", "sol")]
    p = max(sol or pairs, key=lambda x: num((x.get("liquidity") or {}).get("usd")))
    price = num(p.get("priceUsd"))
    mc = num(p.get("marketCap") or p.get("fdv"))
    return price, mc


# ---------- state ----------
# Mutations of POS/CLOSED/COUNTS/PENDING/REASONS/TRADED happen under LOCK (RLock so helpers can nest).
SEEN, REASONS, SKIPPED = set(), {}, {}
TRADED, EVALUATED, PASSED = set(), set(), set()
PENDING = {}  # mint -> {x, score, t0, price, mcap, b5, s5, v5, dev_rate}
POS, CLOSED = {}, []
COUNTS = {"launches_seen": 0, "candidates_evaluated": 0, "passed_filters": 0,
          "security_rejects": 0, "bought": 0, "confirm_rejects": 0}
LOCK = threading.RLock()
STOP_EVENT = threading.Event()
TICK = {"scan": 0, "monitor": 0, "price": 0, "heartbeat": 0}


def reason(mint, r):
    """Latest reject reason per mint (digits normalised so counts group). BOUGHT is never overwritten."""
    key = re.sub(r"\d+(\.\d+)?", "#", r)
    with LOCK:
        if REASONS.get(mint) != "BOUGHT":
            REASONS[mint] = key


FIELDS = ["time", "action", "mint", "symbol", "name", "reason", "price_usd", "mcap_usd",
          "usd_value", "pnl_usd", "pnl_pct", "score", "note"]
CSV_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
def record(row):
    with CSV_LOCK:
        new = not os.path.exists(CSV)
        with open(CSV, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in FIELDS})


# ---------- filters ----------
def f(x, k, d=0.0):
    return num((x or {}).get(k), d)


SOLD_STATUSES = ("creator_close", "creator_sold", "creator_close_position")


def dev_sold(x):
    """Scan-time check on a trenches row."""
    status = str(x.get("creator_token_status") or "")
    if status in SOLD_STATUSES:
        return True
    if x.get("creator_balance_rate") is not None:
        try:
            if float(x.get("creator_balance_rate")) <= 0:
                return True
        except Exception:
            pass
    return False


def dev_sold_info(j, rate_at_watch=None):
    """Confirm-time check on a fresh `token info` response (dev / stat blocks).
    The security endpoint has no creator fields, so this is the real 'dev hasn't sold' recheck."""
    dev = (j or {}).get("dev") or {}
    stat = (j or {}).get("stat") or {}
    if str(dev.get("creator_token_status") or "") in SOLD_STATUSES:
        return True
    now_rate = stat.get("creator_hold_rate")
    if rate_at_watch and rate_at_watch > 0 and now_rate is not None and num(now_rate) <= 0:
        return True
    return False


def screen(x):
    """Return (reason|None, score)."""
    age = (time.time() - f(x, "created_timestamp")) / 60
    mc = f(x, "usd_market_cap", f(x, "market_cap"))
    if age < CFG["min_age_min"] or age > CFG["max_age_min"]:
        return "age window", 0
    if mc < CFG["min_mcap"] or mc > CFG["max_mcap"]:
        return "mcap out of range", 0
    if str(x.get("renounced_mint")) not in ("1", "True", "true") or str(x.get("renounced_freeze_account")) not in ("1", "True", "true"):
        return "mint/freeze not renounced", 0
    if f(x, "rug_ratio") > CFG["max_rug_ratio"]:
        return "rug ratio high", 0
    if f(x, "bundler_trader_amount_rate") > CFG["max_bundler_rate"]:
        return "bundler rate high", 0
    if f(x, "suspected_insider_hold_rate") > CFG["max_insider_rate"] or f(x, "rat_trader_amount_rate") > CFG["max_insider_rate"]:
        return "insider/rat traders high", 0
    if f(x, "entrapment_ratio") > CFG["max_entrapment"]:
        return "entrapment ratio high", 0
    if x.get("is_wash_trading"):
        return "wash trading", 0
    if f(x, "top_10_holder_rate") > CFG["max_top10"]:
        return "top10 concentration", 0
    if f(x, "top70_sniper_hold_rate") > CFG["max_sniper_hold"]:
        return "snipers hold too much", 0
    if f(x, "creator_balance_rate") > CFG["max_creator_hold"] or f(x, "dev_team_hold_rate") > CFG["max_creator_hold"]:
        return "dev holds too much", 0
    if dev_sold(x):
        return "dev already sold", 0
    created = f(x, "creator_created_count")
    open_ratio = f(x, "creator_created_open_ratio")
    if created >= CFG["serial_dev_min_created"] and open_ratio <= CFG["serial_dev_max_open_ratio"]:
        return "serial dev (many launches, none graduated)", 0
    if created >= CFG.get("serial_dev_hard_count", 8) and open_ratio < CFG.get("serial_dev_hard_min_ratio", 0.2):
        return "serial dev (many launches, low graduation)", 0
    if f(x, "twitter_dup") > CFG["max_twitter_dup"]:
        return "copycat X link", 0
    has_social = bool(x.get("twitter") or x.get("telegram") or x.get("website"))
    if CFG["require_social"] and not has_social:
        return "no socials", 0
    score = 3 * f(x, "smart_degen_count") + 3 * f(x, "renowned_count") + (1 if has_social else 0) \
        + min(2, f(x, "x_user_follower") / 5000) + min(2, f(x, "holder_count") / 100) + (1 if f(x, "net_buy_24h") > 0 else 0)
    if score < CFG.get("min_score", 0):
        return f"score {score:.2f} below min {CFG.get('min_score')}", 0
    return None, round(score, 2)


def activity_ok(b5, s5, v5):
    if b5 < CFG["min_buys_5m"] or v5 < CFG["min_volume_5m_usd"]:
        return False
    if b5 < CFG["min_buy_sell_ratio_5m"] * max(s5, 1):
        return False
    return True


def smart_or_pressure(x, b5, s5, v5):
    smart = f(x, "smart_degen_count") + f(x, "renowned_count")
    if smart >= CFG.get("min_smart_or_kol", 1):
        return True
    ratio = b5 / max(s5, 1)
    return (ratio >= CFG.get("strong_ratio_5m", 1.5)
            and v5 >= CFG.get("strong_volume_5m_usd", 2000)
            and b5 >= CFG.get("strong_buys_5m", 20))


def info(mint, max_wait=None, timeout=None):
    j = gm(["token", "info", "--chain", "sol", "--address", mint], 1, timeout=timeout, max_wait=max_wait)
    if not isinstance(j, dict):
        raise ApiError("token info: unexpected response shape")
    p = j.get("price") or {}
    price = num(p.get("price"))
    supply = num(j.get("circulating_supply") or j.get("total_supply"), 1e9) or 1e9
    return j, p, price, price * supply


def security_ok(mint):
    sec = gm(["token", "security", "--chain", "sol", "--address", mint], 1)
    if not isinstance(sec, dict):
        raise ApiError("token security: unexpected response shape")
    if sec.get("is_honeypot") or num(sec.get("honeypot")) > 0 or num(sec.get("can_not_sell")) > 0 \
            or num(sec.get("sell_tax")) > 0.05 or num(sec.get("buy_tax")) > 0.05:
        with LOCK:
            COUNTS["security_rejects"] += 1
        reason(mint, "security: honeypot/cannot sell/tax")
        return False, sec
    if not (sec.get("renounced_mint") and sec.get("renounced_freeze_account")):
        with LOCK:
            COUNTS["security_rejects"] += 1
        reason(mint, "security: authorities not renounced")
        return False, sec
    return True, sec


def note_for(x, b5, s5, v5, extra=""):
    n = (f"age {int((time.time()-f(x,'created_timestamp'))/60)}m holders {int(f(x,'holder_count'))} "
         f"top10 {f(x,'top_10_holder_rate'):.0%} smart {int(f(x,'smart_degen_count'))} "
         f"kol {int(f(x,'renowned_count'))} 5m {b5}b/{s5}s ${v5:.0f} "
         f"dev_launches {int(f(x,'creator_created_count'))}")
    return n + (f" {extra}" if extra else "")


def reject_pending(mint, why, skip=True):
    with LOCK:
        pending = PENDING.pop(mint, None)
        if pending is None:
            return False  # already handled (no double counting)
        reason(mint, why)
        COUNTS["confirm_rejects"] += 1
        TRADED.add(mint)
        if skip:
            SKIPPED[mint] = {"sym": pending["x"].get("symbol"), "mc": pending.get("mcap"), "t": time.time(), "why": why}
    log(f"CONFIRM REJECT {pending['x'].get('symbol')} {mint} — {why}")
    return True


def entry_value(usd):
    """Mark-to-market value right after a buy: pay +haircut on entry, receive -haircut on exit."""
    h = CFG["haircut_pct"] / 100
    return usd * (1 - h) / (1 + h)


def open_position(x, score, price, mc, b5, s5, v5, note_extra=""):
    mint = x["address"]
    if price <= 0:
        raise ValueError(f"open_position with non-positive price {price}")
    with LOCK:
        if len(POS) >= CFG["max_open"] or mint in POS:
            reason(mint, "passed but max positions")
            return False
        entry = price * (1 + CFG["haircut_pct"] / 100)
        tokens = CFG["trade_usd"] / entry
        now = time.time()
        POS[mint] = {
            "symbol": x.get("symbol"), "name": x.get("name"), "opened": now,
            "entry_price": price, "entry_price_eff": entry, "tokens": tokens, "cost": CFG["trade_usd"],
            "tp1": False, "entry_mcap": mc,
            "realized": 0.0, "score": score, "last_price": price, "mcap": mc,
            "value": entry_value(CFG["trade_usd"]),
            "peak_value_per_cost": entry_value(1.0), "last_price_ts": now,
        }
        COUNTS["bought"] += 1
        REASONS[mint] = "BOUGHT"
        TRADED.add(mint)
        PENDING.pop(mint, None)
    note = note_for(x, b5, s5, v5, note_extra)
    record({"time": ts(), "action": "BUY", "mint": mint, "symbol": x.get("symbol"), "name": x.get("name"),
            "reason": "passed GMGN filters + confirm", "price_usd": f"{price:.4e}", "mcap_usd": f"{mc:.0f}",
            "usd_value": f"{CFG['trade_usd']:.2f}", "score": score, "note": note})
    log(f"PAPER BUY {x.get('symbol')} {mint} mcap ${mc:,.0f} score {score} | {note}")
    return True


def queue_or_reject(x, score):
    """First pass: security + live activity, then wait confirm_wait_s before buying."""
    mint = x["address"]
    ok, _sec = security_ok(mint)
    if not ok:
        with LOCK:
            TRADED.add(mint)
        return False
    _j, p, price, mc = info(mint)
    b5, s5, v5 = int(num(p.get("buys_5m"))), int(num(p.get("sells_5m"))), num(p.get("volume_5m"))
    why, short = None, None
    if not activity_ok(b5, s5, v5):
        why, short = "weak live activity (5m buys/sells/volume)", "activity"
    elif not smart_or_pressure(x, b5, s5, v5):
        why, short = "no smart money and buy pressure not strong enough", "no smart/pressure"
    elif price <= 0 or mc > CFG["max_mcap"] * 1.1 or mc < CFG["min_mcap"] * 0.8:
        why, short = "mcap ran before entry", "mcap ran"
    if why:
        reason(mint, why)
        with LOCK:
            SKIPPED[mint] = {"sym": x.get("symbol"), "mc": mc, "t": time.time(), "why": short}
            TRADED.add(mint)
        return False
    dev_rate = (_j.get("stat") or {}).get("creator_hold_rate")
    with LOCK:
        PENDING[mint] = {"x": x, "score": score, "t0": time.time(), "price": price, "mcap": mc,
                         "b5": b5, "s5": s5, "v5": v5,
                         "dev_rate": num(dev_rate) if dev_rate is not None else None}
    log(f"WATCH {x.get('symbol')} {mint} mcap ${mc:,.0f} score {score} — recheck in {CFG.get('confirm_wait_s', 45)}s")
    return True


def confirm_one(mint, pending):
    """Re-check one watched coin after the wait; buy or reject. Returns 'bought'|'rejected'|'kept'."""
    drop_max = CFG.get("confirm_max_drop_pct", 12) / 100.0
    x, score = pending["x"], pending["score"]
    ok, _sec = security_ok(mint)
    if not ok:
        reject_pending(mint, REASONS.get(mint, "security failed on confirm"))
        return "rejected"
    j, p, price, mc = info(mint)
    if dev_sold_info(j, pending.get("dev_rate")):
        reject_pending(mint, "dev sold during confirm wait")
        return "rejected"
    b5, s5, v5 = int(num(p.get("buys_5m"))), int(num(p.get("sells_5m"))), num(p.get("volume_5m"))
    if price <= 0:
        reject_pending(mint, "no price on confirm")
        return "rejected"
    if pending["price"] and price <= pending["price"] * (1 - drop_max):
        drop = (1 - price / pending["price"]) * 100
        reject_pending(mint, f"dumped {drop:.0f}% during confirm wait")
        return "rejected"
    if mc > CFG["max_mcap"] * 1.1 or mc < CFG["min_mcap"] * 0.8:
        reject_pending(mint, "mcap left range during confirm")
        return "rejected"
    if not activity_ok(b5, s5, v5):
        reject_pending(mint, "activity faded during confirm")
        return "rejected"
    if not smart_or_pressure(x, b5, s5, v5):
        reject_pending(mint, "smart/pressure faded during confirm")
        return "rejected"
    with LOCK:
        if mint not in PENDING:
            return "rejected"
        if len(POS) >= CFG["max_open"]:
            return "kept"  # retry when a slot opens, until the confirm window expires
        bought = open_position(x, score, price, mc, b5, s5, v5, note_extra="confirmed")
    return "bought" if bought else "kept"


def confirm_pending():
    wait = CFG.get("confirm_wait_s", 45)
    # 1) expire stale watches FIRST, so an expired coin can never be bought below
    with LOCK:
        stale = [m for m, p in PENDING.items() if time.time() - p["t0"] > wait + 180]
    for mint in stale:
        reject_pending(mint, "confirm window expired")
    with LOCK:
        due = [(m, p) for m, p in PENDING.items() if time.time() - p["t0"] >= wait]
    results = {}
    for mint, pending in due[:3]:
        if STOP_EVENT.is_set() or os.path.exists(STOP):
            break
        with LOCK:
            if mint not in PENDING:
                continue
        try:
            results[mint] = confirm_one(mint, pending)
        except Exception as e:
            results[mint] = "error"
            report_error(f"confirm {pending['x'].get('symbol')}", e)
            # leave pending to retry until the confirm window expires
    return results


# ---------- exits (call with LOCK held) ----------
def sell(mint, frac, why):
    with LOCK:
        p = POS[mint]
        frac = min(1.0, max(0.0, frac))
        tok = p["tokens"] * frac
        usd = tok * p["last_price"] * (1 - CFG["haircut_pct"] / 100)
        cost = p["cost"] * frac
        pnl = usd - cost
        full = frac >= 0.999
        if full:
            p["tokens"], p["cost"] = 0.0, 0.0
        else:
            p["tokens"] -= tok
            p["cost"] -= cost
        p["realized"] += pnl
        p["value"] = p["tokens"] * p["last_price"] * (1 - CFG["haircut_pct"] / 100)
        record({"time": ts(), "action": "SELL" if full else f"SELL{int(round(frac*100))}%",
                "mint": mint, "symbol": p["symbol"], "name": p["name"], "reason": why,
                "price_usd": f"{p['last_price']:.4e}", "mcap_usd": f"{p.get('mcap', 0):.0f}",
                "usd_value": f"{usd:.2f}", "pnl_usd": f"{pnl:+.2f}",
                "pnl_pct": f"{pnl/cost*100:+.1f}" if cost else ""})
        log(f"PAPER SELL {p['symbol']} {frac:.0%} - {why}: ${usd:.2f} pnl ${pnl:+.2f}")
        if full or p["tokens"] <= 0:
            tot = p["realized"]
            CLOSED.append({"symbol": p["symbol"], "mint": mint, "entry_mcap": round(p["entry_mcap"]),
                           "exit_reason": why, "pnl_usd": round(tot, 3), "pnl_exact": tot,
                           "pnl_pct": round(tot / CFG["trade_usd"] * 100, 1),
                           "held_min": round((time.time() - p["opened"]) / 60, 1)})
            POS.pop(mint)
        return usd, pnl


def mark_to_market(p, price, mc=None):
    p["last_price"] = price
    p["last_price_ts"] = time.time()
    if mc is not None and mc > 0:
        p["mcap"] = mc
    val = p["tokens"] * price * (1 - CFG["haircut_pct"] / 100)
    p["value"] = val
    return (val / p["cost"] - 1) * 100 if p["cost"] else 0


def maybe_exit(mint, stale=False):
    """Apply exit rules using the last known price. Returns the exit reason or None."""
    with LOCK:
        if mint not in POS:
            return None
        p = POS[mint]
        if p["cost"] <= 0 or p["tokens"] <= 0:
            return None
        val = p["tokens"] * p["last_price"] * (1 - CFG["haircut_pct"] / 100)
        p["value"] = val
        ratio = val / p["cost"]
        pnl_pct = (ratio - 1) * 100
        p["peak_value_per_cost"] = max(p.get("peak_value_per_cost", 0), ratio)
        age = (time.time() - p["opened"]) / 60
        tag = " (stale price)" if stale else ""
        # time stop first so it always fires on schedule, even with a stale price
        if age >= CFG["time_stop_min"]:
            why = f"time stop {age:.0f}m ({pnl_pct:+.0f}%){tag}"
            sell(mint, 1.0, why)
            return why
        if age <= CFG.get("early_stop_window_min", 3) and pnl_pct <= CFG.get("early_stop_pct", -20):
            why = f"early dump {pnl_pct:.0f}% in {age:.1f}m{tag}"
            sell(mint, 1.0, why)
            return why
        if pnl_pct <= CFG["stop_pct"]:
            why = f"stop loss {pnl_pct:.0f}%{tag}"
            sell(mint, 1.0, why)
            return why
        if not p["tp1"] and pnl_pct >= CFG["tp1_pct"]:
            why = f"TP1 +{pnl_pct:.0f}%"
            sell(mint, CFG["tp1_frac"], why)
            if mint in POS:
                POS[mint]["tp1"] = True
            return why
        if p["tp1"] and ratio <= p["peak_value_per_cost"] * (1 - CFG["trail_pct"] / 100):
            why = f"trailing stop ({pnl_pct:+.0f}%){tag}"
            sell(mint, 1.0, why)
            return why
        return None


def fetch_price(mint):
    """GMGN first, DexScreener fallback. Short waits so the monitor cannot stall."""
    last_err = None
    try:
        _j, _p, price, mc = info(mint, max_wait=CFG.get("monitor_rate_wait_s", 3),
                                 timeout=CFG.get("monitor_cli_timeout_s", 8))
        if price > 0:
            return price, mc, "gmgn"
    except Exception as e:
        if not is_api_error(e):
            raise
        last_err = e
    try:
        price, mc = dexscreener_price(mint)
        if price > 0:
            return price, mc, "dexscreener"
    except Exception as e:
        if not is_api_error(e):
            raise
        last_err = e
    raise ApiError(redact(last_err or "no price"))


def monitor_once():
    TICK["monitor"] = time.time()
    # 1) apply time / early / hard stops on last known prices first
    with LOCK:
        mints = list(POS)
    for mint in mints:
        try:
            with LOCK:
                if mint not in POS:
                    continue
                stale = (time.time() - POS[mint].get("last_price_ts", POS[mint]["opened"])) > 60
            maybe_exit(mint, stale=stale)
        except Exception as e:
            report_error(f"monitor exit-check {mint}", e)
    # 2) refresh prices for remaining positions
    with LOCK:
        mints = list(POS)
    for mint in mints:
        if STOP_EVENT.is_set() or os.path.exists(STOP):
            break
        try:
            price, mc, _src = fetch_price(mint)
            with LOCK:
                if mint not in POS or price <= 0:
                    continue
                mark_to_market(POS[mint], price, mc)
            maybe_exit(mint, stale=False)
            TICK["price"] = time.time()
        except Exception as e:
            with LOCK:
                sym = POS[mint].get("symbol") if mint in POS else mint
            report_error(f"price {sym}", e)


def monitor_loop():
    """Independent exit loop: time/early stops fire even if the scanner or GMGN is stuck."""
    interval = CFG.get("price_every_s", 12)
    while not STOP_EVENT.is_set() and not os.path.exists(STOP):
        t0 = time.time()
        try:
            monitor_once()
        except Exception as e:
            report_error("monitor loop", e)
        write_stats()
        STOP_EVENT.wait(timeout=max(0.5, interval - (time.time() - t0)))


def scan_once():
    try:
        # 1) count all new launches
        try:
            d = gm(["market", "trenches", "--chain", "sol", "--type", "new_creation", "--limit", "80"], 2)
            for x in (d.get("new_creation") or []) if isinstance(d, dict) else []:
                a = x.get("address")
                if a and a not in SEEN:
                    SEEN.add(a); COUNTS["launches_seen"] += 1
        except Exception as e:
            report_error("trenches(all)", e)
        # 2) activity-qualified candidates in the age/mcap window
        try:
            d = gm(["market", "trenches", "--chain", "sol", "--type", "new_creation", "--type", "near_completion",
                    "--limit", "80", "--min-created", f"{CFG['min_age_min']}m", "--max-created", f"{CFG['max_age_min']}m",
                    "--max-marketcap", str(CFG["max_mcap"]), "--min-marketcap", str(CFG["min_mcap"]),
                    "--min-holder-count", str(CFG["server_min_holders"]),
                    "--min-volume-24h", str(CFG["server_min_volume_24h"])], 2)
        except Exception as e:
            report_error("trenches(candidates)", e)
            d = {}
        if not isinstance(d, dict):
            d = {}
        items = (d.get("new_creation") or []) + (d.get("pump") or d.get("near_completion") or [])
        cands = []
        for x in items:
            m = x.get("address") if isinstance(x, dict) else None
            if not m:
                continue
            with LOCK:
                if m not in SEEN:
                    SEEN.add(m); COUNTS["launches_seen"] += 1
                if m in POS or m in TRADED or m in PENDING:
                    continue
                if m not in EVALUATED:
                    EVALUATED.add(m); COUNTS["candidates_evaluated"] += 1
            r, sc = screen(x)
            if r:
                reason(m, r)
                with LOCK:
                    if m not in SKIPPED:
                        SKIPPED[m] = {"sym": x.get("symbol"), "mc": f(x, "usd_market_cap", f(x, "market_cap")),
                                      "t": time.time(), "why": r}
                continue
            cands.append((sc, x))
        cands.sort(key=lambda c: -c[0])
        if winding_down():
            if cands:
                log(f"wind-down: {len(cands)} candidate(s) ignored, no new buys in the last "
                    f"{CFG.get('wind_down_min')}m of the run")
            cands = []
        for sc, x in cands[:3]:
            if STOP_EVENT.is_set() or os.path.exists(STOP):
                return
            m = x["address"]
            with LOCK:
                if m in TRADED or m in PENDING:
                    continue
                if len(POS) + len(PENDING) >= CFG["max_open"]:
                    reason(m, "passed but max positions")
                    continue
                if m not in PASSED:
                    PASSED.add(m); COUNTS["passed_filters"] += 1
            try:
                queue_or_reject(x, sc)
            except Exception as e:
                report_error(f"buy check {x.get('symbol')}", e)
    finally:
        # confirms run every cycle, even if the candidate fetch failed (e.g. GMGN 429)
        try:
            confirm_pending()
        except Exception as e:
            report_error("confirm_pending", e)
        TICK["scan"] = time.time()


def winding_down(now=None):
    """Last wind_down_min minutes of a long run: no new buys, so open positions can exit by their own rules."""
    wd = CFG.get("wind_down_min", 0) or 0
    if not wd or not RUN_END[0] or CFG["run_minutes"] < 2 * wd:
        return False
    return (RUN_END[0] - (now or time.time())) < wd * 60


def close_all_at_end():
    """Paper-close anything still open at the last known price so no position leaks across runs."""
    with LOCK:
        for mint in list(POS):
            try:
                sell(mint, 1.0, "run end (paper close at last price)")
            except Exception as e:
                report_error(f"run-end close {mint}", e)


def write_stats(final=False):
    """Never raises; errors are reported loudly."""
    try:
        sol = sol_usd()  # network call kept OUTSIDE the locks
        with STATS_LOCK:
            _write_stats_locked(final, sol)
    except Exception as e:
        report_error("write_stats", e)


def stats_snapshot(final=False, sol=None):
    with LOCK:
        wins = [c for c in CLOSED if c["pnl_exact"] > 0]
        rc = {}
        for r in REASONS.values():
            rc[r] = rc.get(r, 0) + 1
        now = time.time()
        open_pos = [{"symbol": p["symbol"], "mint": m, "entry_mcap": round(p["entry_mcap"]),
                     "mcap_now": round(p.get("mcap", 0)),
                     "value_usd": round(p.get("value", 0), 3),
                     "unrealized_pnl_usd": round(p.get("value", 0) - p["cost"], 3),
                     "realized_usd": round(p["realized"], 3), "tp1_hit": p["tp1"],
                     "held_min": round((now - p["opened"]) / 60, 1),
                     "stale_s": round(now - p.get("last_price_ts", p["opened"]), 1)}
                    for m, p in POS.items()]
        realized = sum(c["pnl_exact"] for c in CLOSED) + sum(p["realized"] for p in POS.values())
        unreal = sum(p.get("value", 0) - p["cost"] for p in POS.values())
        closed_copy = [{k: v for k, v in c.items() if k != "pnl_exact"} for c in CLOSED]
        counts_copy = dict(COUNTS)
        watching = [{"symbol": p["x"].get("symbol"), "mint": m, "score": p["score"],
                     "waited_s": round(now - p["t0"], 1), "mcap": round(p.get("mcap") or 0)}
                    for m, p in PENDING.items()]
    with API_LOCK:
        api_copy = dict(API)
    with ERR_LOCK:
        errors_copy = list(ERRORS)
        err_count, api_warn = ERR["count"], ERR["api_warnings"]
    return {"updated": ts(), "final": final, "pid": os.getpid(), "started": ts(START),
            "ends": ts(RUN_END[0]) if RUN_END[0] else None,
            "counts": counts_copy,
            "filtered_by_reason": dict(sorted(rc.items(), key=lambda kv: -kv[1])),
            "watching": watching, "open_positions": open_pos, "closed": closed_copy,
            "realized_pnl_usd": round(realized, 3), "unrealized_pnl_usd": round(unreal, 3),
            "total_pnl_usd": round(realized + unreal, 3),
            "win_rate": round(len(wins) / len(closed_copy), 3) if closed_copy else None,
            "api": api_copy, "sol_usd": sol,
            "error_count": err_count, "api_warnings": api_warn, "errors": errors_copy,
            "ticks": {k: (ts(v) if v else None) for k, v in TICK.items()},
            "ticks_epoch": dict(TICK)}


def _write_stats_locked(final=False, sol=None, extra=None):
    out = stats_snapshot(final, sol)
    if extra:
        out.update(extra)
    tmp = STATS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATS)


def followup(deadline_s=120):
    """Did skipped coins later pump (ATH >= 2x skip mcap) or rug (now <= 0.3x)? Sample up to 15.
    Runs at shutdown (after STOP_EVENT is set), so it is bounded by its own deadline instead."""
    res = {"checked": 0, "pumped_2x": 0, "rugged_70pct": 0, "examples": [], "errors": 0}
    with LOCK:
        items = list(SKIPPED.items())[:15]
    t_end = time.time() + deadline_s
    for _m, s in items:
        if time.time() > t_end:
            res["timed_out"] = True
            break
        try:
            j, _p, _price, mc = info(_m, max_wait=8, timeout=10)
            ath = num(j.get("ath_price")) * (num(j.get("circulating_supply"), 1e9) or 1e9)
            res["checked"] += 1
            if s["mc"] and ath >= 2 * s["mc"]:
                res["pumped_2x"] += 1
                res["examples"].append(f"{s['sym']} pumped (skip ${s['mc']:.0f} -> ATH ${ath:.0f}, why: {s['why']})")
            if s["mc"] and mc <= 0.3 * s["mc"]:
                res["rugged_70pct"] += 1
        except Exception as e:
            res["errors"] += 1
            if not is_api_error(e):
                report_error("followup", e)
    return res


def heartbeat_once(end):
    now = time.time()
    TICK["heartbeat"] = now
    left_min = max(0, (end - now) / 60)
    with LOCK:
        n_open = len(POS)
        n_closed = len(CLOSED)
        realized = sum(c["pnl_exact"] for c in CLOSED) + sum(p["realized"] for p in POS.values())
        unreal = sum(p.get("value", 0) - p["cost"] for p in POS.values())
        names = ",".join(p["symbol"] or "?" for p in POS.values()) or "-"
        n_watch = len(PENDING)
    blocked = B.blocked_until_ts()
    scan_ago = int(now - TICK["scan"]) if TICK["scan"] else -1
    mon_ago = int(now - TICK["monitor"]) if TICK["monitor"] else -1
    px_ago = int(now - TICK["price"]) if TICK["price"] else -1
    warn = ""
    if mon_ago > 45:
        warn += " MONITOR_STALE"
    if TICK["scan"] and scan_ago > 120:
        warn += " SCAN_STALE"
    with ERR_LOCK:
        n_err = ERR["count"]
    with API_LOCK:
        api_s = f"api {API['calls']}/{API['errors']}t{API['timeouts']} r{API['rate_limited']}"
    log(f"HEARTBEAT up {(now-START)/60:.1f}m left {left_min:.1f}m open {n_open} [{names}] "
        f"closed {n_closed} pnl ${realized:+.2f} unreal ${unreal:+.2f} watching {n_watch} "
        f"scan {scan_ago}s ago mon {mon_ago}s ago px {px_ago}s ago {api_s} errors {n_err}"
        f"{' blocked until '+ts(blocked) if blocked else ''}{warn}")
    write_stats()


def heartbeat_loop(end):
    interval = CFG.get("heartbeat_every_s", 60)
    while not STOP_EVENT.is_set() and not os.path.exists(STOP):
        try:
            heartbeat_once(end)
        except Exception as e:
            report_error("heartbeat", e)
        STOP_EVENT.wait(timeout=interval)


def _on_signal(signum, _frame):
    log(f"signal {signum} received: stopping cleanly")
    STOP_EVENT.set()


def _start_thread(name, target, args=()):
    t = threading.Thread(target=target, args=args, name=name, daemon=True)
    t.start()
    return t


def main():
    if os.path.exists(STOP):
        try:
            os.remove(STOP)
        except Exception:
            pass
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    end = time.time() + CFG["run_minutes"] * 60
    RUN_END[0] = end
    log(f"demo start pid {os.getpid()} (paper only, no wallet, no swaps) "
        f"config: mcap ${CFG['min_mcap']}-{CFG['max_mcap']}, age {CFG['min_age_min']}-{CFG['max_age_min']}m, "
        f"min_score {CFG.get('min_score')}, confirm {CFG.get('confirm_wait_s')}s, "
        f"early_stop {CFG.get('early_stop_pct')}%/{CFG.get('early_stop_window_min')}m, "
        f"time_stop {CFG['time_stop_min']}m, run {CFG['run_minutes']}m, ends {ts(end)}")
    with open(os.path.join(D, "demo.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    sol_usd()
    threads = {"monitor": (monitor_loop, ()), "heartbeat": (heartbeat_loop, (end,))}
    live = {n: _start_thread(n, fn, a) for n, (fn, a) in threads.items()}
    try:
        while time.time() < end and not os.path.exists(STOP) and not STOP_EVENT.is_set():
            t0 = time.time()
            # watchdog: a dead worker thread is a bug -> loud ERROR and restart
            for n, t in list(live.items()):
                if not t.is_alive() and not STOP_EVENT.is_set():
                    report_error(f"watchdog: {n} thread died, restarting", RuntimeError(f"{n} thread not alive"))
                    fn, a = threads[n]
                    live[n] = _start_thread(n, fn, a)
            try:
                scan_once()
            except Exception as e:
                report_error("scan", e)
            write_stats()
            STOP_EVENT.wait(timeout=max(1, CFG["scan_every_s"] - (time.time() - t0)))
    finally:
        STOP_EVENT.set()
        close_all_at_end()
        try:
            with open(STOP, "w") as fh:
                fh.write("done")
        except Exception:
            pass
        write_stats(final=True)
        try:
            fu = followup()
        except Exception as e:
            fu = {"checked": 0, "error": redact(e)}
            report_error("followup", e)
        try:
            with STATS_LOCK:
                _write_stats_locked(True, _sol["v"], extra={"skipped_followup": fu})
        except Exception as e:
            report_error("final stats", e)
        log(f"demo end. closed {len(CLOSED)} open {len(POS)} followup {fu.get('checked', 0)} checked "
            f"errors {ERR['count']}")


if __name__ == "__main__":
    main()
