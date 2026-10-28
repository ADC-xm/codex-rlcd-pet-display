import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SENDER = ROOT / "codex_ble_sender.py"
WATCH_LOG = ROOT / "codex_ble_autostart_watch.log"
HEARTBEAT = ROOT / "codex_ble_sender.heartbeat"
PYTHONW = Path(r"C:\Python313\pythonw.exe")
PYTHON = Path(r"C:\Python313\python.exe")
POLL_SECONDS = 5
HEARTBEAT_MAX_AGE_SECONDS = 180


def log(message):
    text = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        with WATCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def start_sender():
    exe = PYTHONW if PYTHONW.exists() else PYTHON
    log(f"Starting sender with {exe}")
    return subprocess.Popen(
        [str(exe), str(SENDER)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main():
    log("Watcher started.")
    sender_proc = None
    sender_started_at = 0
    while True:
        if sender_proc is None or sender_proc.poll() is not None:
            if sender_proc is not None:
                log(f"Sender exited with code {sender_proc.returncode}; restarting.")
            sender_proc = start_sender()
            sender_started_at = time.time()
        elif time.time() - sender_started_at > HEARTBEAT_MAX_AGE_SECONDS:
            try:
                heartbeat_age = time.time() - HEARTBEAT.stat().st_mtime
            except OSError:
                heartbeat_age = float("inf")
            if heartbeat_age > HEARTBEAT_MAX_AGE_SECONDS:
                log(
                    f"Sender heartbeat stale for {heartbeat_age:.0f}s; restarting."
                )
                sender_proc.terminate()
                try:
                    sender_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    sender_proc.kill()
                    sender_proc.wait(timeout=5)
                sender_proc = None
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
