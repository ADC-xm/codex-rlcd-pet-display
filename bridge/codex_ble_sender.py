import asyncio
import json
import math
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests
from bleak import BleakClient, BleakScanner


DEVICE_NAME = "ESP32S3-Codex"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
REFRESH_SECONDS = 10
STOCK_REFRESH_SECONDS = 60
CODEX_REFRESH_SECONDS = 30
CODEX_ERROR_BACKOFF_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 15
PRIMARY_WINDOW_MINS = 5 * 60
SECONDARY_WINDOW_MINS = 7 * 24 * 60
APP_NAME = "Codex BLE Sender"
ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "codex_ble_sender.log"
QUOTA_CACHE_PATH = ROOT / "codex_quota_cache.json"
BLE_CHUNK_SIZE = 160
HTTP = requests.Session()
HTTP.trust_env = False
LAST_GOOD_QUOTA = None
PENDING_QUOTA = None
LAST_QUOTA_RESULT = None
LAST_CODEX_FETCH_AT = 0
LAST_CODEX_ERROR_AT = 0
LAST_STOCKS_RESULT = []
LAST_STOCKS_FETCH_AT = 0
LAST_STOCKS_ERROR_AT = 0

STOCKS = [
    {"name": "上证指数", "code": "000001", "secid": "1.000001"},
    {"name": "赛力斯", "code": "601127", "secid": "1.601127"},
    {"name": "紫金矿业", "code": "601899", "secid": "1.601899"},
]

STOCKS = [
    {"name": "\u4e0a\u8bc1\u6307\u6570", "code": "000001", "secid": "1.000001", "sina": "sh000001"},
    {"name": "\u8d5b\u529b\u65af", "code": "601127", "secid": "1.601127", "sina": "sh601127"},
    {"name": "\u7d2b\u91d1\u77ff\u4e1a", "code": "601899", "secid": "1.601899", "sina": "sh601899"},
]


def log(message):
    text = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(text, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def load_codex_env():
    env = os.environ.copy()
    env_path = Path.home() / ".codex" / ".env"
    if not env_path.exists():
        return env

    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def find_codex_exe():
    candidates = []
    env_path = os.environ.get("CODEX_CLI_PATH")
    if env_path:
        candidates.append(Path(env_path))

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "OpenAI" / "Codex" / "bin" / "codex.exe")

    extensions = Path.home() / ".vscode" / "extensions"
    if extensions.exists():
        for ext in sorted(extensions.glob("openai.chatgpt-*-win32-x64"), reverse=True):
            candidates.append(ext / "bin" / "windows-x86_64" / "codex.exe")

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "codex"


def send_jsonl(proc, payload, pending_id):
    payload = dict(payload)
    payload["id"] = pending_id
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def read_response(proc, expected_id, deadline):
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != expected_id:
            continue
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                raise RuntimeError(error.get("message") or json.dumps(error, ensure_ascii=False))
            raise RuntimeError(str(error))
        return message.get("result")
    raise TimeoutError("Codex app-server did not respond in time.")


def fetch_rate_limits():
    codex_exe = find_codex_exe()
    env = load_codex_env()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        [codex_exe, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
    )
    try:
        deadline = time.time() + REQUEST_TIMEOUT_SECONDS
        send_jsonl(
            proc,
            {
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-ble-sender",
                        "title": APP_NAME,
                        "version": "1.0.0",
                    },
                    "capabilities": None,
                },
            },
            1,
        )
        read_response(proc, 1, deadline)
        send_jsonl(proc, {"method": "account/rateLimits/read"}, 2)
        return read_response(proc, 2, deadline)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def clamp_percent(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, round(number)))


def normalize_window(data, fallback_label):
    if not isinstance(data, dict):
        return {"label": fallback_label, "used": -1, "remaining": -1, "reset": "-", "reset_ts": None, "duration": None}

    used = clamp_percent(data.get("usedPercent"))
    if used is None:
        used = -1

    duration = data.get("windowDurationMins")
    label = fallback_label
    if duration:
        if duration >= 60 * 24:
            label = f"{round(duration / 60 / 24)}d"
        elif duration >= 60:
            label = f"{round(duration / 60)}h"

    reset_text = "-"
    reset_ts = None
    resets_at = data.get("resetsAt")
    if resets_at:
        try:
            reset_ts = float(resets_at)
            reset_text = datetime.fromtimestamp(reset_ts).strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            reset_text = str(resets_at)

    return {
        "label": label,
        "used": used,
        "remaining": max(0, 100 - used) if used >= 0 else -1,
        "reset": reset_text,
        "reset_ts": reset_ts,
        "duration": duration,
    }


def snapshot_has_quota(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("primary"), dict)
        and isinstance(item.get("secondary"), dict)
    )


def pick_snapshot(response):
    if not isinstance(response, dict):
        raise RuntimeError("Unexpected Codex response.")

    by_id = response.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        if snapshot_has_quota(by_id.get("codex")):
            return by_id["codex"]
        for limit_id, item in by_id.items():
            if snapshot_has_quota(item):
                log(f"Using Codex rate-limit snapshot id: {limit_id}")
                return item

    if snapshot_has_quota(response.get("rateLimits")):
        return response["rateLimits"]

    raise RuntimeError("No Codex rate-limit snapshot was returned.")


def has_valid_quota(primary, secondary):
    return (
        isinstance(primary, dict)
        and isinstance(secondary, dict)
        and 0 <= primary.get("used", -1) <= 100
        and 0 <= primary.get("remaining", -1) <= 100
        and 0 <= secondary.get("used", -1) <= 100
        and 0 <= secondary.get("remaining", -1) <= 100
    )


def duration_matches(value, expected):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return True
    tolerance = 10 if expected == PRIMARY_WINDOW_MINS else 90
    return abs(duration - expected) <= tolerance


def has_expected_windows(primary, secondary):
    return (
        duration_matches(primary.get("duration"), PRIMARY_WINDOW_MINS)
        and duration_matches(secondary.get("duration"), SECONDARY_WINDOW_MINS)
    )


def quota_key(primary, secondary):
    return (
        primary.get("used"),
        primary.get("remaining"),
        primary.get("reset"),
        secondary.get("used"),
        secondary.get("remaining"),
        secondary.get("reset"),
    )


def reset_advanced(old_window, new_window, expected_duration_mins):
    old_ts = old_window.get("reset_ts")
    new_ts = new_window.get("reset_ts")
    try:
        old_ts = float(old_ts)
        new_ts = float(new_ts)
    except (TypeError, ValueError):
        return False

    # Codex's quota windows behave like rolling windows: after real recovery,
    # the reset timestamp may move forward by minutes or hours, not always by
    # a full 5h/7d period. A clear forward move is enough evidence that the
    # high remaining value is a fresh server state rather than a stale spike.
    return new_ts - old_ts >= 60


def jump_has_reset_evidence(current, previous):
    if not previous:
        return False

    saw_jump = False
    checks = (
        ("primary", PRIMARY_WINDOW_MINS),
        ("secondary", SECONDARY_WINDOW_MINS),
    )
    for key, duration in checks:
        old_window = previous[key]
        new_window = current[key]
        jump = new_window["remaining"] - old_window["remaining"]
        if jump < 25:
            continue
        saw_jump = True
        if not reset_advanced(old_window, new_window, duration):
            return False
    return saw_jump


def looks_like_bad_full_spike(current, previous):
    if not previous:
        return False

    primary = current["primary"]
    secondary = current["secondary"]
    old_primary = previous["primary"]
    old_secondary = previous["secondary"]

    primary_jump = primary["remaining"] - old_primary["remaining"]
    secondary_jump = secondary["remaining"] - old_secondary["remaining"]

    # Real 5h resets can jump upward by themselves. The bad Codex app-server
    # sample we see in logs makes both windows look almost full for one cycle.
    return (
        primary["remaining"] >= 95
        and secondary["remaining"] >= 95
        and primary_jump >= 5
        and secondary_jump >= 25
        and not jump_has_reset_evidence(current, previous)
    )


def describe_quota(current):
    primary = current["primary"]
    secondary = current["secondary"]
    return (
        f"5h {primary.get('remaining')}% reset={primary.get('reset')} dur={primary.get('duration')}; "
        f"7d {secondary.get('remaining')}% reset={secondary.get('reset')} dur={secondary.get('duration')}"
    )


def stabilize_quota(primary, secondary, plan_type):
    global LAST_GOOD_QUOTA, PENDING_QUOTA

    if not has_valid_quota(primary, secondary):
        if LAST_GOOD_QUOTA:
            cached = dict(LAST_GOOD_QUOTA)
            cached["cached"] = True
            cached["error"] = "invalid quota sample; using previous"
            return cached
        raise RuntimeError("Codex quota sample was incomplete.")

    if not has_expected_windows(primary, secondary):
        if LAST_GOOD_QUOTA:
            cached = dict(LAST_GOOD_QUOTA)
            cached["cached"] = True
            cached["error"] = (
                f"unexpected quota windows "
                f"5h={primary.get('duration')}, 7d={secondary.get('duration')}"
            )
            log(cached["error"])
            return cached
        raise RuntimeError(
            f"Codex quota windows did not match expected durations: "
            f"5h={primary.get('duration')}, 7d={secondary.get('duration')}"
        )

    current = {
        "primary": primary,
        "secondary": secondary,
        "plan_type": plan_type,
        "cached": False,
        "error": "",
    }

    if looks_like_bad_full_spike(current, LAST_GOOD_QUOTA):
        PENDING_QUOTA = current
        cached = dict(LAST_GOOD_QUOTA)
        cached["cached"] = True
        cached["error"] = (
            f"ignored one-cycle quota spike "
            f"{describe_quota(current)}"
        )
        log(cached["error"])
        return cached

    if LAST_GOOD_QUOTA and jump_has_reset_evidence(current, LAST_GOOD_QUOTA):
        log(
            "Accepted quota reset evidence "
            f"5h reset {LAST_GOOD_QUOTA['primary'].get('reset')} -> {primary.get('reset')}, "
            f"7d reset {LAST_GOOD_QUOTA['secondary'].get('reset')} -> {secondary.get('reset')}"
        )

    LAST_GOOD_QUOTA = current
    PENDING_QUOTA = None
    return current


def compact_quota_state(state):
    if not isinstance(state, dict):
        return None
    primary = state.get("primary")
    secondary = state.get("secondary")
    if not has_valid_quota(primary, secondary):
        return None
    return {
        "primary": dict(primary),
        "secondary": dict(secondary),
        "plan_type": state.get("plan_type") or "unknown",
    }


def save_quota_cache(state):
    compact = compact_quota_state(state)
    if not compact:
        return
    compact["saved_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        QUOTA_CACHE_PATH.write_text(json.dumps(compact, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log(f"Could not save quota cache: {exc}")


def load_quota_cache():
    try:
        data = json.loads(QUOTA_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return compact_quota_state(data)


def get_cached_quota():
    return (
        compact_quota_state(LAST_QUOTA_RESULT)
        or compact_quota_state(LAST_GOOD_QUOTA)
        or load_quota_cache()
    )


def seed_last_good_from_cache(state):
    global LAST_GOOD_QUOTA
    compact = compact_quota_state(state)
    if LAST_GOOD_QUOTA is None and compact:
        LAST_GOOD_QUOTA = {
            "primary": compact["primary"],
            "secondary": compact["secondary"],
            "plan_type": compact["plan_type"],
            "cached": False,
            "error": "",
        }


def empty_quota():
    return {
        "primary": {"label": "5h", "used": -1, "remaining": -1, "reset": "-"},
        "secondary": {"label": "7d", "used": -1, "remaining": -1, "reset": "-"},
        "plan_type": "-",
    }


def get_cached_stocks():
    return list(LAST_STOCKS_RESULT) if isinstance(LAST_STOCKS_RESULT, list) else []


def maybe_build_stocks():
    global LAST_STOCKS_RESULT, LAST_STOCKS_FETCH_AT, LAST_STOCKS_ERROR_AT

    now = time.monotonic()
    cached = get_cached_stocks()
    if cached and now - LAST_STOCKS_FETCH_AT < STOCK_REFRESH_SECONDS:
        return cached

    try:
        stocks = build_stocks()
        LAST_STOCKS_RESULT = stocks
        LAST_STOCKS_FETCH_AT = time.monotonic()
        LAST_STOCKS_ERROR_AT = 0
        return stocks
    except Exception as exc:
        LAST_STOCKS_ERROR_AT = now
        log(f"Stock fetch failed: {exc}")
        return cached


def is_codex_running():
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "codex.exe" in result.stdout.lower()
    except Exception:
        return None


def build_payload():
    global LAST_QUOTA_RESULT, LAST_CODEX_FETCH_AT, LAST_CODEX_ERROR_AT

    now = datetime.now()
    monotonic_now = time.monotonic()
    running = is_codex_running()
    stocks = maybe_build_stocks()

    cached_quota = get_cached_quota()
    seed_last_good_from_cache(cached_quota)
    if running is False:
        quota = cached_quota or empty_quota()
        primary = quota["primary"]
        secondary = quota["secondary"]
        plan_type = quota["plan_type"]
        ok = cached_quota is not None
        status = "cached" if cached_quota else "waiting"
        error = "Codex not running; quota fetch paused"
    elif cached_quota and monotonic_now - LAST_CODEX_FETCH_AT < CODEX_REFRESH_SECONDS:
        primary = cached_quota["primary"]
        secondary = cached_quota["secondary"]
        plan_type = cached_quota["plan_type"]
        ok = True
        status = "running"
        error = ""
    elif cached_quota and LAST_CODEX_ERROR_AT and monotonic_now - LAST_CODEX_ERROR_AT < CODEX_ERROR_BACKOFF_SECONDS:
        primary = cached_quota["primary"]
        secondary = cached_quota["secondary"]
        plan_type = cached_quota["plan_type"]
        ok = True
        status = "cached"
        error = "Codex quota fetch is backing off"
    else:
        try:
            response = fetch_rate_limits()
            snapshot = pick_snapshot(response)
            primary = normalize_window(snapshot.get("primary"), "5h")
            secondary = normalize_window(snapshot.get("secondary"), "7d")
            plan_type = snapshot.get("planType") or "unknown"
            stable = stabilize_quota(primary, secondary, plan_type)
            primary = stable["primary"]
            secondary = stable["secondary"]
            plan_type = stable["plan_type"]
            ok = True
            status = "cached" if stable["cached"] else "running"
            error = stable["error"]
            LAST_CODEX_FETCH_AT = monotonic_now
            LAST_CODEX_ERROR_AT = 0
            LAST_QUOTA_RESULT = compact_quota_state(stable)
            if not stable["cached"]:
                save_quota_cache(stable)
        except Exception as exc:
            LAST_CODEX_ERROR_AT = monotonic_now
            log(f"Codex quota fetch failed: {exc}")
            quota = cached_quota or empty_quota()
            primary = quota["primary"]
            secondary = quota["secondary"]
            plan_type = quota["plan_type"]
            ok = cached_quota is not None
            status = "cached" if cached_quota else "error"
            error = str(exc)[:80]

    return {
        "ok": ok,
        "codex_running": running,
        "status": status,
        "plan_type": plan_type,
        "primary": primary,
        "secondary": secondary,
        "date": now.strftime("%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "updated": now.strftime("%H:%M:%S"),
        "stocks": stocks,
        "error": error,
    }


def build_error_payload(exc):
    now = datetime.now()
    try:
        stocks = build_stocks()
    except Exception:
        stocks = []
    return {
        "ok": False,
        "codex_running": is_codex_running(),
        "status": "error",
        "plan_type": "-",
        "primary": {"label": "5h", "used": -1, "remaining": -1, "reset": "-"},
        "secondary": {"label": "7d", "used": -1, "remaining": -1, "reset": "-"},
        "date": now.strftime("%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "updated": now.strftime("%H:%M:%S"),
        "error": str(exc)[:80],
        "stocks": stocks,
    }


def fetch_json(url):
    errors = []
    for _ in range(3):
        try:
            response = HTTP.get(
                url,
                timeout=8,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json,text/plain,*/*",
                    "Connection": "close",
                },
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            errors.append(exc)
            time.sleep(0.5)

    try:
        return fetch_json_powershell(url)
    except Exception as exc:
        errors.append(exc)
    raise RuntimeError(" / ".join(str(item) for item in errors[-2:]))


def fetch_text(url, referer="https://quote.eastmoney.com/"):
    errors = []
    for _ in range(2):
        try:
            response = HTTP.get(
                url,
                timeout=8,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": referer,
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            response.raise_for_status()
            if response.encoding is None or response.encoding.lower() in ("iso-8859-1", "ascii"):
                response.encoding = "gb18030"
            return response.text
        except Exception as exc:
            errors.append(exc)
            time.sleep(0.5)

    try:
        return fetch_text_powershell(url)
    except Exception as exc:
        errors.append(exc)
    raise RuntimeError(" / ".join(str(item) for item in errors[-2:]))


def fetch_json_powershell(url):
    return json.loads(fetch_text_powershell(url))


def fetch_text_powershell(url):
    ps = (
        "$ProgressPreference='SilentlyContinue';"
        f"$r=Invoke-WebRequest -UseBasicParsing -Uri {json.dumps(url)} -TimeoutSec 15;"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$r.Content"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def fetch_quotes():
    params = urlencode(
        {
            "fltt": "2",
            "secids": ",".join(item["secid"] for item in STOCKS),
            "fields": "f12,f14,f2,f3,f4,f13",
            "_": str(int(time.time() * 1000)),
        }
    )
    data = fetch_json(f"https://push2.eastmoney.com/api/qt/ulist.np/get?{params}")
    return {item.get("f12"): item for item in data.get("data", {}).get("diff", [])}


def fetch_sina_quotes():
    symbols = ",".join(stock["sina"] for stock in STOCKS)
    text = fetch_text(f"https://hq.sinajs.cn/list={symbols}", "https://finance.sina.com.cn/")
    quotes = {}
    for match in re.finditer(r'var hq_str_(\w+)="([^"]*)"', text):
        symbol = match.group(1)
        fields = match.group(2).split(",")
        if len(fields) < 4 or not fields[0]:
            continue
        stock = next((item for item in STOCKS if item["sina"] == symbol), None)
        if not stock:
            continue
        try:
            previous = float(fields[2])
            current = float(fields[3])
        except ValueError:
            continue
        change = current - previous
        pct = (change / previous * 100) if previous else 0.0
        quotes[stock["code"]] = {"f2": current, "f3": pct, "f4": change, "source": "sina"}
    return quotes


def fetch_trend(secid):
    params = urlencode(
        {
            "secid": secid,
            "fields1": "f1,f2,f3",
            "fields2": "f51,f53",
            "iscr": "0",
            "iscca": "0",
            "_": str(int(time.time() * 1000)),
        }
    )
    data = fetch_json(f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?{params}")
    prices = []
    for raw in data.get("data", {}).get("trends", []) or []:
        parts = raw.split(",")
        if len(parts) >= 2:
            try:
                prices.append(float(parts[1]))
            except ValueError:
                pass
    return prices


def fetch_tencent_trend(symbol):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
    data = fetch_json(url)
    rows = (
        data.get("data", {})
        .get(symbol, {})
        .get("data", {})
        .get("data", [])
    )
    prices = []
    for raw in rows:
        parts = str(raw).split()
        if len(parts) >= 2:
            try:
                prices.append(float(parts[1]))
            except ValueError:
                pass
    return prices


def fetch_sina_trend(symbol):
    url = f"https://quotes.sina.cn/cn/api/jsonp.php/=/CN_MinlineService.getMinlineData?symbol={symbol}"
    text = fetch_text(url, "https://finance.sina.com.cn/")
    body = text
    start = text.find("([")
    end = text.rfind("])")
    if start >= 0 and end > start:
        body = text[start + 1 : end + 1]
    prices = []
    try:
        data = json.loads(body)
        for item in data:
            value = item.get("price") or item.get("p") if isinstance(item, dict) else None
            if value is not None:
                prices.append(float(value))
    except Exception:
        for value in re.findall(r'"(?:price|p)"\s*:\s*"?([0-9.]+)', text):
            try:
                prices.append(float(value))
            except ValueError:
                pass
    return prices


def fetch_best_trend(stock):
    sources = (
        ("eastmoney", lambda: fetch_trend(stock["secid"])),
        ("tencent", lambda: fetch_tencent_trend(stock["sina"])),
        ("sina", lambda: fetch_sina_trend(stock["sina"])),
    )
    errors = []
    for name, fetcher in sources:
        try:
            prices = fetcher()
            if prices:
                return name, prices
            errors.append(f"{name}: empty")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(" / ".join(errors[-3:]))


def compress_trend(values, count=32):
    if not values:
        return []
    if len(values) > count:
        step = len(values) / count
        values = [values[min(len(values) - 1, int(i * step))] for i in range(count)]
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return [50 for _ in values]
    return [max(0, min(100, round((value - low) * 100 / (high - low)))) for value in values]


def build_stocks():
    source = "eastmoney"
    try:
        quotes = fetch_quotes()
    except Exception as exc:
        log(f"Eastmoney quote failed, trying Sina: {exc}")
        quotes = fetch_sina_quotes()
        source = "sina"

    output = []
    for stock in STOCKS:
        quote = quotes.get(stock["code"], {})
        trend = []
        try:
            trend_source, trend = fetch_best_trend(stock)
            if trend_source != source:
                log(f"{stock['code']} trend source {trend_source}")
        except Exception as exc:
            log(f"{stock['code']} trend failed: {exc}")
        if not trend and quote.get("f2") not in (None, "--"):
            try:
                current = float(quote.get("f2"))
                trend = [current for _ in range(8)]
            except (TypeError, ValueError):
                pass
        output.append(
            {
                "n": stock["name"],
                "c": stock["code"],
                "p": quote.get("f2", "--"),
                "z": quote.get("f3", "--"),
                "d": quote.get("f4", "--"),
                "t": compress_trend(trend),
            }
        )
    log(f"Stocks source {source}, count {sum(1 for item in output if item.get('p') != '--')}/{len(output)}")
    return output


async def find_device():
    log(f"Scanning for {DEVICE_NAME}...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name == DEVICE_NAME or ad.local_name == DEVICE_NAME,
        timeout=20,
    )
    if not device:
        raise RuntimeError(f"Could not find BLE device named {DEVICE_NAME}.")
    return device


async def send_loop():
    while True:
        try:
            device = await find_device()
        except Exception as exc:
            log(f"Scan failed: {exc}. Retrying in 5 seconds.")
            await asyncio.sleep(5)
            continue

        log(f"Connecting to {device.name or DEVICE_NAME} [{device.address}]")

        try:
            async with BleakClient(device) as client:
                log(f"Connected. Sending Codex and stock status every {REFRESH_SECONDS} seconds.")

                def on_notify(_, data):
                    try:
                        log("ESP32: " + data.decode("utf-8", errors="replace"))
                    except Exception:
                        pass

                try:
                    await client.start_notify(TX_UUID, on_notify)
                except Exception as exc:
                    log(f"Notify unavailable: {exc}")

                while client.is_connected:
                    try:
                        payload = build_payload()
                    except Exception as exc:
                        payload = build_error_payload(exc)

                    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                    encoded = line.encode("utf-8")
                    for start in range(0, len(encoded), BLE_CHUNK_SIZE):
                        await client.write_gatt_char(RX_UUID, encoded[start : start + BLE_CHUNK_SIZE], response=True)
                        await asyncio.sleep(0.03)
                    primary = payload.get("primary") or {}
                    secondary = payload.get("secondary") or {}
                    log(
                        f"Sent {payload.get('updated')} | "
                        f"5h {primary.get('remaining')}% | 7d {secondary.get('remaining')}% | "
                        f"{payload.get('status')} | "
                        f"reset {primary.get('reset')}/{secondary.get('reset')} | "
                        f"dur {primary.get('duration')}/{secondary.get('duration')}"
                    )
                    await asyncio.sleep(REFRESH_SECONDS)
        except Exception as exc:
            log(f"Connection failed/lost: {exc}. Reconnecting soon.")

        log("Disconnected. Reconnecting soon...")
        await asyncio.sleep(3)


def main():
    try:
        asyncio.run(send_loop())
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
