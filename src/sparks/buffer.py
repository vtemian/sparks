"""Samples waiting for the next flush. Two values for one series in the same
millisecond are an HTTP 400 that rolls back every series in the request, so the
collision is resolved here rather than on the wire."""

import threading
from typing import Any

from sparks.series import Series


class Buffer:
    """Thread-safe, append from anywhere, drain from the pump thread only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[Series, dict[int, float]] = {}
        self._last: dict[Series, int] = {}

    def add(self, series: Series, value: float, ts_ms: int) -> None:
        """Record one sample. A timestamp at or before the last one sent for
        this series is dropped, because Prometheus would reject it."""
        with self._lock:
            if ts_ms <= self._last.get(series, -1):
                return
            slot = self._pending.setdefault(series, {})
            # First value wins: the earlier reading is the truthful one.
            slot.setdefault(ts_ms, value)

    def drain(self) -> list[dict[str, Any]]:
        """Everything buffered, in the wire shape, oldest first per series."""
        with self._lock:
            pending, self._pending = self._pending, {}
            out: list[dict[str, Any]] = []
            for series, samples in pending.items():
                stamps = sorted(samples)
                if not stamps:
                    continue
                self._last[series] = stamps[-1]
                out.append(
                    {
                        "metric": series.as_metric(),
                        "values": [samples[stamp] for stamp in stamps],
                        "timestamps": stamps,
                    }
                )
            return out

    def seen(self) -> dict[Series, int]:
        """Every series sent so far, mapped to its last timestamp: that is what
        lets a stale marker land strictly after the sample it ends."""
        with self._lock:
            return dict(self._last)
