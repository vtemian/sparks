METRICS: dict[str, str] = {
    "training_run_info": "1, carrying the run's immutable metadata as labels",
    "training_run_start_timestamp_seconds": "unix seconds, written once at start",
    "training_run_heartbeat_timestamp_seconds": "unix seconds, refreshed every flush",
    "training_run_end_timestamp_seconds": "unix seconds, written once at the end",
    "training_run_status": (
        "1, labelled with one of summary.STATUSES, written once at the end"
    ),
    "training_run_active": "1 while the run is running, ended by a stale marker",
    "training_progress": "fraction of the whole job complete, 0-1",
    "training_eta_seconds": "estimated seconds remaining until the job finishes",
    "training_epoch": "fractional epoch",
    "training_step": "optimizer steps since this run began",
    "training_loss": "training loss for the last batch",
    "training_grad_norm": "gradient L2 norm; a mapping gives one series per group",
    "training_learning_rate": "learning rate; a mapping gives one series per group",
    "training_steps_per_sec": "optimizer steps per second over the last window",
    "training_tokens_per_sec": "tokens per second over the last window",
    "training_eval_loss": "held-out loss; a mapping gives one series per group",
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
