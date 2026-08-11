import atexit
import logging
import os
import struct
import threading
import time
from collections import deque
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from prometheus_remote_writer import RemoteWriter  # type: ignore[import-untyped]

from sparks.buffer import Buffer
from sparks.metrics import LIFECYCLE, METRICS
from sparks.series import Series

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Run", "RunMetrics", "track"]

LOG = logging.getLogger("sparks")

FLUSH_SECONDS = 5.0

RATE_WINDOW_STEPS = 20  # what "over the last window" means for the rates

MARKS_FOR_A_RATE = 2  # a rate is an interval, and an interval needs two ends

STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]


def training_metric(key: str) -> str:
    name = f"training_{key}"
    if name not in METRICS:
        raise KeyError(f"{name} is not declared in sparks.metrics.METRICS")

    return name


def group_name(group: object) -> str:
    # Not str() of whatever arrived: 0 and "0" are two series in the buffer and
    # one on the wire, sharing a timestamp. That is a duplicate sample, and
    # remote-write answers a 400 by rolling back the whole request.
    if not isinstance(group, str):
        raise TypeError(f"group {group!r} is not a name")

    return group


def check_run_shape(
    total: int | None, tokens_per_step: int | None, window: int
) -> None:
    if total is not None and total < 1:
        raise ValueError(f"total counts steps and cannot be {total}")

    if tokens_per_step is not None and tokens_per_step < 1:
        raise ValueError(
            f"tokens_per_step counts tokens and cannot be {tokens_per_step}"
        )

    # maxlen=1 makes the newest mark the oldest one too, so every span is zero
    # and no rate is ever reported.
    if window < MARKS_FOR_A_RATE:
        raise ValueError(f"window needs {MARKS_FOR_A_RATE} steps, not {window}")


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
            name = training_metric(key)
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


class Run:
    def __init__(
        self,
        metrics: RunMetrics | None,
        total: int | None = None,
        tokens_per_step: int | None = None,
        window: int = RATE_WINDOW_STEPS,
        clock: "Callable[[], float]" = time.monotonic,
    ) -> None:
        check_run_shape(total, tokens_per_step, window)
        self._metrics = metrics
        self.total = total
        self.tokens_per_step = tokens_per_step
        self.clock = clock
        self.count = 0
        self._ended = False
        self._warned = False
        self._shapes: dict[str, bool] = {}
        self._marks: deque[float] = deque(maxlen=window)

    def __enter__(self) -> Self:
        if self._metrics is not None:
            self._metrics.begin()

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close("crashed" if exc_type else "finished")

    def end(self) -> None:
        self._close("finished")

    def _close(self, status: str) -> None:
        if self._ended:
            return

        if self._metrics is not None:
            self._metrics.end(status)

        # Last, so a failed end leaves the run reporting rather than silent.
        self._ended = True

    def _keep_shape(self, key: str, grouped: bool) -> None:
        # A metric reported both ways is two series with one name. The receiver
        # takes both without complaint, and the panel draws two lines carrying
        # the same legend, which is a worse answer than an error.
        if self._shapes.setdefault(key, grouped) is grouped:
            return

        shape = "a mapping" if grouped else "a number"
        raise ValueError(f"{key} was already reported the other way; {shape} now")

    def _warn_once(self) -> None:
        if self._warned:
            return

        self._warned = True
        LOG.warning("sparks: this run already ended; later samples are dropped")

    def step(self, **values: float | dict[str, float]) -> None:
        if self._ended:
            self._warn_once()
            return

        # Counted even with nowhere to send it, so a laptop run still walks the
        # same path the box does.
        self.count += 1
        self._marks.append(self.clock())
        self.log(**{**self.derived(), **values})

    def log(self, **values: float | dict[str, float]) -> None:
        if self._ended:
            self._warn_once()
            return

        # Everything is checked before anything is sent, so a bad third keyword
        # does not leave the first two already recorded. Checked here rather
        # than where they are sent, too: checking only when an emitter exists
        # is how a bad one survives every laptop run and then raises on the
        # box, inside the training loop, after submit reported success.
        for key, value in values.items():
            training_metric(key)
            self._keep_shape(key, isinstance(value, dict))

        plain: dict[str, float] = {}
        for key, value in values.items():
            name = training_metric(key)
            if not isinstance(value, dict):
                plain[key] = float(value)
                continue

            # A mapping is one series per group. Nothing else a metric can be
            # is a mapping, so this needs no keyword of its own.
            grouped = {group_name(group): float(each) for group, each in value.items()}
            if self._metrics is not None:
                self._metrics.log_group(name, grouped)

        if plain and self._metrics is not None:
            self._metrics.log(**plain)

    def derived(self) -> dict[str, float]:
        values = {"step": float(self.count)}
        rate = self.rate()
        if rate is not None:
            values["steps_per_sec"] = rate
            if self.tokens_per_step is not None:
                values["tokens_per_sec"] = rate * self.tokens_per_step

        if self.total is None:
            return values

        # Clamped: a run that overshoots its own estimate would otherwise
        # report 167% complete and a negative eta on panels bounded at 0-1.
        remaining = max(self.total - self.count, 0)
        values["progress"] = min(self.count / self.total, 1.0)
        if rate is not None:
            values["eta_seconds"] = remaining / rate

        return values

    def rate(self) -> float | None:
        if not self._marks:
            return None

        # One step spans zero, which is right: a rate needs two of them. So
        # does a clock that has not moved, and both land here.
        span = self._marks[-1] - self._marks[0]
        if span <= 0:
            return None

        return (len(self._marks) - 1) / span


def track(
    total: int | None = None,
    tokens_per_step: int | None = None,
    window: int = RATE_WINDOW_STEPS,
    **labels: str,
) -> Run:
    for reserved in ("run_id", "group"):
        if reserved in labels:
            raise ValueError(f"{reserved} is the emitter's own and cannot be a label")

    # Before the emitter, so a rejected argument leaves no pump thread behind.
    check_run_shape(total, tokens_per_step, window)
    run_id = os.environ.get("SPARKS_RUN_ID")
    url = (os.environ.get("SPARKS_PROMETHEUS_URL") or "").strip()
    metrics = None
    if run_id and url:
        # lifecycle=False: the supervisor owns the run record, and two writers
        # on one series is a 400 that rolls back the whole batch.
        metrics = RunMetrics(run_id, url, labels=labels, lifecycle=False)
    elif run_id:
        LOG.warning(
            "sparks: SPARKS_RUN_ID is set but SPARKS_PROMETHEUS_URL is not, "
            "so this run reports nothing"
        )
    else:
        LOG.debug("not inside a job: this run reports nothing")

    return Run(metrics, total=total, tokens_per_step=tokens_per_step, window=window)
