# sparks slice 1: emitter, demo run, and the Training Runs dashboard

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A Python library that pushes per-step training metrics into the DGX Spark's existing
Prometheus, a `sparks run demo` synthetic run that exercises the whole path, and a provisioned
Grafana dashboard where that run is watchable live.

**Architecture:** One background thread owns every network write (Prometheus remote-write 1.0 has
no partial write, so a second writer can roll back the first one's batch). The training loop calls
a plain object, never a `TrainerCallback`. The dashboard ships as a JSON file dropped into the
directory sparkup already provisions, which survives a sparkup re-converge without sparkup knowing
we exist.

**Tech Stack:** Python 3.12, uv, `prometheus-remote-writer` 1.1.3, ruff, mypy strict, pytest,
Docker (for the live harness only), Prometheus 3.13.2, Grafana 13.1.1.

---

## Before you start: what the box already gives you

Verified live on 2026-08-04, not read off a document:

- Prometheus at `http://127.0.0.1:9090`, remote-write receiver **enabled**, 15s scrape, 30d retention.
- Grafana 13.1.1 at `http://spark.local` (port 80), anonymous **Viewer**, datasource uid literally
  `prometheus`, one dashboard uid `spark-overview`.
- Scrape jobs `node`, `gpu`, `prometheus` all up.
- `/srv/bbm/{data,checkpoints,runs}`, mode 2775, group `bbm`. Both `vlad` and `marius` are in it.
- `/var/lib/node_exporter/textfile`, mode 2775, group `bbm`, empty.
- Power **is** live: 14 `node_hwmon_power_watt` channels, `sys_total` ~36 W idle, and 4 energy
  counters (`pkg`, `cpu_e`, `cpu_p`, `gpu`).
- NVML energy counter works: 33254 mJ over 3 s = 11.08 W against `PowerUsage` 11.03 W.
- Training venv is `~/bbm-train/.venv` on the box: torch 2.13.0+cu130, transformers 5.14.1,
  peft 0.20.0, nvidia-ml-py. **Not** `~/bbm/.venv`, which has neither transformers nor peft.

Two facts that are not in any document and will cost you a day if you miss them:

- `~/bbm-train/.venv` has `bbm` installed **editable from the checkout**, because
  `src/bbm/cli.py:379` resolves `scripts/tokenizer_report.py` by walking up from `__file__` and hard
  fails if it is absent. A wheel does not contain `scripts/`. Do not "fix" this by installing a wheel.
- `bbm`'s training loop has **never been run**. No `out/` directory, no `*.jsonl`, no `verdict.json`
  anywhere in the repo or in git history. Every timing number below is an estimate from
  `bbm/docs/plans/2026-08-02-anchors-gates-and-corpus.md:874`, not a measurement.

## Three corrections to `sparkup/docs/training-observability.md`

That document is the specification this plan implements. It is right about almost everything. These
three points are wrong, and each one was verified against source or against the running box.

**1. C1's `PrometheusCallback(TrainerCallback)` has nothing to attach to.**
`bbm/bbm_train/train.py:1-16` opens by rejecting HuggingFace `Trainer` with four measured reasons,
and `train_arm()` is a hand-written loop. Build a plain `RunMetrics` object with
`begin()` / `log()` / `end()`. A `TrainerCallback` adapter is a 20-line shim someone can add later
if a project ever wants one. Do not build it now.

The doc also says the structure is "copied from Axolotl". Axolotl's `OpenTelemetryMetricsCallback`
is a **scrape exporter, not a push client**: it starts a `prometheus_client` HTTP server, has no
batching, no flush interval, and **attaches no labels at all**, so two runs on one host produce
indistinguishable series. Copy k6's `internal/output/prometheusrw/` instead: 5s push interval,
buffer plus periodic flusher, millisecond truncation with a `seen` set, and stale markers on
shutdown. Copy exactly one thing from Axolotl: wrapping every push in a broad `try/except` that
only logs a warning, because telemetry must never kill a training run.

**2. `node_hwmon_energy_input_joule_total` does not exist. The metric is `node_hwmon_energy_joule_total`.**
Confirmed live: 4 series (`pkg` 180833 J, `cpu_e`, `cpu_p`, `gpu` 68693 J). The repo's own code has
it right (`sparkup/tests/fake_exporters.py:198` and the dashboard); only the prose is wrong, in
`docs/training-observability.md:202` and `INSTALL_CLAUDE.md:117`. This is the same class of bug as
sparkup commit `9d731f8` ("Fix the power metric name"), made again for energy and not yet caught
because nothing queries it yet. Not slice 1's problem, but do not copy the PromQL from the doc in
slice 2 without fixing the name.

**3. `status` must not be a label on `training_run_info`.**
The doc specifies `training_run_info{run_id, run_name, git_sha, model, dataset, tokenizer, status}`
and then flips `status` at `on_train_end`. Changing any label on an info metric creates a **new
series**, and remote-written series are never automatically marked stale, so both the old and the new
one stay resolvable. Every panel using the documented join
`... * on(run_id) group_left(...) training_run_info` then fails with
`found duplicate series for the match group` and **goes fully red**, for 5 minutes at minimum and
for 30 days if the query wraps the right side in `last_over_time`. Emit instead:

- `training_run_info{...}` with **immutable labels only**, re-pushed unchanged every cycle.
- `training_run_end_timestamp_seconds{run_id}` written once at the end.
- `training_run_status{run_id, status="finished|crashed"}` written once at the end.

## The staleness rule, which shapes everything

Prometheus does **not** mark pushed series stale. Staleness markers are injected by the scrape loop
and the rule evaluator; the remote-write receiver has no such loop. So a series that stops being
pushed:

- returns its **last value, frozen**, for 5 minutes (`--query.lookback-delta`, default `5m`);
- then **vanishes entirely** from instant queries. Not zero. Absent.

Three consequences, all of them load-bearing:

- **Re-push `training_run_info` on every flush cycle.** Pushing it once at start means the join's
  right side is empty five minutes later and every decorated panel returns nothing for the rest of
  the run.
- **Any query that must see finished runs needs `last_over_time(...[30d])`.** `last_over_time` is one
  of the few functions that preserves `__name__`.
- **Push explicit stale markers on clean shutdown** so a finished run ends crisply instead of
  dragging a flat line for five minutes. Task 2 verifies the marker actually survives Python's
  protobuf encoder before anything depends on it.

## Two more constraints worth knowing before you write code

**Remote-write 1.0 has no partial write.** `storage/remote/write_handler.go` returns on the first bad
sample and its `defer` calls `app.Rollback()`. One out-of-order sample discards **every series in
that request**. Therefore: exactly one thread ever calls `send()`. This is why the design has a
single pump thread rather than a heartbeat thread plus a flush thread.

**Invalid labels are dropped silently and the push still returns HTTP 200.** A typo'd label name gives
you a green push and no data, counted only in
`prometheus_api_remote_write_invalid_labels_samples_total`. Validate label names client-side (Task 3)
rather than trusting the round trip.

Acceptable timestamp band is `[head_max - 1h, now + 10m]`; outside it you get HTTP 400 and the whole
batch is rolled back. Timestamps are int64 **milliseconds**.

---

## Task 1: Scaffold the repo

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.python-version`, `Makefile`, `src/sparks/__init__.py`,
  `src/sparks/py.typed`, `tests/__init__.py`

**Step 1: Write the manifest**

`pyproject.toml`:

```toml
[project]
name = "sparks"
version = "0.1.0"
description = "Training runs on the DGX Spark: metrics, launcher, queue"
requires-python = ">=3.12"
dependencies = [
    "prometheus-remote-writer>=1.1.3",
]

[project.scripts]
sparks = "sparks.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "mypy>=1.13",
    "ruff>=0.8",
    "requests>=2.32",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sparks"]

[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "C4", "SIM", "RUF"]

[tool.mypy]
strict = true
files = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = ["-ra", "--strict-markers"]
markers = ["live: needs a real Prometheus (make live)"]
```

`.python-version`:

```
3.12
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
tests/harness/rendered/
```

`src/sparks/__init__.py`: empty. `src/sparks/py.typed`: empty. `tests/__init__.py`: empty.

**Step 2: Write the Makefile**

Mirrors `bbm/Makefile` so the two repos are driven the same way.

```makefile
.PHONY: sync fmt lint typecheck test check live harness-up harness-down

sync:
	uv sync

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests

typecheck:
	uv run mypy

test:
	uv run pytest -m "not live"

check: lint typecheck test

# Everything above runs without Docker. This does not: it brings up a real
# Prometheus, pushes real samples, and reads them back. No mocks anywhere.
live: harness-up
	uv run pytest -m live
	$(MAKE) harness-down

harness-up:
	./tests/harness-up.sh

harness-down:
	./tests/harness-down.sh
```

**Step 3: Verify it resolves**

Run: `cd /Users/whitemonk/projects/ai/sparks && uv sync`
Expected: a `.venv/` appears and `prometheus-remote-writer` is installed.

Run: `uv run python -c "from prometheus_remote_writer import RemoteWriter; print('ok')"`
Expected: `ok`

**Step 4: Commit**

```bash
git add pyproject.toml .gitignore .python-version Makefile src tests
git commit -m "chore: scaffold the sparks package"
```

---

## Task 2: Spike the two unverified assumptions against a real Prometheus

Do this before writing the emitter. Both answers change the design, and both are cheap to get wrong
and expensive to discover later.

**Files:**
- Create: `tests/harness/compose.yml`, `tests/harness-up.sh`, `tests/harness-down.sh`
- Create: `scripts/spike_remote_write.py` (delete it at the end of this task)

**Step 1: Write the harness compose file**

`tests/harness/compose.yml`. Ports are deliberately offset from the box's so this can run on the
Spark itself without colliding with the real stack.

```yaml
# A real Prometheus, for tests that must not mock the thing under test.
# Project name keeps it from colliding with sparkup's own stack or its harness.
name: sparks-harness

services:
  prometheus:
    image: prom/prometheus:v3.13.2
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --web.enable-lifecycle
      # The whole point of this harness.
      - --web.enable-remote-write-receiver
    ports:
      - "127.0.0.1:19091:9090"
```

`tests/harness/prometheus.yml`:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
```

**Step 2: Write the harness scripts**

`tests/harness-up.sh`:

```bash
#!/usr/bin/env bash
# A real Prometheus on 127.0.0.1:19091, for the live tests.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/harness"

docker compose up -d --wait

# --wait returns when the container is running, not when Prometheus answers.
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:19091/-/ready >/dev/null 2>&1; then
    echo "harness: prometheus ready on 127.0.0.1:19091"
    exit 0
  fi
  sleep 1
done

echo "harness: prometheus did not become ready" >&2
docker compose logs prometheus >&2
exit 1
```

`tests/harness-down.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/harness"
docker compose down --volumes
```

Run: `chmod +x tests/harness-up.sh tests/harness-down.sh`

**Step 3: Write the spike**

`scripts/spike_remote_write.py`. This is throwaway code that answers four questions.

```python
"""Four questions the design depends on, asked of a real Prometheus.

    ./tests/harness-up.sh && uv run python scripts/spike_remote_write.py

Delete this file once the answers are recorded in INSTALL_CLAUDE.md.
"""

import struct
import time

import requests
from prometheus_remote_writer import RemoteWriter

URL = "http://127.0.0.1:19091"
STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]


def query(expr: str) -> list[dict]:
    r = requests.get(f"{URL}/api/v1/query", params={"query": expr}, timeout=5)
    r.raise_for_status()
    return r.json()["data"]["result"]


def main() -> None:
    # Q1: which constructor kwargs does 1.1.3 actually accept?
    w = RemoteWriter(
        url=f"{URL}/api/v1/write",
        timeout=5.0,
        retries=3,
        backoff_factor=0.5,
        sort_labels=True,
        strict_timestamps=True,
        auto_convert_seconds_to_ms=False,
    )
    print("Q1 constructor: accepted every kwarg")

    now = int(time.time() * 1000)
    w.send([{
        "metric": {"__name__": "spike_gauge", "run_id": "spike-1"},
        "values": [1.5],
        "timestamps": [now],
    }])
    time.sleep(1)
    print("Q2 round trip:", query('spike_gauge{run_id="spike-1"}'))

    # Q3: does the stale marker survive Python's protobuf encoder?
    # If it does, the series disappears from an instant query immediately
    # rather than persisting for the 5 minute lookback window.
    w.send([{
        "metric": {"__name__": "spike_gauge", "run_id": "spike-1"},
        "values": [STALE_NAN],
        "timestamps": [now + 1000],
    }])
    time.sleep(1)
    after = query('spike_gauge{run_id="spike-1"}')
    print("Q3 stale marker:", "WORKS (series gone)" if not after else f"NO EFFECT: {after}")

    # Q4: what does an out-of-order sample actually do to a batch?
    # Both series are in one request; if the whole request rolls back,
    # spike_canary will be absent too.
    try:
        w.send([
            {"metric": {"__name__": "spike_canary", "run_id": "spike-1"},
             "values": [99.0], "timestamps": [now + 5000]},
            {"metric": {"__name__": "spike_gauge", "run_id": "spike-1"},
             "values": [2.0], "timestamps": [now - 60_000]},
        ])
        print("Q4 out of order: accepted (unexpected)")
    except Exception as e:  # noqa: BLE001 - this is a spike
        print(f"Q4 out of order: rejected -> {type(e).__name__}: {e}")
    time.sleep(1)
    print("Q4 canary survived:", bool(query('spike_canary{run_id="spike-1"}')))


if __name__ == "__main__":
    main()
```

**Step 4: Run it**

```bash
./tests/harness-up.sh
uv run python scripts/spike_remote_write.py
```

Expected, based on the research but **verify rather than assume**:
- Q1: no `TypeError`. If any kwarg is rejected, drop it and note which.
- Q2: one result with value `1.5`.
- Q3: `WORKS (series gone)`. **If it says `NO EFFECT`, the stale-marker step in Task 7 is dead and
  must be replaced by a comment in the dashboard saying finished runs hold their last value for five
  minutes.** Record whichever answer you get.
- Q4: rejected with a 400 mentioning `out of bounds` or `out of order`, and `canary survived: False`.
  That False is the evidence for the single-writer-thread rule.

**Step 5: Record the answers, delete the spike**

Write what actually happened into `INSTALL_CLAUDE.md` under a heading `## Verified against a real
Prometheus`, with the date. Then:

```bash
rm scripts/spike_remote_write.py
./tests/harness-down.sh
git add tests/ INSTALL_CLAUDE.md
git commit -m "test: a real Prometheus harness, and what it says about remote write"
```

---

## Task 3: The series type and label validation

**Files:**
- Create: `src/sparks/series.py`
- Test: `tests/test_series.py`

Labels are validated here because Prometheus drops invalid ones **silently and still returns 200**.
A client-side check is the only place this failure is visible.

**Step 1: Write the failing tests**

`tests/test_series.py`:

```python
import pytest

from sparks.series import Series, InvalidLabel


def test_labels_are_sorted_and_hashable() -> None:
    a = Series("training_loss", {"seed": "0", "run_id": "r1"})
    b = Series("training_loss", {"run_id": "r1", "seed": "0"})
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_label_names_become_prometheus_labels() -> None:
    s = Series("training_loss", {"run_id": "r1"})
    assert s.as_metric() == {"__name__": "training_loss", "run_id": "r1"}


def test_rejects_a_label_name_prometheus_would_drop() -> None:
    # Prometheus drops these silently and still answers 200, so this is the
    # only place the mistake is ever visible.
    with pytest.raises(InvalidLabel):
        Series("training_loss", {"run-id": "r1"})


def test_rejects_a_reserved_label_name() -> None:
    with pytest.raises(InvalidLabel):
        Series("training_loss", {"__name__": "nope"})


def test_rejects_an_invalid_metric_name() -> None:
    with pytest.raises(InvalidLabel):
        Series("training loss", {})
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_series.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'sparks.series'`

**Step 3: Implement**

`src/sparks/series.py`:

```python
"""A metric name plus its labels, in the one form the rest of the code uses.

Labels are validated on construction rather than on push. Prometheus drops a
series carrying an invalid label name and still answers 200, counting it only in
`prometheus_api_remote_write_invalid_labels_samples_total`, so a typo produces a
green push and no data. This is the only place that mistake is visible.
"""

import re
from dataclasses import dataclass

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class InvalidLabel(ValueError):
    """A metric or label name Prometheus would refuse or silently drop."""


@dataclass(frozen=True)
class Series:
    """One time series: a metric name and a sorted, hashable label set."""

    name: str
    labels: tuple[tuple[str, str], ...]

    def __init__(self, name: str, labels: dict[str, str]) -> None:
        if not NAME.match(name):
            raise InvalidLabel(f"{name!r} is not a valid metric name")
        for key in labels:
            if key.startswith("__"):
                raise InvalidLabel(f"{key!r} is reserved")
            if not LABEL.match(key):
                raise InvalidLabel(f"{key!r} is not a valid label name")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "labels", tuple(sorted(labels.items())))

    def as_metric(self) -> dict[str, str]:
        """The wire form: labels plus the name under `__name__`."""
        return {"__name__": self.name, **dict(self.labels)}
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_series.py -v`
Expected: 5 passed

**Step 5: Commit**

```bash
git add src/sparks/series.py tests/test_series.py
git commit -m "feat: a validated series type, because Prometheus drops bad labels silently"
```

---

## Task 4: The sample buffer

Holds samples between flushes, drops the duplicate-timestamp case that would 400 the whole batch.

**Files:**
- Create: `src/sparks/buffer.py`
- Test: `tests/test_buffer.py`

**Step 1: Write the failing tests**

`tests/test_buffer.py`:

```python
from sparks.buffer import Buffer
from sparks.series import Series

S = Series("training_loss", {"run_id": "r1"})
T = Series("training_step", {"run_id": "r1"})


def test_drain_returns_what_was_added_and_empties() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(T, 2.0, 1000)
    assert len(b.drain()) == 2
    assert b.drain() == []


def test_a_repeated_timestamp_on_one_series_is_dropped() -> None:
    # Two samples with the same ms timestamp and different values is a 400
    # that rolls back the entire batch, so it never reaches the wire.
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(S, 2.0, 1000)
    out = b.drain()
    assert len(out) == 1
    assert out[0]["values"] == [1.0]


def test_the_same_timestamp_on_different_series_is_kept() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(T, 5.0, 1000)
    assert len(b.drain()) == 2


def test_a_timestamp_older_than_one_already_sent_is_dropped() -> None:
    b = Buffer()
    b.add(S, 1.0, 2000)
    b.drain()
    b.add(S, 9.0, 1500)
    assert b.drain() == []


def test_samples_for_one_series_batch_into_a_single_entry() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(S, 2.0, 2000)
    out = b.drain()
    assert len(out) == 1
    assert out[0]["values"] == [1.0, 2.0]
    assert out[0]["timestamps"] == [1000, 2000]
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_buffer.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/buffer.py`:

```python
"""Samples waiting for the next flush.

Two samples for one series with the same millisecond timestamp and different
values is `duplicate sample for timestamp`, an HTTP 400, and remote-write 1.0
has no partial write: one bad sample rolls back every series in the request. A
training loop logging on every step will collide on a fast step, so the
de-duplication happens here and never reaches the wire.
"""

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
            # First value wins: a repeat within one flush window is the
            # collision case, and the earlier reading is the truthful one.
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
                out.append({
                    "metric": series.as_metric(),
                    "values": [samples[t] for t in stamps],
                    "timestamps": stamps,
                })
            return out
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_buffer.py -v`
Expected: 5 passed

**Step 5: Commit**

```bash
git add src/sparks/buffer.py tests/test_buffer.py
git commit -m "feat: a sample buffer that cannot emit a duplicate timestamp"
```

---

## Task 5: The metric registry

Every metric the emitter can produce, declared in one place, so the dashboard checker in Task 12 can
assert that no panel queries a metric nobody emits.

**Files:**
- Create: `src/sparks/metrics.py`
- Test: `tests/test_metrics.py`

**Step 1: Write the failing test**

`tests/test_metrics.py`:

```python
from sparks.metrics import METRICS
from sparks.series import NAME


def test_every_declared_metric_is_a_valid_prometheus_name() -> None:
    assert METRICS
    for name in METRICS:
        assert NAME.match(name), name


def test_the_names_the_dashboard_depends_on_are_declared() -> None:
    for name in (
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_heartbeat_timestamp_seconds",
        "training_run_end_timestamp_seconds",
        "training_run_status",
        "training_loss",
        "training_step",
        "training_epoch",
    ):
        assert name in METRICS
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/metrics.py`:

```python
"""Every metric this package emits, and what it means.

The dashboard checker reads this to refuse a panel querying something nothing
emits. Adding a metric means adding it here first.

`status` is deliberately NOT a label on `training_run_info`. Changing a label on
an info metric creates a second series, remote-written series are never marked
stale automatically, and the join every panel uses then fails with `found
duplicate series for the match group` and the panel goes red. Terminal state
lives on its own metrics instead.
"""

METRICS: dict[str, str] = {
    # Identity and lifecycle. Immutable labels only, re-pushed every cycle so
    # the join's right side never falls out of the 5 minute lookback window.
    "training_run_info": "1, carrying the run's immutable metadata as labels",
    "training_run_start_timestamp_seconds": "unix seconds, written once at start",
    "training_run_heartbeat_timestamp_seconds": "unix seconds, refreshed every flush",
    "training_run_end_timestamp_seconds": "unix seconds, written once at the end",
    "training_run_status": "1, labelled finished or crashed, written once at the end",
    # Progress.
    "training_epoch": "fractional epoch",
    "training_step": "optimizer steps since this run began",
    "training_loss": "training loss for the last batch",
    "training_grad_norm": "gradient L2 norm, labelled by parameter group",
    "training_learning_rate": "learning rate, labelled by parameter group",
    # Throughput.
    "training_steps_per_sec": "optimizer steps per second over the last window",
    "training_tokens_per_sec": "tokens per second over the last window",
    # Held-out evaluation, emitted once per epoch rather than per step.
    "training_eval_loss": "held-out loss, labelled by head",
}
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: 2 passed

**Step 5: Commit**

```bash
git add src/sparks/metrics.py tests/test_metrics.py
git commit -m "feat: declare every metric in one place"
```

---

## Task 6: The emitter, part one - construction and buffering

**Files:**
- Create: `src/sparks/emit.py`
- Test: `tests/test_emit.py`

The public API. No network yet: this task proves the object buffers the right samples, and Task 7
adds the thread that sends them.

**Step 1: Write the failing tests**

`tests/test_emit.py`:

```python
from sparks.emit import RunMetrics


def names(drained: list[dict]) -> set[str]:
    return {d["metric"]["__name__"] for d in drained}


def make(**kw) -> RunMetrics:
    # autostart=False keeps the pump thread out of the unit tests; the live
    # test in tests/test_live.py is what exercises the thread and the network.
    return RunMetrics(run_id="run-1", url="http://unused", autostart=False, **kw)


def test_begin_emits_identity_and_start_time() -> None:
    m = make(info={"model": "helium"})
    m.begin()
    out = m._buffer.drain()
    assert names(out) == {
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_heartbeat_timestamp_seconds",
    }


def test_info_carries_run_id_and_the_supplied_metadata() -> None:
    m = make(info={"model": "helium"})
    m.begin()
    info = next(d for d in m._buffer.drain() if d["metric"]["__name__"] == "training_run_info")
    assert info["metric"]["run_id"] == "run-1"
    assert info["metric"]["model"] == "helium"
    assert info["values"] == [1.0]


def test_log_emits_one_series_per_named_value() -> None:
    m = make()
    m.log(step=3, loss=0.5)
    assert names(m._buffer.drain()) == {"training_step", "training_loss"}


def test_log_refuses_an_undeclared_metric() -> None:
    import pytest

    m = make()
    with pytest.raises(KeyError):
        m.log(not_a_real_metric=1.0)


def test_series_labels_land_on_every_sample() -> None:
    m = make(labels={"arm": "real", "seed": "0"})
    m.log(loss=0.5)
    loss = m._buffer.drain()[0]
    assert loss["metric"]["arm"] == "real"
    assert loss["metric"]["seed"] == "0"
    assert loss["metric"]["run_id"] == "run-1"


def test_end_emits_terminal_state_and_never_mutates_info() -> None:
    m = make()
    m.begin()
    m._buffer.drain()
    m.end("finished")
    out = m._buffer.drain()
    assert "training_run_end_timestamp_seconds" in names(out)
    status = next(d for d in out if d["metric"]["__name__"] == "training_run_status")
    assert status["metric"]["status"] == "finished"
    # The info metric must never be re-emitted with different labels: a second
    # label set means a second series and a red panel.
    infos = [d for d in out if d["metric"]["__name__"] == "training_run_info"]
    assert all("status" not in d["metric"] for d in infos)


def test_a_metric_labelled_by_group_keeps_the_group_label() -> None:
    m = make()
    m.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})
    out = m._buffer.drain()
    assert {d["metric"]["group"] for d in out} == {"lora", "tables"}
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_emit.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/emit.py` (buffering half; the pump arrives in Task 7):

```python
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
        raise NotImplementedError("Task 7")
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_emit.py -v`
Expected: 7 passed

**Step 5: Commit**

```bash
git add src/sparks/emit.py tests/test_emit.py
git commit -m "feat: the run metrics object, buffering half"
```

---

## Task 7: The emitter, part two - the pump thread

**Files:**
- Modify: `src/sparks/emit.py` (replace `_start` and `_shutdown`)
- Test: `tests/test_emit.py` (add)

One thread, doing both the heartbeat and the flush. Two threads would be two concurrent `send()`
calls, and remote-write 1.0 rolls back an entire request on one bad sample, so the second writer can
silently discard the first one's batch.

**Step 1: Write the failing tests**

Append to `tests/test_emit.py`:

```python
def test_shutdown_is_idempotent() -> None:
    m = make()
    m.begin()
    m.end("finished")
    m.end("finished")  # must not raise


def test_a_push_failure_does_not_propagate() -> None:
    # A metrics outage must never kill a training run. The URL is unroutable.
    m = RunMetrics(run_id="run-1", url="http://127.0.0.1:1", autostart=True)
    m.begin()
    m.log(loss=0.5)
    m.end("finished")  # must not raise
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_emit.py -v`
Expected: FAIL with `NotImplementedError: Task 7`

**Step 3: Implement**

Add the import at the top of `src/sparks/emit.py`:

```python
import atexit
import struct
import threading

from prometheus_remote_writer import RemoteWriter

STALE_NAN = struct.unpack("<d", struct.pack("<Q", 0x7FF0000000000002))[0]
"""Prometheus's stale marker. A pushed series is never marked stale
automatically, so without this a finished run holds its last value for five
minutes and then vanishes. Task 2's spike is what proved this survives the
Python protobuf encoder."""
```

Replace `_start` and `_shutdown`:

```python
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
        except Exception as e:  # noqa: BLE001 - telemetry never kills a run
            LOG.warning("sparks: dropped %d series: %s", len(batch), e)

    def _shutdown(self) -> None:
        if self._stop is None or self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=FLUSH_SECONDS * 2)
        self._flush()
        self._mark_stale()

    def _mark_stale(self) -> None:
        """End every series this run wrote, so a finished run stops dead on the
        graph instead of flat-lining for the lookback window."""
        ended = int(time.time() * 1000)
        batch = [
            {"metric": s.as_metric(), "values": [STALE_NAN], "timestamps": [ended]}
            for s in self._buffer.seen()
        ]
        if not batch:
            return
        try:
            self._writer.send(batch)
        except Exception as e:  # noqa: BLE001
            LOG.warning("sparks: could not mark %d series stale: %s", len(batch), e)
```

Add to `src/sparks/buffer.py`:

```python
    def seen(self) -> list[Series]:
        """Every series this buffer has ever sent, for the stale markers."""
        with self._lock:
            return list(self._last)
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_emit.py -v`
Expected: 9 passed. The unroutable-URL test takes a few seconds because of the retry backoff; that is
the retry policy working.

**Step 5: Commit**

```bash
git add src/sparks/emit.py src/sparks/buffer.py tests/test_emit.py
git commit -m "feat: one pump thread, and stale markers when a run ends"
```

---

## Task 8: Minting a run id

**Files:**
- Create: `src/sparks/run.py`
- Test: `tests/test_run.py`

**Step 1: Write the failing tests**

`tests/test_run.py`:

```python
import re

from sparks.run import git_sha, new_run_id


def test_run_ids_sort_chronologically_as_strings() -> None:
    early = new_run_id("e0", when=1754300000.0)
    late = new_run_id("e0", when=1754390000.0)
    assert early < late


def test_the_shape_is_stable() -> None:
    rid = new_run_id("e0", when=1754300000.0)
    assert re.match(r"^run-\d{8}-\d{4}-e0$", rid), rid


def test_a_name_is_slugified_so_it_is_a_safe_label_value() -> None:
    assert new_run_id("E0 real/shuffled", when=1754300000.0).endswith("-e0-real-shuffled")


def test_git_sha_is_short_or_unknown() -> None:
    sha = git_sha()
    assert sha == "unknown" or re.match(r"^[0-9a-f]{7,12}$", sha)
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/run.py`:

```python
"""Naming a run, and the metadata that identifies it."""

import re
import subprocess
import time
from pathlib import Path


def new_run_id(name: str, when: float | None = None) -> str:
    """`run-YYYYmmdd-HHMM-<name>`, so runs sort chronologically as strings.

    That matters because the Grafana variable sorts them as strings and picks
    the first, which is how the newest run ends up selected on load."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when or time.time()))
    return f"run-{stamp}-{slug(name)}"


def slug(name: str) -> str:
    """A label-value-safe form: a run id ends up in a PromQL regex."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def git_sha(repo: Path | None = None) -> str:
    """Short HEAD sha, or `unknown` outside a checkout.

    Never raises: a run must not fail because it was launched from a tarball."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or Path.cwd(),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return out.stdout.strip() or "unknown"
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_run.py -v`
Expected: 4 passed

**Step 5: Commit**

```bash
git add src/sparks/run.py tests/test_run.py
git commit -m "feat: run ids that sort chronologically"
```

---

## Task 9: The synthetic run

This is the acceptance test for the whole pipeline. `bbm` has no training code that has ever been
run, so a demo indistinguishable from a real run in Grafana is the only way to know the dashboard
works before there is anything to watch.

Shape it on the real loop's measured numbers: 8 optimizer steps per epoch, 30 to 60 epochs, roughly
6 to 12 s per epoch. So one step per second, and held-out metrics once per epoch.

**Files:**
- Create: `src/sparks/demo.py`
- Test: `tests/test_demo.py`

**Step 1: Write the failing tests**

`tests/test_demo.py`:

```python
from sparks.demo import curve


def test_loss_decays_and_stays_positive() -> None:
    values = [curve(step, total=480, seed=0) for step in range(480)]
    assert all(v > 0 for v in values)
    assert sum(values[:20]) / 20 > sum(values[-20:]) / 20


def test_it_is_noisy_rather_than_monotonic() -> None:
    # A perfectly smooth curve reads as fake at a glance.
    values = [curve(step, total=480, seed=0) for step in range(480)]
    assert any(b > a for a, b in zip(values, values[1:], strict=True))


def test_the_same_seed_gives_the_same_curve() -> None:
    a = [curve(s, total=100, seed=7) for s in range(100)]
    b = [curve(s, total=100, seed=7) for s in range(100)]
    assert a == b
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_demo.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/demo.py`:

```python
"""A synthetic training run.

The acceptance test for the whole pipeline: it must be indistinguishable from a
real run in Grafana. The shape follows bbm's measured loop - 8 optimizer steps
per epoch, 30 to 60 epochs, 6 to 12 s per epoch - so the dashboard is tuned
against realistic cadence rather than against a tight loop.
"""

import math
import random
import time

from sparks.emit import RunMetrics
from sparks.run import git_sha, new_run_id

STEPS_PER_EPOCH = 8
EPOCHS = 40
STEP_SECONDS = 1.0


def curve(step: int, total: int, seed: int) -> float:
    """Exponential decay to a floor, plus reproducible noise."""
    rng = random.Random((seed, step))
    decayed = 0.35 + 3.2 * math.exp(-4.0 * step / max(1, total))
    return max(0.01, decayed * (1.0 + rng.gauss(0.0, 0.06)))


def run(url: str, name: str = "demo", seed: int = 0) -> str:
    """Play a full synthetic run and return its run_id."""
    total = EPOCHS * STEPS_PER_EPOCH
    run_id = new_run_id(name)
    metrics = RunMetrics(
        run_id=run_id,
        url=url,
        info={
            "run_name": name,
            "git_sha": git_sha(),
            "model": "synthetic-2b",
            "dataset": "synthetic",
        },
        labels={"arm": "demo", "seed": str(seed)},
    )
    with metrics as m:
        started = time.monotonic()
        for step in range(total):
            time.sleep(STEP_SECONDS)
            loss = curve(step, total, seed)
            elapsed = time.monotonic() - started
            m.log(
                step=step,
                epoch=step / STEPS_PER_EPOCH,
                loss=loss,
                steps_per_sec=(step + 1) / max(elapsed, 1e-6),
                tokens_per_sec=1180.0 * (step + 1) / max(elapsed, 1e-6),
            )
            m.log_group("training_grad_norm", {"lora": loss * 1.7, "tables": loss * 0.2})
            m.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})
            if (step + 1) % STEPS_PER_EPOCH == 0:
                m.log_group(
                    "training_eval_loss",
                    {"draw": loss * 1.1, "say": loss * 0.85},
                )
    return run_id
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_demo.py -v`
Expected: 3 passed

**Step 5: Commit**

```bash
git add src/sparks/demo.py tests/test_demo.py
git commit -m "feat: a synthetic run shaped like the real one"
```

---

## Task 10: The CLI

**Files:**
- Create: `src/sparks/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import pytest

from sparks.cli import build_parser, deep_link


def test_demo_takes_a_name_and_a_seed() -> None:
    args = build_parser().parse_args(["demo", "--name", "e0", "--seed", "3"])
    assert (args.name, args.seed) == ("e0", 3)


def test_the_prometheus_url_has_a_working_default() -> None:
    assert build_parser().parse_args(["demo"]).url == "http://127.0.0.1:9090"


def test_the_deep_link_starts_a_minute_early() -> None:
    # Otherwise the first datapoints are glued to the axis.
    link = deep_link("http://spark.local", "run-1", started=1_754_300_000.0)
    assert "var-run_id=run-1" in link
    assert "from=1754299940000" in link
    assert link.endswith("&to=now&refresh=10s")


def test_an_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["nope"])
    assert e.value.code == 2
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/cli.py`:

```python
"""    sparks demo --name e0

Plays a synthetic run against the box's Prometheus and prints the Grafana link
to watch it on.
"""

import argparse
import logging
import sys
import time

from sparks import demo

DASHBOARD = "/d/training-runs/training-runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks", description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:9090",
        help="Prometheus, which must have the remote-write receiver enabled",
    )
    parser.add_argument(
        "--grafana", default="http://spark.local", help="where the dashboard lives"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("demo", help="play a synthetic run")
    run.add_argument("--name", default="demo")
    run.add_argument("--seed", type=int, default=0)
    return parser


def deep_link(grafana: str, run_id: str, started: float) -> str:
    """The live form. Backdated a minute so the first samples are not glued to
    the left edge of the graph."""
    frm = int((started - 60) * 1000)
    return (
        f"{grafana.rstrip('/')}{DASHBOARD}"
        f"?orgId=1&var-run_id={run_id}&from={frm}&to=now&refresh=10s"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    started = time.time()
    run_id = demo.run(args.url, name=args.name, seed=args.seed)
    print(run_id)
    print(deep_link(args.grafana, run_id, started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note the ordering problem this hides: `demo.run` mints the id itself and blocks until the run is
over, so the link only prints at the end. Fix it properly in slice 2 when `sparks run -- <cmd>`
arrives and the launcher owns the id. Leave it for now rather than restructuring twice.

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 4 passed

**Step 5: Commit**

```bash
git add src/sparks/cli.py tests/test_cli.py
git commit -m "feat: sparks demo"
```

---

## Task 11: The dashboard

**Files:**
- Create: `dashboards/training-runs.json`

Every JSON detail below was verified against Grafana v13.1.1 source, not recalled. Do not "tidy" any
of it.

- `uid` is `training-runs`. It must **not** be `spark-overview`: sparkup's Ansible `copy` task
  overwrites by basename, and a uid collision makes Grafana drop one board.
- **Omit `id` entirely.** Grafana's file provisioner nulls it, but a stale numeric id breaks an
  import.
- `sort: 8` is **Natural DESC**, confirmed from `VariableSort` in `types.gen.ts` at v13.1.1. The
  public docs list no numeric values at all. It matters because plain alphabetical puts `run-9` above
  `run-10`.
- `refresh: 2` is `onTimeRangeChanged`.
- Every matcher is `=~`, never `=`. A multi-select variable interpolates to `(a|b)`, so `=` silently
  matches nothing the moment a second run is ticked.
- The info join wraps the right side in `max by (...)`. Two `training_run_info` series sharing a
  `run_id` is a hard query error and the panel goes fully red rather than degrading.
- No annotation queries in this slice. `POST /api/annotations` needs credentials the box does not
  have (anonymous is Viewer), so annotations are slice 2.

```json
{
  "uid": "training-runs",
  "title": "Training runs",
  "description": "One row per selected run. Panels come from metrics pushed by the sparks emitter over Prometheus remote-write, not from a scrape, so a finished run's series stop dead rather than reporting zero. The GPU row is scraped by sparkup and is here so training curves and the hardware they ran on share one time axis.",
  "tags": ["training", "sparks"],
  "editable": true,
  "timezone": "browser",
  "graphTooltip": 1,
  "schemaVersion": 39,
  "version": 1,
  "refresh": "10s",
  "time": { "from": "now-3h", "to": "now" },
  "annotations": { "list": [] },
  "templating": {
    "list": [
      {
        "type": "query",
        "name": "run_id",
        "label": "Run",
        "description": "Every run_id training_run_info carried inside the dashboard time range. Sorted natural-descending (8) so the newest run is selected on load; alphabetical would put run-9 above run-10.",
        "datasource": { "type": "prometheus", "uid": "prometheus" },
        "definition": "label_values(training_run_info, run_id)",
        "query": {
          "qryType": 1,
          "query": "label_values(training_run_info, run_id)",
          "refId": "PrometheusVariableQueryEditor-VariableQuery"
        },
        "current": {},
        "options": [],
        "refresh": 2,
        "sort": 8,
        "multi": true,
        "includeAll": false,
        "regex": "",
        "skipUrlSync": false,
        "hide": 0
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "row",
      "title": "Run",
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 },
      "collapsed": false,
      "panels": []
    },
    {
      "id": 2,
      "type": "stat",
      "title": "Step",
      "description": "Optimizer steps since the run began. Step is a value and never a label: one series per step would be hundreds of thousands of single-sample series and would take the whole Prometheus down.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 4, "x": 0, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "short", "decimals": 0 }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto",
        "colorMode": "none"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_step{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}}"
        }
      ]
    },
    {
      "id": 3,
      "type": "stat",
      "title": "Epoch",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 4, "x": 4, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "short", "decimals": 2 }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto",
        "colorMode": "none"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_epoch{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}}"
        }
      ]
    },
    {
      "id": 4,
      "type": "stat",
      "title": "Latest loss",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 4, "x": 8, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "short", "decimals": 4 }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto",
        "colorMode": "none"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_loss{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}}"
        }
      ]
    },
    {
      "id": 5,
      "type": "stat",
      "title": "Alive",
      "description": "Seconds since the last heartbeat. The heartbeat is refreshed by a background thread every 5s and freezes when the run dies, so one expression covers live and finished runs. last_over_time is load-bearing: a pushed series vanishes from an instant query 5 minutes after its last sample, so the bare metric would return nothing at all for a run that ended an hour ago.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 12, "y": 1 },
      "fieldConfig": {
        "defaults": {
          "unit": "s",
          "decimals": 0,
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "color": "green", "value": null },
              { "color": "red", "value": 90 }
            ]
          }
        },
        "overrides": []
      },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto",
        "colorMode": "value"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "time() - last_over_time(training_run_heartbeat_timestamp_seconds{run_id=~\"$run_id\"}[30d])",
          "legendFormat": "{{run_id}}"
        }
      ]
    },
    {
      "id": 6,
      "type": "stat",
      "title": "Duration",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 18, "y": 1 },
      "fieldConfig": { "defaults": { "unit": "s", "decimals": 0 }, "overrides": [] },
      "options": {
        "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false },
        "textMode": "auto",
        "colorMode": "none"
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "last_over_time(training_run_heartbeat_timestamp_seconds{run_id=~\"$run_id\"}[30d]) - last_over_time(training_run_start_timestamp_seconds{run_id=~\"$run_id\"}[30d])",
          "legendFormat": "{{run_id}}"
        }
      ]
    },
    {
      "id": 10,
      "type": "timeseries",
      "title": "Loss",
      "description": "The matcher is =~ because a multi-select variable interpolates to a regex alternation; with = the panel silently empties the moment a second run is ticked. The right side of the join is wrapped in max by(...) because two training_run_info series sharing a run_id is a hard query error that turns this panel red rather than degrading it.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 9, "w": 12, "x": 0, "y": 5 },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "min": 0,
          "custom": { "fillOpacity": 0, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": {
          "displayMode": "table",
          "placement": "right",
          "showLegend": true,
          "calcs": ["min", "lastNotNull"]
        },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_loss{run_id=~\"$run_id\"} * on(run_id) group_left(run_name, git_sha) max by (run_id, run_name, git_sha) (training_run_info)",
          "legendFormat": "{{run_name}} {{arm}} s{{seed}}"
        }
      ]
    },
    {
      "id": 11,
      "type": "timeseries",
      "title": "Held-out loss",
      "description": "Emitted once per epoch, not per step, because it needs a full evaluation pass. Labelled by head: bbm's two-stream model has separate draw and say losses and averaging them hides the one the experiment turns on.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 9, "w": 12, "x": 12, "y": 5 },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "min": 0,
          "custom": { "fillOpacity": 0, "lineWidth": 1, "showPoints": "always" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_eval_loss{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}} {{group}}"
        }
      ]
    },
    {
      "id": 12,
      "type": "timeseries",
      "title": "Gradient norm",
      "description": "Labelled by parameter group. A single global norm would mix LoRA's zero-initialised B matrices, which are expected to travel, with warm-started embedding tables that are expected to barely move, and the sum means nothing.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 0, "y": 14 },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "min": 0,
          "custom": { "fillOpacity": 0, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_grad_norm{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}} {{group}}"
        }
      ]
    },
    {
      "id": 13,
      "type": "timeseries",
      "title": "Learning rate",
      "description": "Two parameter groups at a deliberate 10x ratio: LoRA at 2e-4 because its zero-initialised B has to travel, the warm-started draw tables at 2e-5 because that rate would walk them off a good point. A single learning_rate series would be wrong by 10x for one of them.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 8, "y": 14 },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "decimals": 6,
          "custom": { "fillOpacity": 0, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_learning_rate{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}} {{group}}"
        }
      ]
    },
    {
      "id": 14,
      "type": "timeseries",
      "title": "Throughput",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 16, "y": 14 },
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "min": 0,
          "custom": { "fillOpacity": 0, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": [
          {
            "matcher": { "id": "byRegexp", "options": ".*tokens.*" },
            "properties": [{ "id": "custom.axisPlacement", "value": "right" }]
          }
        ]
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_steps_per_sec{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}} steps/s"
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "training_tokens_per_sec{run_id=~\"$run_id\"}",
          "legendFormat": "{{run_id}} tokens/s"
        }
      ]
    },
    {
      "id": 20,
      "type": "row",
      "title": "The hardware it ran on",
      "gridPos": { "h": 1, "w": 24, "x": 0, "y": 22 },
      "collapsed": false,
      "panels": []
    },
    {
      "id": 21,
      "type": "timeseries",
      "title": "GPU utilisation",
      "description": "Scraped by sparkup, not pushed. On this dashboard so a throughput collapse and the hardware state that caused it share one time axis; that correlation is the reason for using the infra Grafana instead of a separate experiment tracker.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 0, "y": 23 },
      "fieldConfig": {
        "defaults": {
          "unit": "percent",
          "min": 0,
          "max": 100,
          "custom": { "fillOpacity": 10, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "nvidia_smi_utilization_gpu_ratio * 100",
          "legendFormat": "gpu"
        }
      ]
    },
    {
      "id": 22,
      "type": "timeseries",
      "title": "System power",
      "description": "The firmware's whole-box DC figure, through spbm. Empty on a box where spbm_enabled is false, which is the expected state and not a fault. sys_total excludes PSU conversion loss, so it reads under a wall-socket meter.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 8, "y": 23 },
      "fieldConfig": {
        "defaults": {
          "unit": "watt",
          "min": 0,
          "custom": { "fillOpacity": 10, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "node_hwmon_power_watt * on(chip, sensor) group_left(label) node_hwmon_sensor_label{label=\"sys_total\"}",
          "legendFormat": "sys_total"
        }
      ]
    },
    {
      "id": 23,
      "type": "timeseries",
      "title": "Unified memory",
      "description": "node_memory_ is the GPU memory signal on this hardware. nvidia-smi reports [N/A] for GPU memory because the memory is unified, and the exporter drops the field; that absence is by design.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 8, "x": 16, "y": 23 },
      "fieldConfig": {
        "defaults": {
          "unit": "bytes",
          "min": 0,
          "custom": { "fillOpacity": 10, "lineWidth": 1, "showPoints": "never" }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
          "legendFormat": "used"
        }
      ]
    }
  ]
}
```

**Verification**

Run: `uv run python -c "import json; d=json.load(open('dashboards/training-runs.json')); print(d['uid'], len(d['panels']))"`
Expected: `training-runs 15`

Run: `uv run python -c "
import json
d = json.load(open('dashboards/training-runs.json'))
assert 'id' not in d, 'a provisioned dashboard must not carry an id'
ids = [p['id'] for p in d['panels']]
assert len(ids) == len(set(ids)), 'duplicate panel ids'
print('ok')
"`
Expected: `ok`

**Commit**

```bash
git add dashboards/training-runs.json
git commit -m "feat: the training runs dashboard"
```

---

## Task 12: The dashboard checker

sparkup's `tests/check_dashboard.py` cannot validate this board. It hardcodes
`DASHBOARD = REPO_ROOT / "roles/monitoring/files/dashboards/spark-overview.json"`, asserts the uid
equals the configured home dashboard, derives its metric allowlist purely from
`roles/exporters/defaults/main.yml` so every pushed metric is rejected, and raises `CheckFailed` on
any expression containing a `$`, which is every panel here. So sparks needs its own, built on the
same idea: **never query a metric nobody emits.**

**Files:**
- Create: `tests/check_dashboard.py`
- Test: `tests/test_check_dashboard.py`
- Modify: `Makefile`

**Step 1: Write the failing tests**

`tests/test_check_dashboard.py`:

```python
import pytest

from tests.check_dashboard import CheckFailed, check, substitute


def test_variables_are_substituted_not_rejected() -> None:
    assert "$" not in substitute('training_loss{run_id=~"$run_id"}')


def test_rate_interval_is_substituted() -> None:
    assert "$" not in substitute("rate(training_step[$__rate_interval])")


def test_an_unknown_variable_is_an_error() -> None:
    with pytest.raises(CheckFailed):
        substitute("training_loss{x=~\"$nope\"}")


def test_the_shipped_dashboard_passes() -> None:
    check()


def test_a_panel_querying_an_undeclared_metric_fails() -> None:
    with pytest.raises(CheckFailed):
        check(extra_exprs=["training_invented_metric"])
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_check_dashboard.py -v`
Expected: FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`tests/check_dashboard.py`:

```python
"""Every panel query names a metric something actually emits.

sparkup's equivalent cannot check this board: it hardcodes one file path,
asserts the uid matches the Grafana home page, derives its allowlist only from
node_exporter's collectors, and refuses any expression containing a Grafana
variable. This is the same rule applied to a pushed dashboard.

    uv run python tests/check_dashboard.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sparks.metrics import METRICS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboards" / "training-runs.json"

# Scraped by sparkup, so legitimate here even though sparks never emits them.
# Prefixes, because node_exporter names a series after every key in
# /proc/meminfo and an exhaustive list would be a claim about a kernel.
SCRAPED_PREFIXES = ("node_", "nvidia_smi_", "up", "scrape_")

# What a Grafana variable becomes for parsing purposes. A real run id, so the
# regex the datasource would generate is the regex promtool parses.
VARIABLES = {
    "$run_id": "run-20260804-1530-demo",
    "$__rate_interval": "5m",
    "$__interval": "1m",
    "$__range": "3h",
}

METRIC = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?=[({\[]|\s|$)")
KEYWORDS = {
    "by", "on", "group_left", "group_right", "without", "ignoring", "offset",
    "and", "or", "unless", "bool", "rate", "increase", "sum", "avg", "min",
    "max", "count", "topk", "time", "last_over_time", "avg_over_time", "label",
}


class CheckFailed(Exception):
    """A panel query this dashboard should not ship with."""


def substitute(expr: str) -> str:
    for name, value in VARIABLES.items():
        expr = expr.replace(name, value)
    if "$" in expr:
        raise CheckFailed(f"unknown Grafana variable in {expr!r}")
    return expr


def metric_names(expr: str) -> set[str]:
    """Every identifier that looks like a metric name."""
    found = set()
    for name in METRIC.findall(expr):
        if name in KEYWORDS or name.isdigit():
            continue
        found.add(name)
    return found


def allowed(name: str) -> bool:
    return name in METRICS or name.startswith(SCRAPED_PREFIXES)


def expressions(dashboard: dict) -> list[str]:
    out = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if "expr" in target:
                out.append(target["expr"])
    return out


def check(extra_exprs: list[str] | None = None) -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    exprs = expressions(dashboard) + list(extra_exprs or [])
    if not exprs:
        raise CheckFailed("the dashboard has no queries at all")
    for expr in exprs:
        for name in metric_names(substitute(expr)):
            if not allowed(name):
                raise CheckFailed(
                    f"{name!r} is neither declared in sparks.metrics.METRICS "
                    f"nor scraped by sparkup: {expr!r}"
                )


def main() -> int:
    try:
        check()
    except CheckFailed as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"ok: {DASHBOARD.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Add to `Makefile`:

```makefile
dashboard:
	uv run python tests/check_dashboard.py
```

and put `dashboard` into `check`:

```makefile
check: lint typecheck test dashboard
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_check_dashboard.py -v`
Expected: 5 passed

Run: `make dashboard`
Expected: `ok: training-runs.json`

**Step 5: Commit**

```bash
git add tests/check_dashboard.py tests/test_check_dashboard.py Makefile
git commit -m "test: refuse a panel querying a metric nobody emits"
```

---

## Task 13: The live test

Real Prometheus, real remote write, real query. No mocks. This is what proves the emitter works;
everything before it proves the emitter is internally consistent.

**Files:**
- Create: `tests/test_live.py`

**Step 1: Write the test**

`tests/test_live.py`:

```python
"""Against a real Prometheus from tests/harness-up.sh. No mocks: the thing
under test is the wire format and the receiver's opinion of it, and a fake
would only ever confirm our own assumptions.

    make live
"""

import time

import pytest
import requests

from sparks.emit import RunMetrics
from sparks.run import new_run_id

URL = "http://127.0.0.1:19091"

pytestmark = pytest.mark.live


def query(expr: str) -> list[dict]:
    r = requests.get(f"{URL}/api/v1/query", params={"query": expr}, timeout=5)
    r.raise_for_status()
    return r.json()["data"]["result"]


def wait_for(expr: str, seconds: float = 20.0) -> list[dict]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = query(expr)
        if got:
            return got
        time.sleep(0.5)
    raise AssertionError(f"nothing matched {expr!r} within {seconds}s")


def test_a_run_is_visible_end_to_end() -> None:
    run_id = new_run_id("live")
    m = RunMetrics(
        run_id=run_id,
        url=URL,
        info={"run_name": "live", "git_sha": "abc1234", "model": "test"},
        labels={"arm": "real", "seed": "0"},
    )
    m.begin()
    for step in range(5):
        m.log(step=step, loss=1.0 / (step + 1))
        time.sleep(0.2)

    info = wait_for(f'training_run_info{{run_id="{run_id}"}}')
    assert info[0]["metric"]["git_sha"] == "abc1234"
    assert info[0]["value"][1] == "1"

    loss = wait_for(f'training_loss{{run_id="{run_id}"}}')
    assert loss[0]["metric"]["arm"] == "real"

    # The join every panel uses must resolve, and must not corrupt the value.
    joined = wait_for(
        f'training_loss{{run_id="{run_id}"}} * on(run_id) '
        f"group_left(run_name, git_sha) "
        f"max by (run_id, run_name, git_sha) (training_run_info)"
    )
    assert float(joined[0]["value"][1]) == float(loss[0]["value"][1])

    m.end("finished")
    status = wait_for(f'training_run_status{{run_id="{run_id}"}}')
    assert status[0]["metric"]["status"] == "finished"

    # The info metric must not have acquired a status label: a second label set
    # is a second series and turns every joined panel red.
    infos = query(f'training_run_info{{run_id="{run_id}"}}')
    assert len(infos) == 1, f"info metric split into {len(infos)} series"
    assert "status" not in infos[0]["metric"]


def test_the_dashboard_variable_query_returns_the_run() -> None:
    run_id = new_run_id("live-var")
    m = RunMetrics(run_id=run_id, url=URL, info={"run_name": "live-var"})
    m.begin()
    wait_for(f'training_run_info{{run_id="{run_id}"}}')
    r = requests.get(
        f"{URL}/api/v1/label/run_id/values",
        params={"match[]": "training_run_info"},
        timeout=5,
    )
    r.raise_for_status()
    assert run_id in r.json()["data"]
    m.end("finished")
```

**Step 2: Run it**

```bash
make live
```

Expected: 2 passed. If `test_a_run_is_visible_end_to_end` fails on the info-split assertion, the
`end()` path is re-emitting info with a changed label set. That is the exact bug this plan corrects,
so fix the code, never the assertion.

**Step 3: Commit**

```bash
git add tests/test_live.py
git commit -m "test: a run end to end against a real Prometheus"
```

---

## Task 14: Install on the box and watch a real demo

The point of the slice. Everything until now was rehearsal.

**Step 1: Push the dashboard**

The dashboard goes into the directory sparkup already provisions. This survives a sparkup
re-converge: `roles/monitoring/tasks/main.yml:55-63` uses `ansible.builtin.copy`, which has no purge
option and can only overwrite a file whose **basename** collides with something in
`roles/monitoring/files/dashboards/`. Today that is only `spark-overview.json`. `remove_orphans:
true` removes containers, not files. Grafana's `spark` provider rescans on a 10s timer, so no restart
is needed.

```bash
scp dashboards/training-runs.json vlad@spark.local:/tmp/
ssh vlad@spark.local 'sudo install -o root -g root -m 0644 \
  /tmp/training-runs.json /opt/monitoring/grafana/dashboards/training-runs.json'
```

Wait 15 seconds, then confirm Grafana picked it up:

```bash
ssh vlad@spark.local 'curl -s http://127.0.0.1/api/search?query= | python3 -m json.tool | grep -A1 uid'
```

Expected: both `spark-overview` and `training-runs`.

**Step 2: Install sparks into the training venv**

Not into `~/bbm/.venv`. That one is built from bbm's `uv.lock` and carries the Pillow pin the whole
cross-platform determinism story rests on; `bbm/scripts/spark.sh` rebuilds it with `uv sync
--frozen`, which would drop anything added by hand.

```bash
rsync -az --delete --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' \
  /Users/whitemonk/projects/ai/sparks/ vlad@spark.local:~/sparks/
ssh vlad@spark.local '~/bbm-train/.venv/bin/pip install -e ~/sparks'
ssh vlad@spark.local '~/bbm-train/.venv/bin/python -c "import sparks; print(\"ok\")"'
```

Expected: `ok`

**Step 3: Play a demo run**

40 epochs at 8 steps of 1 s is about 5 minutes and 30 seconds.

```bash
ssh vlad@spark.local '~/bbm-train/.venv/bin/sparks demo --name acceptance'
```

Expected: a run id and a URL. Open the URL. While it plays, confirm:

- the `Run` dropdown lists the run and has it selected;
- Loss falls with visible noise, not as a smooth curve;
- Step and Epoch climb;
- Alive stays green and under 10 s;
- Learning rate shows two flat lines an order of magnitude apart;
- Held-out loss shows points every 8 steps, not every step;
- the GPU row is drawing, because that is the whole reason for using this Grafana.

When it ends, wait one minute and confirm Loss **stops** rather than flat-lining. If it flat-lines
for five minutes, the stale markers are not working and Task 2's Q3 answer was `NO EFFECT`; say so in
`INSTALL_CLAUDE.md` rather than leaving the discrepancy.

**Step 4: Check what the receiver thought of it**

```bash
ssh vlad@spark.local 'curl -s --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode "query=prometheus_api_remote_write_invalid_labels_samples_total"'
```

Expected: absent, or `0`. **Anything above zero means series were dropped silently while the push
returned 200**, and the label names need fixing.

```bash
ssh vlad@spark.local 'curl -s --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode "query=prometheus_tsdb_out_of_order_samples_total"'
```

Expected: `0`. Above zero means two writers raced, which should be impossible with one pump thread.

**Step 5: Commit the record**

Write what the dashboard actually looked like into `INSTALL_CLAUDE.md`, including anything that
differed from this plan.

```bash
git add INSTALL_CLAUDE.md
git commit -m "docs: what the first real demo run looked like"
```

---

## Task 15: The two documents

Per Vlad's standing rule, these have opposite audiences and must not be merged.

**Files:**
- Create: `README.md` (human: what it is, how to run it, nothing else)
- Create: `INSTALL_CLAUDE.md` (agent: decisions, traps, exact paths)

**Step 1: `README.md`**

Short. No justification, no counts, no benchmarks.

```markdown
# sparks

Training runs on the DGX Spark, with the curves in Grafana next to the hardware they ran on.

Needs a box provisioned by [sparkup](../sparkup): Prometheus with the remote-write receiver, and
Grafana on `http://spark.local`.

## Watch a synthetic run

```sh
sparks demo --name acceptance
```

Prints a run id and a Grafana link. Loss, gradient norm, learning rate per parameter group,
throughput, and the GPU row underneath.

## Instrument a real one

```python
from sparks.emit import RunMetrics

with RunMetrics(run_id=..., url="http://127.0.0.1:9090", info={"model": ...}) as m:
    for step, batch in enumerate(batches):
        ...
        m.log(step=step, loss=float(loss))
```

`with` records `crashed` if the loop raises. Every push is wrapped: a metrics outage cannot kill a
training run.

## Develop

```sh
make check   # lint, mypy, tests, dashboard
make live    # the same against a real Prometheus in Docker
```
```

**Step 2: `INSTALL_CLAUDE.md`**

The agent doc: everything an agent would otherwise undo. It must contain, at minimum:

- The three corrections to `sparkup/docs/training-observability.md` from the top of this plan, with
  the evidence, so nobody re-litigates them.
- Task 2's spike answers, dated.
- The staleness rule and the three consequences.
- Why one pump thread: remote-write 1.0 rolls back the whole request on one bad sample.
- Why `status` is not a label on the info metric.
- Which venv sparks installs into on the box, and why not the other one.
- Why the dashboard lands in `/opt/monitoring/grafana/dashboards/` and what would break that
  (a basename collision with `spark-overview.json`, or a uid collision).
- That `sort: 8` is Natural DESC and is undocumented on the web.
- That annotations need credentials the box does not have, and that this is slice 2's first problem.

**Step 3: Commit**

```bash
git add README.md INSTALL_CLAUDE.md
git commit -m "docs: the README and the agent doc"
```

---

## Definition of done for slice 1

- `make check` green: lint, mypy strict, unit tests, dashboard checker.
- `make live` green against a real Prometheus.
- `sparks demo` on the box produces a run watchable in Grafana, and the run **ends** on the graph
  rather than flat-lining.
- `prometheus_api_remote_write_invalid_labels_samples_total` is zero.
- The dashboard survives `make apply` in sparkup. Verify by actually running it, not by reasoning
  about it.
- `INSTALL_CLAUDE.md` records the spike answers and the three spec corrections.

## What slice 1 deliberately does not do

- **No `sparks run -- <cmd>`.** Slice 2. It brings run directories under `/srv/bbm/runs/<id>/`,
  the NVML energy delta, the idle baseline, and `summary.json`.
- **No annotations.** They need `POST /api/annotations` and the box's Grafana is anonymous Viewer.
  Resolving that means a service account token provisioned by sparkup. First problem of slice 2.
- **No queue.** Slice 3: a spool directory under `/srv/bbm`, one systemd service under a `sparks`
  service account so exclusivity is structural, and a textfile exporter writing
  `/var/lib/node_exporter/textfile/sparks_queue.prom`. That directory is already group `bbm` and
  mode 2775, so no root is needed to write it, but the files must be `0644` and written atomically
  (`mktemp` in the same directory, then `mv`) or node_exporter skips them in silence.
- **No bbm integration.** Once the emitter is proven, wiring it into `bbm_train/train.py` is small:
  the `log: Path | None` parameter at `train.py:155` is already the seam, `begin()` goes at
  `train.py:173`, per-step `log()` at `train.py:193`, per-epoch at `train.py:199`, and `end()` at
  `run_e0.py:116` after `per_scene_onset` has run. One `run_id` per `run_e0.py` invocation with
  `arm` and `seed` as labels, because the verdict is a property of all six training runs together.
  Note that `epoch` and `step` restart six times inside one run_id, so every panel must group by
  `arm` and `seed`. Grad-norm needs adding (nothing computes it, and there is no clipping, so use
  `clip_grad_norm_(params, max_norm=float("inf"))`, which returns the norm without scaling).
  There is no LR scheduler, so learning rate is constant and belongs in `begin()` rather than as a
  series.
