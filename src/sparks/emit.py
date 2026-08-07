import atexit
import logging
import os
import struct
import threading
import time
from types import TracebackType
from typing import Any, Self

from prometheus_remote_writer import RemoteWriter  # type: ignore[import-untyped]

from sparks.buffer import Buffer
from sparks.metrics import LIFECYCLE, METRICS
from sparks.series import Series

LOG = logging.getLogger("sparks")

FLUSH_SECONDS = 5.0

STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]


class RunMetrics:
    def __init__(
        self,
        run_id: str,
        url: str,
        info: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        autostart: bool = True,
        lifecycle: bool = True,
    ) -> None:
        self._lifecycle = lifecycle
        self.run_id = run_id
        self.url = url.rstrip("/")
        self._info = {"run_id": run_id, **(info or {})}
        self._labels = {"run_id": run_id, **(labels or {})}
        # Built and discarded: a bad label must fail here, on the caller's
        # thread, rather than inside the pump where nothing would report it.
        Series("training_run_info", self._info)
        Series("training_run_heartbeat_timestamp_seconds", self._labels)
        self._buffer = Buffer()
        self._writer: Any = None
        self._thread: Any = None
        self._stop: Any = None
        if autostart:
            self._start()

    def begin(self) -> None:
        if not self._lifecycle:
            return
        now = time.time()
        self._sample(Series("training_run_info", self._info), 1.0, now)
        self._sample(
            Series("training_run_start_timestamp_seconds", self._labels), now, now
        )
        self._beat(now)

    def log(self, **values: float) -> None:
        now = time.time()
        for key, value in values.items():
            name = f"training_{key}"
            if name not in METRICS:
                raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")
            self._refuse_if_not_ours(name)
            self._sample(Series(name, self._labels), float(value), now)

    def log_group(self, name: str, by_group: dict[str, float]) -> None:
        if name not in METRICS:
            raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")
        self._refuse_if_not_ours(name)
        now = time.time()
        for group, value in by_group.items():
            self._sample(
                Series(name, {**self._labels, "group": group}), float(value), now
            )

    def end(self, status: str = "finished") -> None:
        if not self._lifecycle:
            self._shutdown()
            return
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

    def _sample(self, series: Series, value: float, when: float) -> None:
        self._buffer.add(series, value, int(when * 1000))

    def _refuse_if_not_ours(self, name: str) -> None:
        # Asymmetric on purpose: a standalone RunMetrics has no child and owns
        # both halves, so only the child side is constrained.
        if self._lifecycle:
            return
        if name in LIFECYCLE or name == "training_run_active":
            raise KeyError(
                f"{name} belongs to the supervisor; a child emitter writing it "
                "would collide on the same series"
            )

    def _beat(self, now: float) -> None:
        if not self._lifecycle:
            return
        self._sample(
            Series("training_run_heartbeat_timestamp_seconds", self._labels), now, now
        )
        self._sample(Series("training_run_info", self._info), 1.0, now)
        # Stale-marked at end(), unlike the info metric, so an annotation on it
        # draws the run's real span.
        self._sample(Series("training_run_active", self._labels), 1.0, now)

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
        while not self._stop.wait(FLUSH_SECONDS):
            try:
                self._beat(time.time())
                self._flush()
            except Exception as exc:  # deliberately broad: the pump must not die
                LOG.warning("sparks: pump cycle failed: %s", exc, exc_info=True)

    def _flush(self) -> None:
        batch = self._buffer.drain()
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as exc:  # noqa: BLE001 -- telemetry never kills a run
            LOG.warning("sparks: dropped %d series: %s", len(batch), exc)

    def _shutdown(self) -> None:
        if self._stop is None or self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=FLUSH_SECONDS * 2)
        if self._thread.is_alive():
            # The pump is wedged inside send(). Flushing anyway would put a
            # second writer on the wire, where the two batches destroy each
            # other; losing the terminal samples is the lesser harm.
            LOG.warning(
                "sparks: pump still sending after %.0fs; skipping the final "
                "flush rather than writing concurrently",
                FLUSH_SECONDS * 2,
            )
            return
        self._flush()
        self._mark_stale()

    def _mark_stale(self) -> None:
        batch = self._stale_batch()
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as exc:  # noqa: BLE001 -- telemetry never kills a run
            LOG.warning("sparks: could not mark %d series stale: %s", len(batch), exc)

    def _stale_batch(self) -> list[dict[str, Any]]:
        ended = int(time.time() * 1000)
        return [
            {
                "metric": series.as_metric(),
                "values": [STALE_NAN],
                # Strictly after the last real sample: sharing its millisecond
                # loses every marker in the batch.
                "timestamps": [max(ended, last + 1)],
            }
            for series, last in self._buffer.seen().items()
            if series.name not in LIFECYCLE
        ]


def from_env(autostart: bool = True, **labels: str) -> RunMetrics | None:
    run_id = os.environ.get("SPARKS_RUN_ID")
    url = os.environ.get("SPARKS_PROMETHEUS_URL")
    if not run_id or not url:
        return None
    return RunMetrics(run_id, url, labels=labels, autostart=autostart, lifecycle=False)
