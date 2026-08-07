import threading
from typing import Any

from sparks.series import Series


class Buffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[Series, dict[int, float]] = {}
        self._last: dict[Series, int] = {}

    def add(self, series: Series, value: float, ts_ms: int) -> None:
        with self._lock:
            if ts_ms <= self._last.get(series, -1):
                return
            slot = self._pending.setdefault(series, {})
            # First value wins: the earlier reading is the truthful one.
            slot.setdefault(ts_ms, value)

    def drain(self) -> list[dict[str, Any]]:
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
        with self._lock:
            return dict(self._last)
