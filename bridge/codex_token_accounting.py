"""Local token accounting; API-equivalent estimates, not subscription charges."""

import json
from datetime import datetime

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
PRICING_VERIFIED = "2026-09-07"
# Standard USD per million: ordinary input, cache reads, cache writes, output.
RATES = {
    "gpt-6-astra": (10.0, 1.0, 12.5, 50.0),
    "gpt-5.6-sol": (4.0, 0.4, 5.0, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 2.5, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
    "gpt-5.5": (5.0, 0.5, None, 30.0),
    "gpt-5.4": (2.5, 0.25, None, 15.0),
    "gpt-5.3-codex": (1.75, 0.175, None, 14.0),
}
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "total_tokens")


def count(usage, key):
    return max(0, int(usage.get(key, 0) or 0))


def token_parts(usage):
    inputs = count(usage, "input_tokens")
    cached = min(inputs, count(usage, "cached_input_tokens"))
    written = min(inputs - cached, count(usage, "cache_write_input_tokens"))
    output = count(usage, "output_tokens")
    # Input includes cache reads/writes; output already includes reasoning.
    return inputs - cached - written, cached, written, output


def estimate_call(usage, model, tier=None):
    rates = RATES.get(model)
    if rates is None:
        return None
    ordinary, cached, written, output = token_parts(usage)
    if written and rates[2] is None:
        return None
    long_context = count(usage, "input_tokens") > 272_000
    if long_context and model == "gpt-5.3-codex":
        return None
    input_multiplier, output_multiplier = (2, 1.5) if long_context else (1, 1)
    tier = tier or "default"
    if tier in ("default", "standard", "auto"):
        tier_multiplier = 1
    elif tier in ("flex", "batch"):
        tier_multiplier = 0.5
    elif tier in ("fast", "priority"):
        tier_multiplier = 2.5 if model == "gpt-5.5" else 2
    else:
        return None
    return (
        (ordinary * rates[0] + cached * rates[1] + written * (rates[2] or 0))
        * input_multiplier + output * rates[3] * output_multiplier
    ) * tier_multiplier / 1_000_000


def iter_session_calls(path):
    model, tier = None, None
    previous_by_limit = {}
    with path.open(encoding="utf-8", errors="ignore") as lines:
        for raw in lines:
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            payload = row.get("payload") or {}
            if row.get("type") == "turn_context":
                model = payload.get("model") or model
                tier = payload.get("service_tier")
                continue
            if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            current = info.get("total_token_usage") or {}
            last = info.get("last_token_usage") or {}
            if not current and not last:
                continue
            try:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone()
            except (KeyError, ValueError):
                continue
            limit_id = (payload.get("rate_limits") or {}).get("limit_id") or "codex"
            previous = previous_by_limit.get(limit_id)
            if current:
                normalized = {key: count(current, key) for key in USAGE_KEYS}
                previous_by_limit[limit_id] = normalized
                # Some notifications repeat the last call solely to update quota.
                if previous == normalized:
                    continue
                delta = {key: max(0, value - (previous or {}).get(key, 0))
                         for key, value in normalized.items()}
            else:
                delta = {}
            usage = last or delta
            if usage:
                yield stamp, model, tier, usage


def summarize_usage(files, today):
    totals = {"total": 0, "input": 0, "cached": 0, "cache_write": 0, "output": 0}
    hourly = [0] * 24
    usd, sessions, unpriced_calls, calls = 0.0, 0, 0, 0
    models = set()
    assumed_tier_calls = 0
    for path in files:
        saw_today = False
        for stamp, model, tier, usage in iter_session_calls(path):
            if stamp.date() != today:
                continue
            ordinary, cached, written, output = token_parts(usage)
            total = ordinary + cached + written + output
            if not total:
                continue
            saw_today = True
            calls += 1
            models.add(model or "unknown")
            assumed_tier_calls += tier in (None, "auto")
            totals["input"] += ordinary + written
            totals["cached"] += cached
            totals["cache_write"] += written
            totals["output"] += output
            totals["total"] += total
            hourly[stamp.hour] += total
            cost = estimate_call(usage, model, tier)
            if cost is None:
                unpriced_calls += 1
            else:
                usd += cost
        sessions += saw_today
    labels = {"gpt-6-astra": "ASTRA EST.", "gpt-5.6-sol": "SOL EST.",
              "gpt-5.6-terra": "TERRA EST.", "gpt-5.6-luna": "LUNA EST."}
    label = labels.get(next(iter(models)), "MODEL EST.") if len(models) == 1 else "MIXED EST."
    return {**totals, "usd": None if unpriced_calls else round(usd, 2),
            "known_usd": round(usd, 2), "sessions": sessions, "hourly": hourly,
            "pricing_label": "PRICE UNKNOWN" if unpriced_calls else label,
            "models": sorted(models), "unpriced_calls": unpriced_calls,
            "calls": calls, "assumed_standard_calls": assumed_tier_calls}
