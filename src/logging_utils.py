"""Console logging helpers, mirroring CPACodexKeeper's buffered style.

Each credential is processed concurrently; per-credential log lines are buffered
and flushed atomically so concurrent output never interleaves.
"""
import sys
import threading
import time


_LEVEL_TAGS = {
    "INFO": "[INFO]",
    "OK": "[ OK ]",
    "WARN": "[WARN]",
    "ERROR": "[FAIL]",
    "DRY": "[DRY ]",
    "SET": "[ SET]",
    "SKIP": "[SKIP]",
    "DEAD": "[DEAD]",
}


class ConsoleLogger:
    def __init__(self):
        self._lock = threading.Lock()

    def _emit(self, text):
        with self._lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def log(self, level, message, indent=0):
        tag = _LEVEL_TAGS.get(level, f"[{level}]")
        pad = "  " * indent
        self._emit(f"{tag} {pad}{message}")

    def blank_line(self):
        self._emit("")

    def divider(self):
        self._emit("=" * 78)


class EntryLogger:
    """Buffers lines for one credential, flushed atomically at the end."""

    def __init__(self, console, label):
        self.console = console
        self.label = label
        self._lines = []

    def log(self, level, message, indent=0):
        tag = _LEVEL_TAGS.get(level, f"[{level}]")
        pad = "  " * indent
        self._lines.append(f"{tag} {pad}{message}")

    def header(self, idx, total):
        self._lines.append(f"\n--- [{idx}/{total}] {self.label} ---")

    def flush(self):
        if not self._lines:
            return
        with self.console._lock:
            sys.stdout.write("\n".join(self._lines) + "\n")
            sys.stdout.flush()
        self._lines = []


def now_stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
