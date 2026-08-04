"""Every metric this package emits, and what it means.

The dashboard checker reads this to refuse a panel querying something nothing
emits. Adding a metric means adding it here first.

`status` is deliberately NOT a label on `training_run_info`. Changing a label on
an info metric creates a second series, remote-written series are never marked
stale automatically, and the join every panel uses then fails with `found
duplicate series for the match group` and the panel goes red rather than
degrading. Terminal state lives on its own metrics instead.
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
