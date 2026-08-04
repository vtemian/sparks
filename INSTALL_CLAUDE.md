# Operating sparks

This repo's facts, decisions and traps, for an agent working in it. Humans want
[README.md](README.md).

**Scope.** sparks owns training runs: emitting their metrics, launching them, and queueing them.
[sparkup](https://github.com/vtemian/sparkup) owns the box and gets system metrics into Prometheus,
and its `INSTALL_CLAUDE.md` says explicitly that it does not own training runs. Do not push work
across that line in either direction.

[docs/training-observability.md](docs/training-observability.md) is the original design research.
It used to live in sparkup and was deleted there on 2026-08-04 ("docs: drop the training-observability
handoff") because it specifies this project. **Three of its claims are wrong; they are corrected
below and in the plan. Read those corrections before implementing anything from it.**

---

## Verified against a real Prometheus, 2026-08-04

Run with `prom/prometheus:v3.13.2`, remote-write receiver on, via `tests/harness-up.sh`. These four
answers are the reason the design has the shape it does. Re-run the spike rather than trusting this
list if you are changing the emitter's threading or shutdown.

- **Every `RemoteWriter` kwarg the code passes is accepted by 1.1.3**: `timeout`, `retries`,
  `backoff_factor`, `sort_labels`, `strict_timestamps`, `auto_convert_seconds_to_ms`.
- **The stale marker works.** Pushing `0x7FF0000000000002` as a value makes the series vanish from
  an instant query immediately. The NaN payload survives Python's protobuf encoder. This was the one
  assumption the design research flagged as unverified, and it holds.
- **An out-of-order sample rolls back the whole request.** A batch of two series, one with a
  timestamp 3 hours old, returned `status=400, body=out of bounds`, and the *other* series in the
  same batch was absent afterwards. This is the empirical reason exactly one thread may ever call
  `send()`.
- A normal push round-trips and is queryable within a second.

## Why one pump thread

Remote-write 1.0 has no partial write. `storage/remote/write_handler.go` returns on the first bad
sample and its `defer` calls `app.Rollback()`, so one bad sample discards every series in the
request, as measured above. A second writer's request can therefore silently destroy the first one's
batch. `RunMetrics._pump` is the only caller of `send()`, and it does the heartbeat and the flush
together for that reason. Do not split them into two threads.

## The staleness rule

Prometheus does **not** mark pushed series stale. Staleness markers are injected by the scrape loop
and the rule evaluator, and the remote-write receiver has neither. A series that stops being pushed
returns its **last value, frozen**, for `--query.lookback-delta` (default 5m), then vanishes from
instant queries entirely. Not zero. Absent.

Three consequences, all load-bearing:

- `_beat()` re-pushes `training_run_info` every cycle. Pushing it once at start means the join's
  right side is empty five minutes later and every decorated panel returns nothing mid-run.
- Any query that must see a finished run needs `last_over_time(...[30d])`. It is one of the few
  functions that preserves `__name__`.
- Clean shutdown pushes explicit stale markers, so a finished run stops dead on the graph.

## `metrics.LIFECYCLE` exists because of a bug a real Prometheus caught

`end()` writes `training_run_end_timestamp_seconds` and `training_run_status`, then `_shutdown()`
flushes and calls `_mark_stale()`. `Buffer.drain()` records every drained series in `_last`, and
`seen()` reads `_last`, so the terminal samples were being marked stale roughly a millisecond after
being written and **`training_run_status` never resolved at all**.

The fix is `metrics.LIFECYCLE`, the five metrics `_mark_stale` skips. The distinction is real, not a
workaround: the lifecycle metrics are the run's permanent record of itself and how it ended, while
everything else is the live view that should stop when the run does.

No unit test caught this, and no unit test could have: it only appears when a real receiver is asked
what it actually holds. `tests/test_live.py` is not optional coverage.

## Three corrections to `docs/training-observability.md`

**1. C1's `PrometheusCallback(TrainerCallback)` has nothing to attach to.** `bbm/bbm_train/train.py`
opens by rejecting HuggingFace `Trainer` with four measured reasons, and `train_arm()` is a hand
written loop. Hence `RunMetrics` with `begin()`/`log()`/`end()`. A `TrainerCallback` adapter is a
20-line shim if some project ever wants one; do not invert this.

The doc also says the structure is copied from Axolotl. Axolotl's `OpenTelemetryMetricsCallback` is
a **scrape exporter, not a push client**: no batching, no flush interval, and **no labels at all**,
so two runs on one host produce indistinguishable series. The batching design here follows k6's
`internal/output/prometheusrw/` instead: 5s push interval, buffer plus periodic flusher, millisecond
truncation with a seen-set, stale markers on shutdown. Exactly one thing is copied from Axolotl:
wrapping every push in a broad `try/except` that only logs.

**2. `node_hwmon_energy_input_joule_total` does not exist.** The series is
`node_hwmon_energy_joule_total`. Confirmed live: 4 counters, `pkg`, `cpu_e`, `cpu_p`, `gpu`. The
doc's D2 PromQL returns nothing as written. Same class of bug as sparkup's commit `9d731f8` ("Fix
the power metric name"), made again for energy.

**3. `status` must never be a label on `training_run_info`.** The doc specifies it and then flips it
at `on_train_end`. Changing a label on an info metric creates a **second series**, pushed series are
never automatically staled, and the join every panel uses then fails with `found duplicate series for
the match group` and the panel goes **fully red** rather than degrading. Terminal state lives on
`training_run_end_timestamp_seconds` and `training_run_status`.

## Traps

- **`random.Random((seed, step))` raises.** A tuple is not a supported seed type. `demo.curve` mixes
  the two into one int.
- **`# noqa: BLE001` fails `ruff check` here.** `BLE` is not in the `select` list, so `RUF100` flags
  the directive as unused. The broad `except` clauses carry plain comments instead.
- **`prometheus_remote_writer` ships no `py.typed`**, so its import carries
  `# type: ignore[import-untyped]`. Do not relax `[tool.mypy]` to avoid this.
- **A test using `autostart=False` cannot exercise `_shutdown`.** `_stop` is None and the method
  returns on its first line, so the test passes having run nothing. `test_shutdown_is_idempotent`
  uses `autostart=True` deliberately; do not "simplify" it back.
- **The dashboard checker's metric extractor is the second version on purpose.** A single
  "identifier followed by something" regex misses the metric inside
  `max by (...) (training_run_info)`, which is the shape every joined panel uses, so it silently
  skips the interesting half. Two tests pin this.
- **`sort: 8` in the dashboard variable is Natural DESC** and is real, though the Grafana web docs
  list no numeric sort values at all. It is verified from `VariableSort` in `types.gen.ts` at
  v13.1.1. Alphabetical would put `run-9` above `run-10`.
- **Panels match with `=~`, never `=`.** A multi-select variable interpolates to `(a|b)`, so `=`
  silently matches nothing the moment a second run is ticked.
- **The info join wraps its right side in `max by (...)`.** Two `training_run_info` series sharing a
  `run_id` is a hard query error, not a degraded panel.
- **The `$run_id` dropdown is scoped to the dashboard's time range**, and the variable's `refresh: 2`
  re-queries when that range changes. With the default `now-3h` it lists recent runs only. Widening
  the range is what surfaces old ones. This is not the retention change failing.

## The box

- Dashboards go to `/srv/bbm/dashboards/`, which sparkup's `feat/shared-dashboard-dir` branch creates
  at 2775 group `bbm` and bind-mounts into Grafana. No root. Grafana rescans on a 10s timer.
  **Until that branch lands**, the fallback is `sudo install` into `/opt/monitoring/grafana/dashboards/`,
  which is root:root 0755.
- sparks installs into **`~/bbm-train/.venv`** on the box, never `~/bbm/.venv`. The latter is built
  from bbm's `uv.lock` by `bbm/scripts/spark.sh` with `uv sync --frozen`, which drops anything added
  by hand, and it carries the Pillow pin the cross-platform determinism story rests on.
- `bbm`'s training loop has **never been run**: no `out/`, no `*.jsonl`, no `verdict.json` anywhere
  in that repo or its history. Every timing estimate for it is from a plan document, not a
  measurement.

## What is not built yet

Slice 2: `sparks run -- <cmd>`, run directories under `/srv/bbm/runs/<id>/`, the NVML energy delta
and idle baseline, `summary.json`, Grafana annotations, and the `sparks-overview` table. Annotations
and snapshots both need a Grafana service account token, because the box's Grafana is anonymous
**Viewer** and cannot POST.

Slice 3: the queue. A spool directory under `/srv/bbm`, one systemd service under a service account
so exclusivity is structural, and a textfile exporter. Exclusivity is what makes marginal energy
attribution mean anything.

**The overview table must come from the textfile collector, not remote-write.** A `.prom` file in
`/var/lib/node_exporter/textfile` is re-scraped every 15s for as long as it exists, so those series
never go stale and never age out, and the files on disk stay the source of truth even if the TSDB is
lost. Write one aggregated `sparks_runs.prom` rebuilt from `/srv/bbm/runs/*/summary.json`, not one
file per run: the collector reads every file on every scrape. Files must be `0644` and written
atomically (`mktemp` in the same directory, then `mv`) or node_exporter skips them in silence.

## Settled twice. Do not re-open.

**There is no dashboard per experiment.** One `training-runs` board with a `$run_id` variable.
Killed 2026-07-31 on Grafana's own best-practice guidance and because no maintained project
generates one per run; raised again and killed again 2026-08-04 on a stronger argument: a generated
per-run dashboard queries live Prometheus, so it is an empty grid of panels once its data ages out.
It looks like a permanent record and is not one. Raising retention is what actually makes old
experiments viewable.

**k6 does not do this either**, despite being the stated model: dashboard 19665 defines `testid` as
`label_values(...)` with `multi: true` and filters every panel on it.

If permanence beyond retention is wanted, the answer is `POST /api/snapshots` at run end, which
freezes a board **with its data** into a URL that never queries Prometheus again.
