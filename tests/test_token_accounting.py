import json
import tempfile
import unittest
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from codex_token_accounting import estimate_call, summarize_usage


class AccountingTests(unittest.TestCase):
    def test_astra_cache_write_and_reasoning(self):
        usage = dict(input_tokens=100000, cached_input_tokens=60000,
                     cache_write_input_tokens=20000, output_tokens=10000,
                     reasoning_output_tokens=9000)
        self.assertAlmostEqual(estimate_call(usage, "gpt-6-astra"), 1.01)
        self.assertAlmostEqual(estimate_call(usage, "gpt-6-astra", "fast"), 2.02)
        self.assertAlmostEqual(estimate_call(usage, "gpt-6-astra", "flex"), 0.505)

    def test_long_context_boundary(self):
        self.assertAlmostEqual(estimate_call(dict(input_tokens=272000, output_tokens=1000), "gpt-6-astra"), 2.77)
        self.assertAlmostEqual(estimate_call(dict(input_tokens=272001, output_tokens=1000), "gpt-6-astra"), 5.51502)
        self.assertIsNone(estimate_call(dict(input_tokens=100), "unknown"))

    def test_mixed_models_duplicates_and_midnight(self):
        now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        def event(stamp, total, last):
            return dict(type="event_msg", timestamp=stamp.isoformat(), payload=dict(
                type="token_count", rate_limits=dict(limit_id="codex"),
                info=dict(total_token_usage=total, last_token_usage=last)))
        small = dict(input_tokens=10000, output_tokens=1000)
        cumulative = dict(input_tokens=20000, output_tokens=2000)
        newer = dict(input_tokens=40000, output_tokens=4000)
        rows = [dict(type="turn_context", payload=dict(model="gpt-5.6-sol")),
                event(now-timedelta(days=1), small, small),
                event(now, cumulative, small), event(now, cumulative, small),
                dict(type="turn_context", payload=dict(model="gpt-6-astra")),
                event(now+timedelta(hours=1), newer, dict(input_tokens=20000, output_tokens=2000))]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"test.jsonl"
            path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            result = summarize_usage([path], now.date())
        self.assertEqual(result["total"], 33000)
        self.assertEqual(sum(result["hourly"]), result["total"])
        self.assertEqual(result["hourly"][10:12], [11000, 22000])
        self.assertEqual(result["usd"], 0.36)
        self.assertEqual(result["pricing_label"], "MIXED EST.")
        self.assertEqual(result["calls"], 2)

    def test_cumulative_fallback_and_unknown_price(self):
        now = datetime.now().astimezone()
        rows = [dict(type="turn_context", payload=dict(model="new-unknown"))]
        for value in (100, 150):
            rows.append(dict(type="event_msg", timestamp=now.isoformat(), payload=dict(
                type="token_count", info=dict(total_token_usage=dict(input_tokens=value)))))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"test.jsonl"
            path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
            result = summarize_usage([path], now.date())
        self.assertEqual(result["total"], 150)
        self.assertIsNone(result["usd"])
        self.assertEqual(result["unpriced_calls"], 2)


if __name__ == "__main__":
    unittest.main()
