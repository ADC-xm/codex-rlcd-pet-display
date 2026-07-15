import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
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
STOCK_REFRESH_SECONDS = 10
CODEX_REFRESH_SECONDS = 10
CODEX_ERROR_BACKOFF_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 15
# This can be disabled with CODEX_BLE_ENABLE_QUOTA_READ=0 if Codex account
# authentication becomes unstable on a particular desktop installation.
CODEX_QUOTA_FETCH_ENABLED = os.environ.get("CODEX_BLE_ENABLE_QUOTA_READ", "1") == "1"
PRIMARY_WINDOW_MINS = 5 * 60
SECONDARY_WINDOW_MINS = 7 * 24 * 60
APP_NAME = "Codex BLE Sender"
ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "codex_ble_sender.log"
QUOTA_CACHE_PATH = ROOT / "codex_quota_cache.json"
CODEX_HOME = Path.home() / ".codex"
TOKEN_REFRESH_SECONDS = 10
TOKEN_INPUT_USD_PER_MTOK = 5.00
TOKEN_CACHED_USD_PER_MTOK = 0.50
TOKEN_OUTPUT_USD_PER_MTOK = 30.00
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
STOCK_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stock-refresh")
STOCK_FETCH_FUTURE = None
LAST_TOKEN_RESULT = None
LAST_TOKEN_FETCH_AT = 0

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


def snapshot_has_weekly_quota(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("primary"), dict)
        and item.get("secondary") is None
        and duration_matches(item["primary"].get("windowDurationMins"), SECONDARY_WINDOW_MINS)
    )


def rate_limit_shape(response):
    if not isinstance(response, dict):
        return type(response).__name__
    summary = {"top": sorted(response.keys())}
    for key, value in response.items():
        if isinstance(value, dict):
            summary[key] = sorted(value.keys())
            if key == "rateLimitsByLimitId":
                nested = {}
                for item_key, item in value.items():
                    if not isinstance(item, dict):
                        nested[item_key] = type(item).__name__
                        continue
                    nested[item_key] = {}
                    for window_key in ("primary", "secondary"):
                        window = item.get(window_key)
                        nested[item_key][window_key] = (
                            {"keys": sorted(window.keys()), "duration": window.get("windowDurationMins")}
                            if isinstance(window, dict)
                            else type(window).__name__
                        )
                summary[key] = nested
    return json.dumps(summary, ensure_ascii=True, separators=(",", ":"))


def pick_snapshot(response):
    if not isinstance(response, dict):
        raise RuntimeError("Unexpected Codex response.")

    by_id = response.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        if snapshot_has_quota(by_id.get("codex")):
            return by_id["codex"]
        if snapshot_has_weekly_quota(by_id.get("codex")):
            snapshot = dict(by_id["codex"])
            snapshot["_quota_mode"] = "weekly"
            return snapshot
        for limit_id, item in by_id.items():
            if snapshot_has_quota(item):
                log(f"Using Codex rate-limit snapshot id: {limit_id}")
                return item
            if snapshot_has_weekly_quota(item):
                log(f"Using weekly Codex rate-limit snapshot id: {limit_id}")
                snapshot = dict(item)
                snapshot["_quota_mode"] = "weekly"
                return snapshot

    if snapshot_has_quota(response.get("rateLimits")):
        return response["rateLimits"]
    if snapshot_has_weekly_quota(response.get("rateLimits")):
        snapshot = dict(response["rateLimits"])
        snapshot["_quota_mode"] = "weekly"
        return snapshot

    raise RuntimeError(f"No Codex rate-limit snapshot was returned: {rate_limit_shape(response)}")


def has_valid_quota(primary, secondary):
    return (
        isinstance(primary, dict)
        and isinstance(secondary, dict)
        and 0 <= primary.get("used", -1) <= 100
        and 0 <= primary.get("remaining", -1) <= 100
        and 0 <= secondary.get("used", -1) <= 100
        and 0 <= secondary.get("remaining", -1) <= 100
    )


def has_valid_window(window):
    return (
        isinstance(window, dict)
        and 0 <= window.get("used", -1) <= 100
        and 0 <= window.get("remaining", -1) <= 100
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
    quota_mode = state.get("quota_mode") or "dual"
    if quota_mode == "weekly":
        if not has_valid_window(primary) or not duration_matches(primary.get("duration"), SECONDARY_WINDOW_MINS):
            return None
        return {
            "primary": dict(primary),
            "secondary": dict(secondary) if isinstance(secondary, dict) else empty_quota()["secondary"],
            "plan_type": state.get("plan_type") or "unknown",
            "quota_mode": "weekly",
        }
    if not has_valid_quota(primary, secondary):
        return None
    return {
        "primary": dict(primary),
        "secondary": dict(secondary),
        "plan_type": state.get("plan_type") or "unknown",
        "quota_mode": "dual",
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
    if LAST_GOOD_QUOTA is None and compact and compact.get("quota_mode") != "weekly":
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
        "quota_mode": "dual",
    }


def get_cached_stocks():
    return list(LAST_STOCKS_RESULT) if isinstance(LAST_STOCKS_RESULT, list) else []


def maybe_build_stocks():
    global LAST_STOCKS_RESULT, LAST_STOCKS_FETCH_AT, LAST_STOCKS_ERROR_AT, STOCK_FETCH_FUTURE

    now = time.monotonic()
    cached = get_cached_stocks()

    # Seed the display once, then keep network work off the BLE send loop.
    if not cached:
        try:
            stocks = build_stocks()
            LAST_STOCKS_RESULT = stocks
            LAST_STOCKS_FETCH_AT = time.monotonic()
            LAST_STOCKS_ERROR_AT = 0
            return stocks
        except Exception as exc:
            LAST_STOCKS_ERROR_AT = now
            log(f"Initial stock fetch failed: {exc}")
            return cached

    if STOCK_FETCH_FUTURE is not None and STOCK_FETCH_FUTURE.done():
        try:
            LAST_STOCKS_RESULT = STOCK_FETCH_FUTURE.result()
            LAST_STOCKS_FETCH_AT = time.monotonic()
            LAST_STOCKS_ERROR_AT = 0
        except Exception as exc:
            LAST_STOCKS_ERROR_AT = now
            LAST_STOCKS_FETCH_AT = now
            log(f"Stock fetch failed: {exc}")
        STOCK_FETCH_FUTURE = None

    if (
        STOCK_FETCH_FUTURE is None
        and now - LAST_STOCKS_FETCH_AT >= STOCK_REFRESH_SECONDS
    ):
        STOCK_FETCH_FUTURE = STOCK_FETCH_EXECUTOR.submit(build_stocks)

    return get_cached_stocks()


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


def parse_session_timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def collect_token_usage():
    today = datetime.now().astimezone().date()
    totals = {"total": 0, "input": 0, "cached": 0, "output": 0}
    hourly = [0] * 24
    session_count = 0

    files = []
    for root in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
        if root.exists():
            files.extend(root.rglob("*.jsonl"))

    for path in files:
        previous = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        saw_today = False
        try:
            lines = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue

        with lines:
            for raw in lines:
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                payload = row.get("payload") or {}
                if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                current = ((payload.get("info") or {}).get("total_token_usage") or {})
                if not current:
                    continue
                timestamp = parse_session_timestamp(row.get("timestamp", ""))
                if timestamp is None:
                    continue

                delta = {}
                for key in previous:
                    value = int(current.get(key, 0) or 0)
                    delta[key] = value - previous[key] if value >= previous[key] else value
                    previous[key] = value

                if timestamp.date() != today:
                    continue
                saw_today = True
                uncached = max(0, delta["input_tokens"] - delta["cached_input_tokens"])
                totals["input"] += uncached
                totals["cached"] += max(0, delta["cached_input_tokens"])
                totals["output"] += max(0, delta["output_tokens"])
                token_delta = max(0, delta["total_tokens"])
                totals["total"] += token_delta
                hourly[timestamp.hour] += token_delta

        if saw_today:
            session_count += 1

    usd = (
        totals["input"] * TOKEN_INPUT_USD_PER_MTOK
        + totals["cached"] * TOKEN_CACHED_USD_PER_MTOK
        + totals["output"] * TOKEN_OUTPUT_USD_PER_MTOK
    ) / 1_000_000
    return {
        **totals,
        "usd": round(usd, 2),
        "sessions": session_count,
        "hourly": hourly,
    }


def maybe_collect_token_usage():
    global LAST_TOKEN_RESULT, LAST_TOKEN_FETCH_AT

    now = time.monotonic()
    if LAST_TOKEN_RESULT is not None and now - LAST_TOKEN_FETCH_AT < TOKEN_REFRESH_SECONDS:
        return LAST_TOKEN_RESULT
    try:
        LAST_TOKEN_RESULT = collect_token_usage()
        LAST_TOKEN_FETCH_AT = now
    except Exception as exc:
        log(f"Token log scan failed: {exc}")
        if LAST_TOKEN_RESULT is None:
            LAST_TOKEN_RESULT = {
                "total": 0,
                "input": 0,
                "cached": 0,
                "output": 0,
                "usd": 0.0,
                "sessions": 0,
                "hourly": [0] * 24,
            }
    return LAST_TOKEN_RESULT


def build_payload():
    global LAST_QUOTA_RESULT, LAST_CODEX_FETCH_AT, LAST_CODEX_ERROR_AT

    now = datetime.now()
    monotonic_now = time.monotonic()
    running = is_codex_running()
    stocks = maybe_build_stocks()
    tokens = maybe_collect_token_usage()

    cached_quota = get_cached_quota()
    seed_last_good_from_cache(cached_quota)
    if not CODEX_QUOTA_FETCH_ENABLED:
        quota = cached_quota or empty_quota()
        primary = quota["primary"]
        secondary = quota["secondary"]
        plan_type = quota["plan_type"]
        quota_mode = quota.get("quota_mode", "dual")
        ok = cached_quota is not None
        status = "cached" if cached_quota else "waiting"
        error = "Automatic Codex quota reads are disabled to protect sign-in"
    elif running is False:
        quota = cached_quota or empty_quota()
        primary = quota["primary"]
        secondary = quota["secondary"]
        plan_type = quota["plan_type"]
        quota_mode = quota.get("quota_mode", "dual")
        ok = cached_quota is not None
        status = "cached" if cached_quota else "waiting"
        error = "Codex not running; quota fetch paused"
    elif cached_quota and monotonic_now - LAST_CODEX_FETCH_AT < CODEX_REFRESH_SECONDS:
        primary = cached_quota["primary"]
        secondary = cached_quota["secondary"]
        plan_type = cached_quota["plan_type"]
        quota_mode = cached_quota.get("quota_mode", "dual")
        ok = True
        status = "running"
        error = ""
    elif cached_quota and LAST_CODEX_ERROR_AT and monotonic_now - LAST_CODEX_ERROR_AT < CODEX_ERROR_BACKOFF_SECONDS:
        primary = cached_quota["primary"]
        secondary = cached_quota["secondary"]
        plan_type = cached_quota["plan_type"]
        quota_mode = cached_quota.get("quota_mode", "dual")
        ok = True
        status = "cached"
        error = "Codex quota fetch is backing off"
    else:
        try:
            response = fetch_rate_limits()
            snapshot = pick_snapshot(response)
            quota_mode = snapshot.get("_quota_mode", "dual")
            plan_type = snapshot.get("planType") or "unknown"
            if quota_mode == "weekly":
                primary = normalize_window(snapshot.get("primary"), "7d")
                secondary = normalize_window(None, "-")
                stable = {
                    "primary": primary,
                    "secondary": secondary,
                    "plan_type": plan_type,
                    "quota_mode": "weekly",
                    "cached": False,
                    "error": "",
                }
            else:
                primary = normalize_window(snapshot.get("primary"), "5h")
                secondary = normalize_window(snapshot.get("secondary"), "7d")
                stable = stabilize_quota(primary, secondary, plan_type)
            primary = stable["primary"]
            secondary = stable["secondary"]
            plan_type = stable["plan_type"]
            quota_mode = stable.get("quota_mode", quota_mode)
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
            quota_mode = quota.get("quota_mode", "dual")
            ok = cached_quota is not None
            status = "cached" if cached_quota else "error"
            error = str(exc)[:80]

    return {
        "ok": ok,
        "codex_running": running,
        "status": status,
        "plan_type": plan_type,
        "quota_mode": quota_mode,
        "primary": primary,
        "secondary": secondary,
        "date": now.strftime("%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "updated": now.strftime("%H:%M:%S"),
        "stocks": stocks,
        "tokens": tokens,
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
        "tokens": maybe_collect_token_usage(),
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
        ("tencent", lambda: fetch_tencent_trend(stock["sina"])),
        ("eastmoney", lambda: fetch_trend(stock["secid"])),
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

    trends = {}
    # Fetch all three intraday curves simultaneously. Sequential fallback
    # fetches made a nominal 10-second stock refresh take roughly 30 seconds.
    with ThreadPoolExecutor(max_workers=len(STOCKS), thread_name_prefix="stock-trend") as executor:
        futures = {executor.submit(fetch_best_trend, stock): stock for stock in STOCKS}
        for future in as_completed(futures):
            stock = futures[future]
            try:
                trend_source, trend = future.result()
                trends[stock["code"]] = trend
                if trend_source != source:
                    log(f"{stock['code']} trend source {trend_source}")
            except Exception as exc:
                log(f"{stock['code']} trend failed: {exc}")

    output = []
    for stock in STOCKS:
        quote = quotes.get(stock["code"], {})
        trend = trends.get(stock["code"], [])
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
                    cycle_started = time.monotonic()
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
                    remaining_delay = REFRESH_SECONDS - (time.monotonic() - cycle_started)
                    await asyncio.sleep(max(0, remaining_delay))
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
