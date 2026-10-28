import importlib.util
from pathlib import Path
import subprocess
import sys
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

BRIDGE_DIR = Path(__file__).resolve().parents[1] / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))
import codex_quota_refresh as quota


class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fetch):
        future = Future()
        self.calls.append(future)
        return future


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 1
        self.executor = Executor()
        self.refresh = quota.QuotaRefresh(clock=lambda: self.now, executor=self.executor)
        self.events = []

    def poll(self):
        return self.refresh.poll(self.events.append)

    def test_nonblocking_single_flight(self):
        for _ in range(10):
            self.assertIsNone(self.poll())
        self.assertEqual(len(self.executor.calls), 1)

    def test_refresh_without_session_activity(self):
        self.poll()
        self.executor.calls[0].set_result({"quota": 43})
        self.assertEqual(self.poll(), {"quota": 43})
        self.now += 59
        self.poll()
        self.assertEqual(len(self.executor.calls), 1)
        self.now += 1
        self.assertEqual(self.poll(), {"quota": 43})
        self.assertEqual(len(self.executor.calls), 2)
        self.executor.calls[1].set_result({"quota": 42})
        self.assertEqual(self.poll(), {"quota": 42})

    def test_failure_backoff_and_recovery(self):
        self.poll()
        self.executor.calls[0].set_exception(RuntimeError("offline"))
        self.assertIsNone(self.poll())
        self.assertEqual(self.refresh.error, "offline")
        self.now += 119
        self.poll()
        self.assertEqual(len(self.executor.calls), 1)
        self.now += 1
        self.poll()
        self.executor.calls[1].set_result({"quota": 42})
        self.assertEqual(self.poll(), {"quota": 42})
        self.assertEqual(self.refresh.failures, 0)

    def test_expired_response_is_not_fresh(self):
        self.poll()
        self.executor.calls[0].set_result({"quota": 43})
        self.poll()
        self.now += 121
        self.assertIsNone(self.poll())

    def test_live_response_precedes_old_logs(self):
        spec = importlib.util.spec_from_file_location("sender_candidate", BRIDGE_DIR / "codex_ble_sender.py")
        sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sender)
        sender.QUOTA_REFRESH = self.refresh
        sender.log = self.events.append
        self.poll()
        self.executor.calls[0].set_result({"quota": 43})
        with patch.object(sender.Path, "rglob", side_effect=AssertionError("should not read old logs")):
            self.assertEqual(sender.fetch_rate_limits(), {"quota": 43})
            self.now += 121
            with self.assertRaisesRegex(RuntimeError, "Waiting for fresh"):
                sender.fetch_rate_limits()

    def test_silent_process_times_out_and_is_reaped(self):
        real_popen = subprocess.Popen
        processes = []

        def popen(command, **kwargs):
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
            processes.append(proc)
            return proc

        started = time.monotonic()
        with patch.object(quota, "find_codex_exe", return_value="unused"), patch.object(quota.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(TimeoutError):
                quota.read_account_quota(timeout=0.15)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(processes[0].poll())

    def test_protocol_error_is_returned_and_process_closed(self):
        real_popen = subprocess.Popen
        program = (
            "import json,sys,time; "
            "sys.stdin.readline(); "
            "print(json.dumps({'id':1,'result':{}}),flush=True); "
            "sys.stdin.readline(); sys.stdin.readline(); "
            "print(json.dumps({'id':2,'error':{'message':'offline'}}),flush=True); "
            "time.sleep(20)"
        )
        def popen(command, **kwargs):
            return real_popen([sys.executable, "-c", program], **kwargs)
        with patch.object(quota, "find_codex_exe", return_value="unused"), patch.object(quota.subprocess, "Popen", side_effect=popen):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                quota.read_account_quota(timeout=3)


if __name__ == "__main__":
    unittest.main()
