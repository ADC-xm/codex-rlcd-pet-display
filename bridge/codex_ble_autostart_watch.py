import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SENDER = ROOT / "codex_ble_sender.py"
WATCH_LOG = ROOT / "codex_ble_autostart_watch.log"
PYTHONW = Path(r"C:\Python313\pythonw.exe")
PYTHON = Path(r"C:\Python313\python.exe")
POLL_SECONDS = 5


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
    while True:
        if sender_proc is None or sender_proc.poll() is not None:
            if sender_proc is not None:
                log(f"Sender exited with code {sender_proc.returncode}; restarting.")
            sender_proc = start_sender()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
