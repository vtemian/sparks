import atexit
import logging
import os
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from prometheus_remote_writer import RemoteWriter  # type: ignore[import-untyped]

from sparks.buffer import Buffer
from sparks.metrics import LIFECYCLE, METRICS
from sparks.run import current_user, git_sha, new_run_id, slug
from sparks.series import Series

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Pump", "Run", "RunRecord", "track"]

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


RECORD_ONLY = LIFECYCLE | {"training_run_active"}


class Pump:
    def __init__(
        self,
        url: str,
        autostart: bool = True,
        on_tick: "Callable[[float], None] | None" = None,
        never_stale: frozenset[str] = frozenset(),
    ) -> None:
        self.url = url.rstrip("/")
        self._on_tick = on_tick
        self._never_stale = never_stale
        self._buffer = Buffer()
        self._writer: Any = None
        self._thread: Any = None
        self._stop: Any = None
        if autostart:
            self._start()

    def sample(self, series: Series, value: float, when: float) -> None:
        self._buffer.add(series, value, int(when * 1000))

    def close(self) -> None:
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
            target=self._run, name="sparks-pump", daemon=True
        )
        self._thread.start()
        # Backstop: a run killed without reaching close() still flushes.
        atexit.register(self.close)

    def _run(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            try:
                if self._on_tick is not None:
                    self._on_tick(time.time())

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
            if series.name not in self._never_stale
        ]


class RunRecord:
    def __init__(
        self,
        run_id: str,
        url: str,
        info: dict[str, str] | None = None,
        autostart: bool = True,
    ) -> None:
        self.run_id = run_id
        self._ended = False
        self._info = {"run_id": run_id, **(info or {})}
        self._labels = {"run_id": run_id}
        # Built and discarded: a bad label must fail here, on the caller's
        # thread, rather than inside the pump where nothing would report it.
        Series("training_run_info", self._info)
        # LIFECYCLE is never staled: end() writes the status, and a marker a
        # millisecond later would erase it before anything could read it.
        self._pump = Pump(
            url, autostart=autostart, on_tick=self.beat, never_stale=LIFECYCLE
        )

    def begin(self) -> None:
        now = time.time()
        self._pump.sample(Series("training_run_info", self._info), 1.0, now)
        self._pump.sample(
            Series("training_run_start_timestamp_seconds", self._labels), now, now
        )
        self.beat(now)

    def beat(self, now: float) -> None:
        self._pump.sample(
            Series("training_run_heartbeat_timestamp_seconds", self._labels), now, now
        )
        self._pump.sample(Series("training_run_info", self._info), 1.0, now)
        # Stale-marked at end(), unlike the info metric, so an annotation on it
        # draws the run's real span.
        self._pump.sample(Series("training_run_active", self._labels), 1.0, now)

    def end(self, status: str = "finished") -> None:
        # The pump's close is idempotent, but these two samples are written
        # before it, so a second end would record a second, contradictory one.
        if self._ended:
            return

        self._ended = True
        now = time.time()
        self._pump.sample(
            Series("training_run_end_timestamp_seconds", self._labels), now, now
        )
        self._pump.sample(
            Series("training_run_status", {**self._labels, "status": status}), 1.0, now
        )
        self._pump.close()


class Run:
    def __init__(
        self,
        pump: Pump | None,
        labels: dict[str, str] | None = None,
        total: int | None = None,
        tokens_per_step: int | None = None,
        window: int = RATE_WINDOW_STEPS,
        clock: "Callable[[], float]" = time.monotonic,
        record: "RunRecord | None" = None,
    ) -> None:
        check_run_shape(total, tokens_per_step, window)
        self._pump = pump
        self._record = record
        self._labels = labels or {}
        Series("training_loss", self._labels)
        self.total = total
        self.tokens_per_step = tokens_per_step
        self.clock = clock
        self.count = 0
        self._ended = False
        self._warned = False
        self._shapes: dict[str, bool] = {}
        self._marks: deque[float] = deque(maxlen=window)

    def __enter__(self) -> Self:
        return self

    @property
    def run_id(self) -> str:
        return self._labels.get("run_id", "")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # The status is derived, never a parameter: a child has no record to
        # put one on, and an argument that is silently ignored for children is
        # the defect this class was split to remove.
        self._finish("crashed" if exc_type is not None else "finished")

    def end(self) -> None:
        self._finish("finished")

    def _finish(self, status: str) -> None:
        if self._ended:
            return

        if self._pump is not None:
            self._pump.close()
        if self._record is not None:
            self._record.end(status)

        # Last, so a failed end leaves the run reporting rather than silent.
        self._ended = True

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
        # does not leave the first two already recorded. Checked whether or not
        # there is a pump, too: checking only when there is one is how a bad
        # name survives every laptop run and then raises on the box, inside the
        # training loop, after submit reported success.
        for key, value in values.items():
            self._refuse_the_records(training_metric(key))
            self._keep_shape(key, isinstance(value, dict))

        now = time.time()
        for key, value in values.items():
            self._write(training_metric(key), value, now)

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

    def _write(self, name: str, value: float | dict[str, float], now: float) -> None:
        if not isinstance(value, dict):
            self._sample(Series(name, self._labels), float(value), now)
            return

        # A mapping is one series per group. Nothing else a metric can be is a
        # mapping, so this needs no keyword of its own.
        for group, each in value.items():
            labels = {**self._labels, "group": group_name(group)}
            self._sample(Series(name, labels), float(each), now)

    def _sample(self, series: Series, value: float, when: float) -> None:
        if self._pump is not None:
            self._pump.sample(series, value, when)

    def _refuse_the_records(self, name: str) -> None:
        if name not in RECORD_ONLY:
            return

        raise KeyError(
            f"{name} belongs to the supervisor; a second writer on one series "
            "is a 400 that rolls back the whole batch"
        )

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


def track(
    total: int | None = None,
    tokens_per_step: int | None = None,
    window: int = RATE_WINDOW_STEPS,
    name: str | None = None,
    info: dict[str, str] | None = None,
    **labels: str,
) -> Run:
    for reserved in ("run_id", "group"):
        if reserved in labels:
            raise ValueError(f"{reserved} is the emitter's own and cannot be a label")

    # Before the pump, so a rejected argument leaves no thread behind.
    check_run_shape(total, tokens_per_step, window)
    run_id = os.environ.get("SPARKS_RUN_ID")
    url = (os.environ.get("SPARKS_PROMETHEUS_URL") or "").strip()
    pump = None
    record = None
    if run_id and url:
        # Submitted: the supervisor began the record and will end it from how
        # the process exited. `name` and `info` are its business, not ours.
        pump = Pump(url)
    elif run_id:
        LOG.warning(
            "sparks: SPARKS_RUN_ID is set but SPARKS_PROMETHEUS_URL is not, "
            "so this run reports nothing"
        )
    elif url:
        # On the box, launched by hand. Nobody else owns this run's identity,
        # and without the record the dashboard's run picker never lists it: the
        # loss arrives under a run_id no query selects.
        run_id = new_run_id(run_name(name), current_user())
        record = RunRecord(
            run_id=run_id,
            url=url,
            info={"run_name": run_name(name), "git_sha": git_sha(), **(info or {})},
        )
        record.begin()
        # So a second track in this process becomes a child rather than minting
        # a second run, and so a subprocess inherits the right id.
        os.environ["SPARKS_RUN_ID"] = run_id
        pump = Pump(url)
    else:
        LOG.debug("not inside a job: this run reports nothing")

    return Run(
        pump,
        labels={"run_id": run_id or "", **labels},
        total=total,
        tokens_per_step=tokens_per_step,
        window=window,
        record=record,
    )


def run_name(name: str | None) -> str:
    if name:
        return name

    return slug(Path(sys.argv[0]).stem, "run")
