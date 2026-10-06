"""Small metric helpers."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

import numpy as np


class Rolling:
    """Rolling window of float samples with percentiles (thread-safe)."""

    def __init__(self, maxlen: int = 500) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, value: float) -> None:
        with self._lock:
            self._values.append(float(value))

    def percentile(self, q: float) -> float | None:
        with self._lock:
            if not self._values:
                return None
            return float(np.percentile(np.fromiter(self._values, dtype=np.float64), q))

    def __len__(self) -> int:
        return len(self._values)


def iso(ts: float) -> str:
    """Epoch seconds → ISO-8601 UTC with milliseconds and a Z suffix."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def r(value: float | None, nd: int = 2) -> float | None:
    return None if value is None else round(float(value), nd)
