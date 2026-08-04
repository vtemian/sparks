# Training observability: design handoff

**Status: not built here.** This design was researched and specified inside `sparkup`, then split
out when the scope was separated: `sparkup` provisions the box and gets metrics *into* Prometheus;
a separate project owns the training wrapper that emits per-run metrics and correlates them.

This file is the handoff. It is the research and the decisions, so the separate project starts from
a specification rather than from scratch. Nothing here is implemented.

## What the separate project has to build

A small wrapper around a training/fine-tuning round that exposes, per run:

- **training metrics**: epoch, step, loss, learning rate, grad-norm, tokens/sec, steps/sec
- **system metrics**: already provided by `sparkup` (CPU, load, unified memory, temperatures, GPU
  utilisation/power/clocks). The wrapper does not re-collect these; it correlates against them.
- **energy**: provided by `sparkup` as firmware power and energy channels on the `node` job, via
  the `spbm` driver, **on boxes that opted into it**. The wrapper turns them into per-run energy and
  cost, and must still produce a run report on a box where they are absent.

What `sparkup` guarantees it can rely on: a Prometheus at `127.0.0.1:9090` with the remote-write
receiver enabled, a Grafana at `http://spark.local` with a provisioned Prometheus datasource, and
live `node` and `gpu` scrape jobs. Power is not in that guarantee. Where it exists it arrives on
`node`, as `node_hwmon_power_watt` and `node_hwmon_energy_input_joule_total`, with no separate power
job; where `spbm_enabled` is false those series do not exist and the energy and cost figures have to
be reported as unavailable rather than as zero.

## Phase C: training observability (the k6 part)

### C0: `/srv/bbm`
`/srv/bbm/{data,checkpoints,runs}`, group `bbm`, setgid so the group shares artifacts.
**This exists because `rsync --delete` owns `~/bbm`.** Every training path points here. `/srv/bbm`
and `bbm` are this box's values of `spark_shared_dir` and `spark_shared_group`; the repo defaults
are `/srv/spark` and `spark`.

### C1: `trainobs`, the Python package
Small, dependency-light, deployed to a venv on the box and importable by `bbm`'s future `train/`
module. `sparkup` owns it because it knows the Prometheus URL; `bbm` imports it.

Dependency: `prometheus-remote-writer` (v1.1.3, Jan 2026, Apache-2.0; small enough to vendor if
abandoned; the remote-write 1.0 protocol is frozen). On aarch64, `python-snappy` 0.7+ wraps
`cramjam` which ships manylinux aarch64 wheels. **Verify a plain install before assuming**; fall
back to a ~50-line DIY protobuf+snappy writer if it fights.

`PrometheusCallback(TrainerCallback)`, structure copied from Axolotl, labels fixed:

- `on_train_begin` → the info metric plus the two timestamps that make everything else work:
  ```
  training_run_info{run_id, run_name, git_sha, model, dataset, tokenizer, status} 1
  training_run_start_timestamp_seconds{run_id}
  training_run_heartbeat_timestamp_seconds{run_id}
  ```
- `on_log` → `loss`, `learning_rate`, `grad_norm`, `epoch` from `logs`; `training_step` from
  `state.global_step`; `training_steps_per_sec` and `training_tokens_per_sec` derived from deltas
  of `global_step` / `num_input_tokens_seen` over `time.monotonic()` (needs
  `TrainingArguments(include_num_input_tokens_seen=True)`; it counts padding). Refresh the
  heartbeat every push.
- `on_train_end` → terminal status, so a dashboard distinguishes finished from crashed.
- Guard with `state.is_world_process_zero`; **wrap every push in try/except, because a metrics
  outage must never kill a training run.** Batch pushes on a ~2–5 s cadence (k6 uses 5 s).

**The heartbeat is the trick.** It refreshes while the run lives and freezes when it dies, so one
link expression covers live and finished runs: no sentinel values, no "crashed and never wrote its
end time" hole.

Cardinality: **never put `step` in a label**, because step is a gauge value and time is the
axis. Metadata lives on the info metric and joins in PromQL:
`training_loss{run_id=~"$run_id"} * on(run_id) group_left(run_name, git_sha) training_run_info`.

transformers 5.x: `TrainerCallback` signatures and `logs`/`TrainerState` fields are unchanged;
`report_to` now defaults to `"none"`; callback kwargs carry `processing_class`, not `tokenizer`.
Set `logging_steps=1` and `logging_first_step=True` for a live feel.

### C2: `sparkup-train`, the launcher
- `sparkup-train demo`: a **synthetic run** (decaying loss with noise, plausible step timing,
  realistic tokens/sec). `bbm` has no training code yet, so this is the acceptance test for the
  whole pipeline. It must be indistinguishable from a real run in Grafana.
- `sparkup-train run -- <command>`: mint a `run_id`, sample the idle power baseline (Phase D),
  export `run_id` + Prometheus URL into the environment, submit to the queue, record metadata
  under `/srv/bbm/runs/<run_id>/`, and on completion write `summary.json` and the per-run energy
  metrics.
- `run_id` format `run-YYYYmmdd-HHMM-<name>` so runs sort chronologically as strings.
- Print the deep link at launch (live form) and on completion (pinned form):
  ```
  http://spark.local/d/trainrun/training-run?orgId=1&var-run_id=<id>&from=<start_ms>&to=now&refresh=10s
  ```
  Subtract ~60 s from `start_ms` so the first datapoints are not glued to the axis. `&kiosk` must be
  bare. Optionally `POST /api/snapshots` at the end for runs worth keeping.
- **Annotations**: `POST /api/annotations` at start with tags `["training-run", "<run_id>"]`,
  `PATCH` with `timeEnd` at the end → a shaded region marking the run. Omit `dashboardUID` so the
  boundary also appears on infra dashboards, which is where you diagnose "why did throughput tank
  at 03:00". Persist the annotation id so a crash handler can still close the region.
- Keep the Trainer's own `log_history` JSON on disk. Prometheus is the live view; disk is the archive.

### C3: the "Training Runs" dashboard
Clone the k6 structure (dashboard 19665 defines `testid` as `label_values(...)`, `multi: true`,
every panel filtering on it):

- Variable `run_id` = `label_values(training_run_info, run_id)`. Anchoring on the info metric is far
  cheaper than scanning every series. `multi: true`, `includeAll: false`, `refresh: 2`
  (re-query on time-range change), **`sort: 8`** (natural descending → newest first; 7/8 exist in
  the schema though the docs list only six).
- Panels: loss, lr, grad-norm, tokens/sec, steps/sec, legend `{{run_id}}`, matcher `=~` (multi-select
  interpolates to a regex). Stats for current step / max steps and latest loss.
- **GPU util, power, unified memory and wall power on the same dashboard.** This is the payoff of
  using the infra Grafana instead of a separate tracker: training curves next to the hardware.
- Annotation query filtered `["training-run", "$run_id"]`, `matchAny: false`. For compare mode add
  a second query with `matchAny: true` and just `["training-run"]`, since ANDing multiple run tags
  matches nothing.

### C4: runs index
A separate `/d/runs` dashboard: one Table panel, three **instant** queries in Table format
(`training_run_info`, `training_run_start_timestamp_seconds * 1000`,
`training_run_heartbeat_timestamp_seconds * 1000`) joined by `run_id` (outer), with an override on
the `run_id` field carrying two data links:

```
Follow live:       /d/trainrun/training-run?var-run_id=${__data.fields["run_id"]}&from=${__data.fields["start_ms"]}&to=now&refresh=10s
Full run (pinned): /d/trainrun/training-run?var-run_id=${__data.fields["run_id"]}&from=${__data.fields["start_ms"]}&to=${__data.fields["end_ms"]}
```

Add energy, duration and cost columns from Phase D. This is the experiment index, ~40 lines of JSON.
`${__data.fields["<name>"]}` pulls another column on the same row; timestamps must be **ms**, hence
the `* 1000` in PromQL rather than a transformation.

### C5: run comparison, honestly scoped
Multi-selecting `$run_id` gives wall-clock side-by-side, which is all the official k6 dashboards
offer and enough for most needs. **A step-aligned overlay (loss-vs-step superimposed) is not
natural in Prometheus**; the axis is wall-clock. Options in order of sanity: (1) accept
side-by-side, start here; (2) `$run_a`/`$run_b` with PromQL `offset`, clunky; (3) the Comparison
Panel plugin; (4) push a rebased twin series with `out_of_order_time_window` set generously.

**If step-aligned overlay becomes daily bread rather than a nice-to-have, that is the one genuine
argument for a real experiment tracker instead of bending Prometheus.** Say so out loud rather than
building option 4 by default. Grafana's ceiling here is "watch a run, compare a few, correlate with
infra and power", a real and valuable ceiling; know where it is. If this ever grows run-comparison
tables, hyperparameter diffing and artifact links, we have reinvented MLflow badly.

### C6: bbm-specific metrics (after stage 3 exists)
`verifier_pass_rate` and per-check failure counts from `bbm.verify.Report.failures`, **the stage-6
GRPO reward signal**, so watching it live is watching the reward. Plus corpus composition from
`stats.as_dict()`, draw-channel PAD fraction per batch (measured baseline 31.3% across the seven
fixtures, per-scene idle 21–48%), and the `stroke` degradation rate once stage 5 runs;
`bbm/PROMPT.md` calls that "the metric that matters".

### D2: the per-run energy series

Written by the launcher at run end, one sample per run, cheap to keep forever:

```
training_run_energy_wh{run_id}            total wall energy over the run   (plug)
training_run_energy_marginal_wh{run_id}   energy attributable to the run   (plug − idle)
training_run_gpu_energy_wh{run_id}        GPU-rail energy                  (NVML counter, exact)
training_run_idle_baseline_watts{run_id}  measured immediately before the run
training_run_duration_seconds{run_id}
training_run_cost{run_id}                 marginal Wh × tariff
```

`training_run_gpu_energy_wh` comes from `nvmlDeviceGetTotalEnergyConsumption` sampled at run start
and end, **verified working on this box** (10024 mJ / 3 s → 3.34 W, matching `PowerUsage` 3.38 W).
It is millijoules and **resets on driver reload**, so read it as a delta and discard the run's
figure if the counter went backwards. `nvidia-ml-py` is the client; it is now installed in
`~/bbm-train/.venv`.

The ratio `gpu_energy_wh / energy_wh` is the useful derived number: how much of what you paid for
was the GPU actually working, rather than the box merely being switched on. Under sustained load the
published wall-to-rail gap is around 2×, so **0.5 is roughly the ceiling, not the expectation**;
the ratio falls steeply as utilisation drops, because the rail collapses to a few watts while the
rest of the box does not. If a run scores far below 0.5, the bottleneck is not the GPU and more
epochs will mostly buy electricity.

**Two numbers, both wanted.** *Total* answers "what did this cost me". *Marginal* answers "was this
experiment worth it", because the box draws power whether or not you train. Measure the baseline
for ~60 s immediately before each run rather than assuming a global constant; it drifts with ambient
temperature and whatever else is running.

**Whole-box energy is a gauge integral; only package energy has a counter.** This is the one place
the firmware source is weaker than a smart plug, and it is worth knowing before you write the panel.
`spbm` exposes 14 power channels (including `sys_total`, the whole box) but only **4** energy
accumulators, and they are `pkg`, `cpu_e`, `cpu_p` and `gpu`. There is no `sys_total` energy
counter. So:

```promql
# whole-box Wh over the run window: integrate the sys_total gauge
avg_over_time(
  (node_hwmon_power_watt * on(chip, sensor) group_left(label)
   node_hwmon_sensor_label{label="sys_total"})[$__range:]
) * $__range_s / 3600

# marginal: subtract the idle baseline over the same duration
# (the above) - (avg_over_time(training_run_idle_baseline_watts[$__range]) * $__range_s / 3600)

# cost, tariff as a Grafana constant variable in currency per kWh
# (the above) / 1000 * $tariff
```

The integral's error scales with the scrape interval, but at 15 s over a run measured in hours it is
well under a percent: adequate, and not worth owning a smart plug to improve. Where an exact figure
matters and package scope is acceptable, the counter is there and is exact:

```promql
increase(node_hwmon_energy_input_joule_total{...label="pkg"}[$__range]) / 3600   # Wh, not J
```

Note the units: hwmon energy is **joules**, so divide by 3600 for watt-hours. The old Shelly series
was already in watt-hours; anything ported from it needs this conversion.

`sys_total` is DC-side and excludes PSU conversion loss, so it reads under a wall-socket meter.
Cost computed from it is a slight underestimate; state that in the panel description rather than
letting a reader assume it is the number on their electricity bill.

**Tariff is a Grafana variable, not a hardcoded number**: it changes, and a variable means no
dashboard rebuild. At a Romanian tariff around 1.3 RON/kWh a continuous 200 W box is on the order
of €0.06/hour. **The interesting number is probably watt-hours per run, for comparing
efficiency between configs, more than the euros.**

**Clock discipline** is free now: the sensors are read on the host and scraped by a Prometheus on
the same host, so there is only one clock. The plug this replaced had its own, and it was not to be
trusted.

### D3: cost panels
Add energy, duration and cost columns to the runs index (C4), and a per-run stat row. The
comparison that justifies this whole phase is **final loss per watt-hour** across configs; put it
on the dashboard explicitly rather than leaving it as arithmetic for the reader.

