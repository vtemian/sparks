# Operating sparks

This repo's facts, decisions and traps, for an agent working in it. Humans want
[README.md](README.md).

**Scope.** sparks owns training runs: emitting their metrics, launching them, and queueing them.
[sparkup](https://github.com/vtemian/sparkup) owns the box (including the local image registry) and
gets system metrics into Prometheus, and its `INSTALL_CLAUDE.md` says explicitly that it does not
own training runs. Do not push work across that line in either direction.

**Two products: client and server.**

| Install | Binary | Role |
|---|---|---|
| Laptop venv | `sparks` | client: build/push image, upload `--data`, enqueue, queue/cancel/abort/remove |
| Queue image | `fire` | server: drain the spool, pull image, start job, honour cancel/abort |

Queue control from the laptop SSHes host `fire-ctl` (sparkup installs it), which `docker exec`s
into the queue container and runs `fire <verb>`; bulk `--data` still rsyncs over SSH.

Supervision of each training container is private: `python -m sparks.fire.supervise` inside the
image (called by `fire`), not a console script on PATH. There is no `sparks-run`, `sparks-runner`,
laptop `sparks run`, or `sparks demo`. Images are built on the laptop and pushed to the box
registry; `fire` only pulls. Job data is one folder via `--data`, mounted at `/data`
(`$SPARKS_DATA`) in the container — training code must read that path. The box does **not** build
from a shipped `context/` directory.

---

## Verified against a real Prometheus

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

## Design decisions that supersede the original handoff

The sparkup-era design research is gone from this tree. Three of its claims were wrong and still
shape the code; do not reintroduce them.

**1. No `PrometheusCallback(TrainerCallback)`.** The loop this was built for is hand written, having
rejected HuggingFace `Trainer` over four measured problems with it under a PEFT wrapper. Hence
`RunMetrics` with `begin()`/`log()`/`end()`. A `TrainerCallback` adapter is a 20-line shim if some
project ever wants one; do not invert this.

Axolotl's `OpenTelemetryMetricsCallback` is a **scrape exporter, not a push client**: no batching,
no flush interval, and **no labels at all**, so two runs on one host produce indistinguishable
series. The batching design here follows k6's `internal/output/prometheusrw/` instead: 5s push
interval, buffer plus periodic flusher, millisecond truncation with a seen-set, stale markers on
shutdown. Exactly one thing is copied from Axolotl: wrapping every push in a broad `try/except`
that only logs.

**2. `node_hwmon_energy_input_joule_total` does not exist.** The series is
`node_hwmon_energy_joule_total`. Confirmed live: 4 counters, `pkg`, `cpu_e`, `cpu_p`, `gpu`. Same
class of bug as sparkup's commit `9d731f8` ("Fix the power metric name"), made again for energy.

**3. `status` must never be a label on `training_run_info`.** Changing a label on an info metric
creates a **second series**, pushed series are never automatically staled, and the join every panel
uses then fails with `found duplicate series for the match group` and the panel goes **fully red**
rather than degrading. Terminal state lives on `training_run_end_timestamp_seconds` and
`training_run_status`.

## Traps

- **Laptop Docker needs `insecure-registries` for `registry_url`.** The registry is plain HTTP on
  purpose (LAN trust = ssh trust). Without e.g. `{"insecure-registries": ["spark.local:5000"]}` in
  the laptop's `daemon.json` (then restart Docker), `docker push` fails and submit dies before the
  job is reserved. Match the host:port in `/etc/sparks/box.toml`'s `registry_url`.
- **`--data` is required; train against `/data` or `$SPARKS_DATA`.** The client uploads that folder
  into the job and the runner mounts it read-only at `/data`. A script that hard-codes a laptop
  corpus path will fail on the box even though submit succeeded.
- **The box never builds a job image.** `job.image` is required; `fire` pulls it. Shipping
  project `context/` for `docker build` on the box is gone. To change code, rebuild and submit from
  a laptop (or pass `--image` to reuse a tag already in the registry).
- **Every deliberate broad `except` needs `# noqa: BLE001` AND a reason.** `BLE` is in the `select`
  list, so an unmarked `except Exception` fails `ruff check`; `RUF100` fails the opposite mistake, a
  directive on a clause that does not need one. Ruff already treats a handler as non-blind when it
  re-raises, calls `LOG.exception`, or passes `exc_info`, so **four** sites are clean *without* a
  directive and must stay that way: `runner.py:98` and `launch.py:93` (`LOG.exception`),
  `emit.py:238` (`LOG.warning(..., exc_info=True)`), and `summary.py:201`, which is an
  `except BaseException:` with a bare `raise`. Adding a directive to any of them is the `RUF100`
  failure.
- **Ruff's statement counting is not lines**, so a one-line change can cost three: a docstring is
  one statement, an `except` handler is two, a `finally` is two. `contain.main` sits at exactly 30
  against the 30 cap, and `Supervisor.run` at 27, so adding narration to either forces something
  else out. Reach for a wider cap before an extraction: these are the functions with the most
  sequencing to protect, and the last time the cap won, splitting `contain.main` pushed its
  signal-handler state into module globals and left a test-only hook in production code.
- **Write the reason with ` -- `, not a colon.** `# noqa: BLE001: reason` is a malformed code list:
  that one directive suppresses nothing, so its violation comes back. Ruff is loud about it, on
  stderr and with a non-zero exit (`warning: Invalid # noqa directive ... expected code to consist
  of uppercase letters followed by digits`), and other directives in the same file keep working.
  The one thing it will not tell you is a malformed directive on a line that has no violation:
  `RUF100` only sees directives that parsed, so that one is dead weight nothing flags.
- **`prometheus_remote_writer` ships no `py.typed`**, so its import carries
  `# type: ignore[import-untyped]`. Do not relax `[tool.mypy]` to avoid this.
- **A test using `autostart=False` cannot exercise `_shutdown`.** `_stop` is None and the method
  returns on its first line, so the test passes having run nothing. `test_shutdown_is_idempotent`
  uses `autostart=True` deliberately; do not "simplify" it back.
- **The dashboard checker's metric extractor is the second version on purpose.** A single
  "identifier followed by something" regex misses the metric inside
  `max by (...) (training_run_info)`, which is the shape every joined panel uses, so it silently
  skips the interesting half. Two tests pin this.
- **The dashboard variable uses `sort: 4` (numerical DESC), not `sort: 8`.** An earlier note here
  claimed `sort: 8` was Natural DESC, "verified from `VariableSort` in `types.gen.ts` at v13.1.1".
  That was read rather than run, and it is wrong. Observed in a real Grafana 13.1.1 testing every
  value 0-8: sort values **5, 6, 7 and 8 are silently ignored** (byte-identical to `sort: 0`, raw
  datasource order). The `@grafana/scenes` `sortVariableValues` switch does have arms for 5-8, which
  is what the earlier agent read, but they never fire at runtime. `sort: 8` therefore auto-selected
  the oldest run in the window, which with the default `now-3h` had already scrolled out of range,
  leaving every training panel "No data". Use **`sort: 4`**: it keys on the leading number, so it
  auto-selects the newest run and also puts `run-10` above `run-9`. Do not "restore" `sort: 8`.
- **Panels match with `=~`, never `=`.** A multi-select variable interpolates to `(a|b)`, so `=`
  silently matches nothing the moment a second run is ticked.
- **The info join wraps its right side in `max by (...)`.** Two `training_run_info` series sharing a
  `run_id` is a hard query error, not a degraded panel.
- **The `$run_id` dropdown is scoped to the dashboard's time range**, and the variable's `refresh: 2`
  re-queries when that range changes. With the default `now-3h` it lists recent runs only. Widening
  the range is what surfaces old ones. This is not the retention change failing.

## Paths, and why none of them are literals here

sparks is not tied to any one training project. Every path below comes from
sparkup's **`spark_shared_dir`**, whose repo default is `/srv/spark` and which a box may override in
its untracked `host_vars`. Write `$SPARKS_SHARED_DIR` in this repo, never a literal: baking one
project's name into a framework meant to serve several is the mistake this section exists to prevent.

**The box says where those paths are; sparks does not guess.** sparkup's `sparks` role writes
`/etc/sparks/box.toml` with the shared directory, textfile directory, Prometheus URL, Grafana URL
and `registry_url`. `fire` and `python -m sparks.fire.supervise` read it on the box; without that
file (or with a promised path missing) they **refuse to start**, exiting **78** (`EX_CONFIG`,
distinct so a queue can tell a misconfigured box from a crashed job). Explicit `--shared-dir` and
`--url` still override it for tests and non-sparkup machines.

Laptops never load the contract locally for the happy path: they set `SPARKS_HOST` (or `--host`)
and SSH `fire-ctl` on the box for queue verbs and `fire-ctl contract` for `registry_url` when
building and pushing.

This replaced two guesses, both of which lost data quietly. `--shared-dir` used to default to
`/srv/spark`, which is *this repo's* default and not the box's — the box overrides it — so omitting
the flag recorded runs into a directory nobody reads. And the textfile directory used to fall back to
`$SPARKS_SHARED_DIR/index` when node_exporter's own was missing, which nothing scrapes. Both looked
like success. A run that cannot be recorded properly now refuses to start instead.

`make deploy` reads three values, defaults in the Makefile, overrides in an untracked `local.mk`
(copy `local.mk.example`). That is the same tracked-defaults plus untracked-identity split sparkup
uses, and for the same reason.

- `SPARKS_HOST` defaults to `spark.local`, so it uses your own SSH login. Nobody else's belongs in a
  tracked file. The laptop client uses the same variable to reach the queue.
- `SPARKS_SHARED_DIR` defaults to `/srv/spark`, matching sparkup's default rather than any box.
- `SPARKS_VENV` has **no** default and `make deploy` refuses without it. It is the venv your training
  code runs in, which belongs to a project this one does not know about. Guessing would be worse than
  failing.

The image registry itself is sparkup's: converge with the `registry` role so `registry_url` names a
service that exists. `make deploy` here does not start it. **Converge sparkup (registry +
`registry_url` in box.toml) before deploying this sparks.** An old contract without `registry_url`
makes every client and runner refuse with `MalformedError`. Drain the queue first: `job.json` files
written before `image` was required are skipped by `entries()` rather than failed cleanly.

## The seam into Grafana

- **Dashboards go to `$SPARKS_SHARED_DIR/dashboards/`.** sparkup creates it at 2775, group
  `spark_shared_group`, and bind-mounts it read-only into Grafana, so installing one needs no root and
  no restart: Grafana rescans on a 10s timer.

  Do **not** also leave a copy in `/opt/monitoring/grafana/dashboards/`. Both directories are mounted
  under the same provider path (`/etc/grafana/dashboards/spark` and `.../sparks`), and the provider
  walks recursively, so two files with one uid is a duplicate-uid conflict and Grafana drops one.
- **sparkup's compose mounts the two dashboard directories as siblings, never nested.** Mounting one
  inside the other fails at container init: runc has to create the mountpoint and the read-only
  parent refuses the write. This was found by trying it, and the naive version would have broken
  `make apply` on the box rather than just CI.
- Retention is **1y**, which is what makes the `$run_id` variable a permanent experiment index.
  Recheck `prometheus_tsdb_head_series` against free space before growing the exporter set.
- Install sparks into the **training** venv, not a library venv that a lockfile owns. A venv rebuilt
  by `uv sync --frozen` drops anything added by hand, silently, at the next sync.

## What is still open

The queue, the laptop client, and private supervision under `fire` are in. Still missing relative to
earlier slice notes: Grafana annotations and snapshots (need a Grafana service account token —
anonymous **Viewer** cannot POST), and the overview-table / energy caveats listed under
"Deferred" below.

**The alerts are evaluated but not routed.** `alerts/sparks.yml` is loaded on a provisioned box:
sparkup's `sparks` role vendors it, validates it with `promtool` from the pinned Prometheus image,
and installs it into a `rule_files:` directory. Their state shows in Prometheus's `/rules` and is
queryable as `ALERTS{alertname="..."}`. There is still **no Alertmanager**, so nothing pages, emails
or notifies — "firing" means a series says so and somebody has to look.

Two consequences worth keeping in mind. The file the box loads is sparkup's **copy**, so editing
`alerts/sparks.yml` here changes nothing until it is copied to `roles/sparks/files/sparks.yml` and
the play is re-run; provisioning deliberately does not reach a git remote. And the `CAVEAT` comments
still apply to what the rules *mean*: `SparksRunIndexEmpty` cannot tell a fresh box that has never
run anything from a box whose index broke, so do not treat it as a page even once routing exists.

**The overview table must come from the textfile collector, not remote-write.** A `.prom` file in
`/var/lib/node_exporter/textfile` is re-scraped every 15s for as long as it exists, so those series
never go stale and never age out, and the files on disk stay the source of truth even if the TSDB is
lost. Write one aggregated `sparks_runs.prom` rebuilt from `$SPARKS_SHARED_DIR/runs/*/summary.json`, not one
file per run: the collector reads every file on every scrape. Files must be `0644` and written
atomically (`mktemp` in the same directory, then `mv`) or node_exporter skips them in silence.

## Settled twice. Do not re-open.

**There is no dashboard per experiment.** One `training-runs` board with a `$run_id` variable.
Killed on Grafana's own best-practice guidance and because no maintained project generates one per
run; raised a second time and killed again on a stronger argument: a generated
per-run dashboard queries live Prometheus, so it is an empty grid of panels once its data ages out.
It looks like a permanent record and is not one. Raising retention is what actually makes old
experiments viewable.

**k6 does not do this either**, despite being the stated model: dashboard 19665 defines `testid` as
`label_values(...)` with `multi: true` and filters every panel on it.

If permanence beyond retention is wanted, the answer is `POST /api/snapshots` at run end, which
freezes a board **with its data** into a URL that never queries Prometheus again.

## Findings from the slice-1 code review, and what they changed

All six were real. Two were mutation-proven before and after the fix.

- **`_shutdown` used to flush without checking the join result**, so a Prometheus that accepts the
  connection and never answers (compaction, memory pressure, the other person's job) put a second
  writer on the wire: measured at 12 concurrent `send()` calls, with `end()` blocking for 56s.
  That breaks the single-writer invariant the whole design exists to enforce. `_shutdown` now
  returns without flushing if the pump is still alive. Losing the terminal samples is the lesser
  harm, and the frozen heartbeat still says the run stopped.
  `tests/test_concurrency.py` pins it against a real stalling HTTP server; with the check removed
  that test fails and takes 63s.
- **`_mark_stale` picked its own timestamp**, and the measured margin against the last real sample
  was 2 to 7 ms. A collision is `duplicate sample for timestamp`, a 400, and the rollback means *no*
  series gets a marker and every one of them flat-lines. It now uses `max(now, last + 1)` per series,
  which is why `Buffer.seen()` returns timestamps rather than just names.
- **The pump loop had no exception guard.** Only `send()` was wrapped, so an `InvalidLabelError` from
  `_beat` killed telemetry for the rest of the run behind a bare `threading.excepthook`, with
  `_stop` never set so nothing could detect it. The loop body is guarded now, and `RunMetrics.__init__`
  builds both Series eagerly so a bad label fails on the caller's thread instead.
- **The first `test_the_run_record_is_never_marked_stale` was vacuous**: it re-implemented
  `_mark_stale`'s filter in the test and asserted its own arithmetic, and still passed with the
  exemption deleted from the implementation. `_stale_batch()` was split out so the test can assert
  on the batch that is actually sent.
- **The dashboard checker had two bypasses.** `{__name__="fake_metric"}` names a metric from inside
  the label set, which was stripped before being read, so such a panel passed with an empty result
  set. And `expressions()` walked only one level, so a panel inside a *collapsed* row was invisible
  (Grafana nests the children in the row panel on collapse, which is one UI round-trip away). It now
  reads `__name__` matchers, recurses, and checks the templating variable's own query.
- **`tests/test_metrics.py` was in the plan and never written.** `METRICS` is the checker's
  allowlist, so an invalid name in it makes a metric dashboardable and unemittable.

Known and accepted: `SCRAPED_PREFIXES` means the checker's guarantee covers `training_*` only.
`node_hwmon_energy_input_joule_total`, the typo this project has now made twice, passes it. Checking
the scraped half needs sparkup's exporter defaults, which is its checker's job, not this one's.

## The supervisor and the child write disjoint series, enforced in code

`python -m sparks.fire.supervise` is the supervisor (nested by `fire` around each job container) and
holds a `RunMetrics` with `lifecycle=True`; the training child gets its own via `emit.from_env(...)`,
which sets `lifecycle=False`. They must never write the same series. Two writers choose timestamps
independently, and remote-write 1.0 rejects an out-of-order sample by rolling back the **entire
request**, so the batch carrying the loss simply never lands.

A review caught this happening for real. The launcher pushed stale markers for the child's series
while the pump thread was still running, and the resulting out-of-order sample destroyed the batch
holding `training_run_info`, both timestamps and `training_run_status`. `summary.json` said
`crashed` and Prometheus held **nothing at all** for the run. `finished` runs were unaffected, so the
failure was confined to crashed, killed, oom and cancelled: precisely the runs the wrapper exists to
report on.

Two things came out of that and must not be undone:

- **There is no `send_now`.** There is no safe moment for a second writer, so the API does not offer
  one. The only send path is the pump, plus `_mark_stale` after the pump has stopped.
- **`_refuse_if_not_ours` is asymmetric on purpose.** A child may not write `LIFECYCLE` or
  `training_run_active`. The supervisor side is deliberately unconstrained, because a standalone
  `RunMetrics` (no child emitter) legitimately owns both halves; making the guard symmetric breaks
  that.

`tests/test_live.py::test_a_crashed_run_still_lands_its_whole_record` is the regression test, and it
is the only kind that could have caught this. Reintroducing the bug makes it fail with
`status=400, body=out of order sample`. No unit test caught it or could have.

**The parent cannot stale the child's series on its behalf.** It was tried and removed: the parent
builds markers from its own label set, and a child that passes labels (`from_env(arm="real")`, or
anything using `log_group`, which always adds `group`) has a different label set and therefore a
different series. The parent would write markers to series nobody ever wrote while the child's real
curves flat-lined for the lookback window. Doing this properly needs the child to publish its label
set somewhere the parent can read; until something needs it, a killed run's curves flat-line for five
minutes and that is the honest behaviour.

## Deferred in slice 2, with reasons

- **`oom` is unreachable.** `Supervisor` accepts a `cgroup=` and promotes `killed` to `oom` on a
  positive `memory.events.local` delta, and `tests/test_process.py` proves the mechanism, but the
  launcher never creates a cgroup and never passes one. Wiring it needs
  `systemd-run --user --scope -p Delegate=yes`. The box supports it: systemd 255, `systemd-oomd`
  inactive, `Delegate=yes`, `systemd-run --user --scope` works. Until then every OOM reads as
  `killed`, which is honest but coarse.
- **`final_loss` is always absent**, so the overview column is empty. The supervisor has no channel
  to learn the child's last loss; the child would have to write it into the run directory on the way
  out.
- **`total_joules` still has no cross-check**, though it can now say so. The GPU pair catches a
  counter reset through `gpu_sources`; a `pkg` reset or a failed read has nothing to compare against.
  It is at least no longer reported as `0.0`: an unmeasurable endpoint yields `null`, so a real zero
  and an unknown are distinguishable and `absent()` works. What is missing is a second whole-box
  source to disagree with.
- **The index lands in `SPARKS_TEXTFILE_DIR`, or the `textfile_dir` the box declared in
  `/etc/sparks/box.toml`.** There is no fallback any more: `sparks.fire.supervise` checks that
  directory is writable before starting anything and refuses with exit 78 if it is not, because the
  old fallback (`<shared>/index`) was never scraped and left `count(sparks_run_info)` empty while
  looking healthy. A library caller that bypasses the CLI still only gets a logged warning, since a
  failed index must not fail a training run that otherwise succeeded.

## Energy under real load, which corrects the design research

Everything the design research said about power was extrapolated, because no GPU-saturating run had
ever coincided with spbm hwmon coverage. One finally did, and the extrapolation was low.

```
                        research said        measured under real load
sys_total, loaded       ~95 W (extrapolated)   125.7 W   (max seen 166.6 W)
sys_total, max ever      42.4 W                166.6 W
gpu rail                 ~73 W (extrapolated)   73.7 W
NVML draw                 60 W                  61.6 W
gpu / sys_total          "0.5 is the ceiling"    0.586
firmware gpu / NVML       1.22                   1.195
```

Three things follow.

- **The "0.5 ceiling" on `gpu_energy / total_energy` is wrong.** Measured 0.586 under load, so a
  heuristic like "below 0.5 means the GPU is not the bottleneck" would misread a genuinely
  GPU-bound run. The research predicted this would break and it did.
- **The 22.5% NVML-versus-firmware gap holds under real load** (1.195 against 1.22 at idle), which
  is the strongest evidence yet that it is a measurement-boundary difference rather than noise.
  Keep reporting both, labelled.
- **Cost is still small but not as small as the plan said.** At 166.6 W and 1.3 RON/kWh a
  continuously saturating box is 0.217 RON/hour, so a 40-minute run is about 0.14 RON. The plan's
  "2 to 9 bani" was based on a 42 W ceiling that no longer holds. Watt-hours per run between configs
  remains the number worth reading; the currency column remains decoration.

## Verified on the box

The acceptance matrix, run against real hardware with a training job in flight:

```
t1  true                                    finished   exit=0    signal=None     $?=0
t2  sh -c 'exit 3'                          crashed    exit=3    signal=None     $?=3
t4  trap "exit 0" TERM; sleep 30            cancelled  exit=0    signal=SIGTERM  $?=143
    (the WRAPPER signalled, child exited 0 under its own power)
```

t4 is the row that matters, and both `exit_code` and `signal` are set at once. All three runs landed
`training_run_info`, `training_run_status` and `training_run_end_timestamp_seconds`; before the
second-writer fix, the non-`finished` ones held nothing at all. `training_run_active` returns zero
series once the runs end, so the annotation region is exact.
`prometheus_api_remote_write_invalid_labels_samples_total` and `prometheus_tsdb_out_of_order_samples_total`
are both 0.

The index writes `0644`, ends with a newline, and passes `promtool check metrics`.

**`python -m sparks.fire.supervise` takes `--url` / `--shared-dir` as its own flags** (private
module invoked by `fire`, not a `sparks` subcommand or product binary). The laptop client is only
`sparks submit|queue|cancel|abort|retry|remove`.
