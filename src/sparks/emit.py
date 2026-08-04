"""Per-run training metrics, pushed to Prometheus.

Deliberately not a `TrainerCallback`. bbm's training loop is hand written and
rejected HuggingFace `Trainer` for four measured reasons, so there is no
callback to attach to. This is a plain object a loop calls.

    m = RunMetrics(run_id="run-20260804-1530-e0", url="http://127.0.0.1:9090",
                   info={"model": "helium-2b", "git_sha": sha})
    m.begin()
    for step, batch in enumerate(batches):
        ...
        m.log(step=step, loss=float(loss))
    m.end("finished")

Or as a context manager, which records `crashed` on an exception:

    with RunMetrics(...) as m:
        ...

Every push is wrapped in try/except. A metrics outage must never kill a run.
"""

import atexit
import logging
import struct
import threading
import time
from types import TracebackType
from typing import Any, Self

# 1.1.3 ships no py.typed, so mypy cannot see into it and strict mode refuses
# the import outright. The writer is used through one method, send().
from prometheus_remote_writer import RemoteWriter  # type: ignore[import-untyped]

from sparks.buffer import Buffer
from sparks.metrics import LIFECYCLE, METRICS
from sparks.series import Series

LOG = logging.getLogger("sparks")

FLUSH_SECONDS = 5.0
"""k6's number. Small enough to feel live, large enough that a slow push does
not queue behind itself."""

STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]
"""Prometheus's stale marker. A pushed series is never marked stale
automatically, so without this a finished run holds its last value for five
minutes and then vanishes. Task 2's spike is what proved this survives the
Python protobuf encoder."""


class RunMetrics:
    def __init__(
        self,
        run_id: str,
        url: str,
        info: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        autostart: bool = True,
    ) -> None:
        """`info` is immutable metadata for the info metric and is never
        plotted directly. `labels` are dimensions carried on every sample, for
        panels that group by them: keep them few and low cardinality."""
        self.run_id = run_id
        self.url = url.rstrip("/")
        self._info = {"run_id": run_id, **(info or {})}
        self._labels = {"run_id": run_id, **(labels or {})}
        self._buffer = Buffer()
        self._writer: Any = None
        self._thread: Any = None
        self._stop: Any = None
        if autostart:
            self._start()

    # -- public API ----------------------------------------------------------

    def begin(self) -> None:
        """Identity, start time, and the first heartbeat."""
        now = time.time()
        self._sample(Series("training_run_info", self._info), 1.0, now)
        self._sample(
            Series("training_run_start_timestamp_seconds", self._labels), now, now
        )
        self._beat(now)

    def log(self, **values: float) -> None:
        """One sample per keyword, all sharing one timestamp.

        Keywords are metric names without the `training_` prefix, so
        `m.log(loss=0.5)` writes `training_loss`."""
        now = time.time()
        for key, value in values.items():
            name = f"training_{key}"
            if name not in METRICS:
                raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")
            self._sample(Series(name, self._labels), float(value), now)

    def log_group(self, name: str, by_group: dict[str, float]) -> None:
        """A metric that only means something per parameter group.

        bbm trains LoRA at 2e-4 and the warm-started draw tables at 2e-5, so a
        single `learning_rate` series would be wrong by 10x for one of them."""
        if name not in METRICS:
            raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")
        now = time.time()
        for group, value in by_group.items():
            self._sample(
                Series(name, {**self._labels, "group": group}), float(value), now
            )

    def end(self, status: str = "finished") -> None:
        """Terminal state. Never re-labels the info metric."""
        now = time.time()
        self._sample(
            Series("training_run_end_timestamp_seconds", self._labels), now, now
        )
        self._sample(
            Series("training_run_status", {**self._labels, "status": status}), 1.0, now
        )
        self._shutdown()

    def __enter__(self) -> Self:
        self.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.end("crashed" if exc_type else "finished")

    # -- internals -----------------------------------------------------------

    def _sample(self, series: Series, value: float, when: float) -> None:
        self._buffer.add(series, value, int(when * 1000))

    def _beat(self, now: float) -> None:
        """The heartbeat freezes when the run dies, which is what lets one
        expression cover live and finished runs. The info metric rides along
        because a series that stops being pushed vanishes from instant queries
        after the 5 minute lookback window, taking every join with it."""
        self._sample(
            Series("training_run_heartbeat_timestamp_seconds", self._labels), now, now
        )
        self._sample(Series("training_run_info", self._info), 1.0, now)

    def _start(self) -> None:
        self._writer = RemoteWriter(
            url=f"{self.url}/api/v1/write",
            timeout=5.0,
            retries=3,
            backoff_factor=0.5,
            sort_labels=True,
            strict_timestamps=True,
            auto_convert_seconds_to_ms=False,
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._pump, name="sparks-pump", daemon=True
        )
        self._thread.start()
        # Backstop: a run killed without reaching end() still flushes what it had.
        atexit.register(self._shutdown)

    def _pump(self) -> None:
        """The only thread that ever calls send()."""
        while not self._stop.wait(FLUSH_SECONDS):
            self._beat(time.time())
            self._flush()

    def _flush(self) -> None:
        batch = self._buffer.drain()
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as e:  # deliberately broad: telemetry never kills a run
            LOG.warning("sparks: dropped %d series: %s", len(batch), e)

    def _shutdown(self) -> None:
        if self._stop is None or self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=FLUSH_SECONDS * 2)
        self._flush()
        self._mark_stale()

    def _mark_stale(self) -> None:
        """End the live series, so a finished run stops dead on the graph
        instead of flat-lining for the lookback window.

        The lifecycle metrics are deliberately spared. `end()` writes the status
        and end-time samples immediately before this runs, so staling everything
        the buffer has seen would erase them a millisecond after writing them and
        `training_run_status` would never resolve at all.
        """
        ended = int(time.time() * 1000)
        batch = [
            {"metric": s.as_metric(), "values": [STALE_NAN], "timestamps": [ended]}
            for s in self._buffer.seen()
            if s.name not in LIFECYCLE
        ]
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as e:  # deliberately broad: telemetry never kills a run
            LOG.warning("sparks: could not mark %d series stale: %s", len(batch), e)
