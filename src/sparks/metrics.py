"""Every metric this package emits, and what it means.

The dashboard checker reads this to refuse a panel querying something nothing
emits. Adding a metric means adding it here first.

`status` is deliberately NOT a label on `training_run_info`. Changing a label on
an info metric creates a second series, remote-written series are never marked
stale automatically, and the join every panel uses then fails with `found
duplicate series for the match group` and the panel goes red rather than
degrading. Terminal state lives on its own metrics instead.

`training_run_info` carries immutable labels only, re-pushed every cycle so the
join's right side never falls out of the 5 minute lookback window.

`training_run_active` is 1 while the run is going, and deliberately NOT in
`LIFECYCLE` below, so it is stale-marked at `end()`. This is what a Grafana
annotation queries: the info metric is exempt from stale marking to keep panel
joins resolvable, and so persists for the whole 5m lookback. Measured on a 48s
run, the info series ended 293s past the end while stale-marked series ended
12s before it, so an annotation on info draws a region 7x the run.

Held-out evaluation (`training_eval_loss`) is emitted once per epoch rather
than per step.

The `sparks_run_*` family is the permanent run index, written to the
node_exporter textfile collector rather than pushed. A `.prom` file is
re-scraped for as long as it exists, so these never age out of retention the
way pushed series do.

The `sparks_queue_*` family is the live queue, rewritten every runner tick.
Separate from the run index because these series come and go and those never
do.
"""

METRICS: dict[str, str] = {
    "training_run_info": "1, carrying the run's immutable metadata as labels",
    "training_run_start_timestamp_seconds": "unix seconds, written once at start",
    "training_run_heartbeat_timestamp_seconds": "unix seconds, refreshed every flush",
    "training_run_end_timestamp_seconds": "unix seconds, written once at the end",
    "training_run_status": "1, labelled finished or crashed, written once at the end",
    "training_run_active": "1 while the run is running, ended by a stale marker",
    "training_progress": "fraction of the whole job complete, 0-1",
    "training_eta_seconds": "estimated seconds remaining until the job finishes",
    "training_epoch": "fractional epoch",
    "training_step": "optimizer steps since this run began",
    "training_loss": "training loss for the last batch",
    "training_grad_norm": "gradient L2 norm, labelled by parameter group",
    "training_learning_rate": "learning rate, labelled by parameter group",
    "training_steps_per_sec": "optimizer steps per second over the last window",
    "training_tokens_per_sec": "tokens per second over the last window",
    "training_eval_loss": "held-out loss, labelled by head",
    "sparks_run_info": "1, identity + energy_sources of a completed run, as labels",
    "sparks_run_start_timestamp_seconds": "unix seconds the run started",
    "sparks_run_duration_seconds": "wall-clock duration",
    "sparks_run_energy_window_seconds": "window the energy counters bracketed",
    "sparks_run_energy_joules": "total energy drawn over the run",
    "sparks_run_marginal_energy_joules": "energy above idle, absent when unknown",
    "sparks_run_idle_watts": "idle baseline power, 0 when none was measured",
    "sparks_run_gpu_nvml_energy_joules": "GPU energy per NVML",
    "sparks_run_gpu_firmware_energy_joules": "GPU energy per the firmware counter",
    "sparks_run_final_loss": "final training loss, absent when the run produced none",
    "sparks_queue_job_info": "1, carrying a queued job's identity and state as labels",
    "sparks_queue_depth": "jobs in each state, published at 0 for the live states",
    "sparks_queue_job_submitted_timestamp_seconds": "unix seconds it was submitted",
    "sparks_queue_job_started_timestamp_seconds": "unix seconds its container started",
    "sparks_queue_runner_heartbeat_timestamp_seconds": (
        "unix seconds the runner last completed a pass, the proof it is alive"
    ),
}

LIFECYCLE = frozenset(
    {
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_heartbeat_timestamp_seconds",
        "training_run_end_timestamp_seconds",
        "training_run_status",
    }
)
"""The run's record of itself, which is never marked stale on shutdown.

Everything else is the live view and should stop dead when the run ends, so a
finished run does not hold its last loss on the graph for the lookback window.
These five are the opposite: they say the run happened and how it ended, and
staling them erases that. `training_run_status` in particular is written and
then immediately staled if this distinction is not made, so it never resolves
at all - which is exactly the bug the live test caught.
"""
