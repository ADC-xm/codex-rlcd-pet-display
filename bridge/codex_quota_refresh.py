"""Bounded, non-blocking account quota refresh for the BLE dashboard."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def find_codex_exe():
    override = os.environ.get("CODEX_CLI_PATH")
    if override and Path(override).is_file():
        return override
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = list(root.glob("*/codex.exe")) + [root / "codex.exe"]
    candidates = [path for path in candidates if path.is_file()]
    if candidates:
        return str(max(candidates, key=lambda path: path.stat().st_mtime))
    executable = shutil.which("codex")
    if executable:
        return executable
    raise RuntimeError("Codex executable not found")


def read_account_quota(timeout=15):
    proc = subprocess.Popen(
        [find_codex_exe(), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    messages = queue.Queue()

    def read_lines():
        try:
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex quota query timed out")
            try:
                message = messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex quota query timed out") from None
            if message is None:
                raise RuntimeError("Codex quota query closed before responding")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message", "request failed") if isinstance(error, dict) else str(error)
                raise RuntimeError(detail)
            return message.get("result")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-ble-sender", "version": "1.1.0"},
            "capabilities": None,
        }})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        result = receive(2)
        if not isinstance(result, dict) or not (result.get("rateLimits") or result.get("rateLimitsByLimitId")):
            raise RuntimeError("Codex returned no account quota")
        return result
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=2)
        proc.stdin.close()
        proc.stdout.close()


class QuotaRefresh:
    def __init__(self, fetch=read_account_quota, clock=time.monotonic,
                 interval=60, max_age=120, executor=None):
        self.fetch = fetch
        self.clock = clock
        self.interval = interval
        self.max_age = max_age
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="quota-refresh")
        self.future = None
        self.next_attempt = 0
        self.response = None
        self.received_at = None
        self.error = None
        self.failures = 0

    def poll(self, log):
        now = self.clock()
        if self.future is not None and self.future.done():
            try:
                self.response = self.future.result()
                self.received_at = now
                self.error = None
                self.failures = 0
                log("Account quota refreshed actively (not session-log cache)")
            except Exception as exc:
                self.failures += 1
                self.error = str(exc)
                log(f"Active account quota refresh failed: {exc}")
            self.future = None
            self.next_attempt = now + min(300, self.interval * 2 ** min(self.failures, 3))
        if self.future is None and now >= self.next_attempt:
            self.future = self.executor.submit(self.fetch)
        if self.response is not None and now - self.received_at <= self.max_age:
            return self.response
        return None
