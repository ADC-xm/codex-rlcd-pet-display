import importlib.util
import json
import tempfile
import sys
from datetime import datetime, timedelta
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "bridge" / "codex_ble_sender.py"
sys.path.insert(0, str(BRIDGE_PATH.parent))
spec = importlib.util.spec_from_file_location("codex_ble_sender_test", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def token_row(timestamp, total, input_tokens, output_tokens):
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": output_tokens,
        "total_tokens": total,
    }
    return {
        "timestamp": timestamp.isoformat(),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage, "last_token_usage": usage},
            "rate_limits": {"limit_id": "codex"},
        },
    }


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def main():
    now = datetime.now().astimezone()
    with tempfile.TemporaryDirectory() as temp_dir:
        codex_home = Path(temp_dir)
        session_dir = codex_home / "sessions" / now.strftime("%Y/%m/%d")
        normal = session_dir / "normal.jsonl"
        replay = session_dir / "forked.jsonl"
        incomplete = session_dir / "incomplete.jsonl"

        write_jsonl(
            normal,
            [
                {"timestamp": now.isoformat(), "type": "session_meta", "payload": {"thread_source": "user"}},
                token_row(now - timedelta(seconds=2), 100, 90, 10),
                token_row(now - timedelta(seconds=1), 150, 135, 15),
            ],
        )
        write_jsonl(
            replay,
            [
                {
                    "timestamp": now.isoformat(),
                    "type": "session_meta",
                    "payload": {"forked_from_id": "parent", "thread_source": "subagent"},
                },
                token_row(now, 5_000_000_000, 4_900_000_000, 100_000_000),
            ],
        )
        write_jsonl(incomplete, [token_row(now, 5_000_000_000, 4_900_000_000, 100_000_000)])

        original_home = bridge.CODEX_HOME
        bridge.CODEX_HOME = codex_home
        try:
            result = bridge.collect_token_usage()
        finally:
            bridge.CODEX_HOME = original_home

        assert result["total"] == 250, result
        assert result["hourly"][now.hour] == 250, result
        assert result["sessions"] == 1, result

    bridge.LAST_TOKEN_RESULT = None
    bridge.LAST_TOKEN_DATE = None
    overflow = bridge.stabilize_token_usage(
        {"total": 5_000_000_000, "input": 5_000_000_000, "cached": 0, "output": 0, "hourly": [5_000_000_000]}
    )
    assert overflow["total"] == bridge.UINT32_MAX, overflow
    assert overflow["hourly"][0] == bridge.UINT32_MAX, overflow

    bridge.LAST_TOKEN_RESULT = {"total": 200, "hourly": [200] + [0] * 23}
    bridge.LAST_TOKEN_DATE = now.date().isoformat()
    bridge.PENDING_TOKEN_RESULT = None
    bridge.PENDING_TOKEN_KIND = None
    bridge.PENDING_TOKEN_COUNT = 0
    correction = {"total": 100, "sessions": 1, "hourly": [100] + [0] * 23}
    stable = bridge.stabilize_token_usage(correction)
    assert stable["total"] == 200, stable
    stable = bridge.stabilize_token_usage(correction)
    assert stable["total"] == 200, stable
    stable = bridge.stabilize_token_usage(correction)
    assert stable["total"] == 100, stable
    print("token usage regression checks passed")


if __name__ == "__main__":
    main()
