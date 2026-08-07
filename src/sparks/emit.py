"""Per-run training metrics, pushed to Prometheus.

Deliberately not a `TrainerCallback`. A hand-written training loop has no
callback to attach one to, and plenty of loops are hand written for good
reasons: the one this was built against rejected HuggingFace `Trainer` over four
measured problems with it under a PEFT wrapper. So this is a plain object a loop
calls. Wrapping it in a `TrainerCallback` for a project that does use `Trainer`
is a twenty-line shim; the reverse is not.

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

`prometheus_remote_writer` 1.1.3 ships no py.typed, so mypy cannot see into it
and strict mode refuses the import outright (hence the ignore below). The
writer is used through one method, `send()`.
"""

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
        lifecycle: bool = True,
    ) -> None:
        """`info` is immutable metadata for the info metric and is never
        plotted directly. `labels` are dimensions carried on every sample, for
        panels that group by them: keep them few and low cardinality.

        `lifecycle=False` suppresses the run's own record of itself, for the
        training child. The supervisor and the child both hold a RunMetrics for
        one run_id and MUST write disjoint series: two writers on the same
        series choose timestamps independently, which is out of order, a 400,
        and remote-write 1.0 rolls back the whole request, destroying the batch
        that carried the loss. The supervisor owns metrics.LIFECYCLE because it
        is the only process guaranteed to outlive the run; an OOM-killed child
        never runs atexit and cannot write its own terminal status.
        """
        self._lifecycle = lifecycle
        self.run_id = run_id
        self.url = url.rstrip("/")
        self._info = {"run_id": run_id, **(info or {})}
        self._labels = {"run_id": run_id, **(labels or {})}
        # Fail here, on the caller's thread, rather than five seconds later on
        # the pump. _beat builds these same two Series every cycle, and an
        # InvalidLabelError raised there would kill telemetry for the whole run
        # with nothing but a bare threading traceback to show for it.
        Series("training_run_info", self._info)
        Series("training_run_heartbeat_timestamp_seconds", self._labels)
        self._buffer = Buffer()
        self._writer: Any = None
        self._thread: Any = None
        self._stop: Any = None
        if autostart:
            self._start()

    # -- public API ----------------------------------------------------------

    def begin(self) -> None:
        """Identity, start time, and the first heartbeat."""
        if not self._lifecycle:
            return
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
            self._refuse_if_not_ours(name)
            self._sample(Series(name, self._labels), float(value), now)

    def log_group(self, name: str, by_group: dict[str, float]) -> None:
        """A metric that only means something per parameter group.

        Two groups at different rates is normal whenever part of a model is
        warm-started and part is not: a LoRA adapter that has to travel wants a
        rate an order of magnitude above the pretrained tables beside it, and a
        single `learning_rate` series would then be wrong by 10x for one of
        them."""
        if name not in METRICS:
            raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")
        self._refuse_if_not_ours(name)
        now = time.time()
        for group, value in by_group.items():
            self._sample(
                Series(name, {**self._labels, "group": group}), float(value), now
            )

    def end(self, status: str = "finished") -> None:
        """Terminal state. Never re-labels the info metric."""
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

    # -- internals -----------------------------------------------------------

    def _sample(self, series: Series, value: float, when: float) -> None:
        self._buffer.add(series, value, int(when * 1000))

    def _refuse_if_not_ours(self, name: str) -> None:
        """Keep the supervisor and the child on disjoint series.

        Two writers on one series choose timestamps independently, which is out
        of order, a 400, and remote-write 1.0 rolls back the whole request. The
        split is enforced here rather than by convention because the failure is
        silent: the batch that carried the loss simply never lands.
        """
        # Asymmetric on purpose. A standalone `RunMetrics` has no child and
        # legitimately owns both halves, so the supervisor side is not
        # constrained. The child is: the supervisor is definitely writing the
        # lifecycle series, so a child writing them too is a guaranteed
        # collision rather than a possible one.
        if self._lifecycle:
            return
        if name in LIFECYCLE or name == "training_run_active":
            raise KeyError(
                f"{name} belongs to the supervisor; a child emitter writing it "
                "would collide on the same series"
            )

    def _beat(self, now: float) -> None:
        """The heartbeat freezes when the run dies, which is what lets one
        expression cover live and finished runs. The info metric rides along
        because a series that stops being pushed vanishes from instant queries
        after the 5 minute lookback window, taking every join with it."""
        if not self._lifecycle:
            return
        self._sample(
            Series("training_run_heartbeat_timestamp_seconds", self._labels), now, now
        )
        self._sample(Series("training_run_info", self._info), 1.0, now)
        # Stale-marked at end(), unlike the info metric, so a Grafana
        # annotation on it draws the run's real span rather than overshooting
        # by the whole lookback window.
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
        """The only thread that ever calls send().

        The body is guarded as a whole, not just the network call. An exception
        anywhere in here kills the thread for the rest of the run, nothing
        restarts it, and the only trace is a bare threading.excepthook that no
        training log would ever show.
        """
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
            # The pump is wedged inside send(). A stalled Prometheus that
            # accepts the connection and never answers costs 4 attempts at a 5s
            # read timeout, which outlives this join, and flushing here anyway
            # would put a second writer on the wire. Remote-write 1.0 rolls back
            # a whole request on one bad sample, so the two batches can destroy
            # each other. Losing the terminal samples is the lesser harm, and
            # the frozen heartbeat still says the run stopped.
            LOG.warning(
                "sparks: pump still sending after %.0fs; skipping the final "
                "flush rather than writing concurrently",
                FLUSH_SECONDS * 2,
            )
            return
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
        batch = self._stale_batch()
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as exc:  # noqa: BLE001 -- telemetry never kills a run
            LOG.warning("sparks: could not mark %d series stale: %s", len(batch), exc)

    def _stale_batch(self) -> list[dict[str, Any]]:
        """The stale markers this run would write, separated out so a test can
        assert on the batch itself rather than re-implementing the filter and
        then checking its own arithmetic."""
        ended = int(time.time() * 1000)
        return [
            {
                "metric": series.as_metric(),
                "values": [STALE_NAN],
                # Strictly after the last real sample. Sharing its millisecond
                # is a 400 that rolls back every marker in the batch, and the
                # measured margin here is single-digit milliseconds.
                "timestamps": [max(ended, last + 1)],
            }
            for series, last in self._buffer.seen().items()
            if series.name not in LIFECYCLE
        ]


def from_env(autostart: bool = True, **labels: str) -> RunMetrics | None:
    """The training loop's entry point, for a child launched by the supervisor.

    Returns None outside supervised runs, so the same script runs standalone
    without the caller branching on it.
    """
    run_id = os.environ.get("SPARKS_RUN_ID")
    url = os.environ.get("SPARKS_PROMETHEUS_URL")
    if not run_id or not url:
        return None
    return RunMetrics(run_id, url, labels=labels, autostart=autostart, lifecycle=False)
