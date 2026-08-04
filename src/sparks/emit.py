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

import logging
import time
from types import TracebackType
from typing import Any, Self

from sparks.buffer import Buffer
from sparks.metrics import METRICS
from sparks.series import Series

LOG = logging.getLogger("sparks")

FLUSH_SECONDS = 5.0
"""k6's number. Small enough to feel live, large enough that a slow push does
not queue behind itself."""


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
        raise NotImplementedError("Task 7")

    def _shutdown(self) -> None:
        """Nothing to shut down until Task 7 owns the pump thread."""
