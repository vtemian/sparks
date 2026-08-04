"""Wrap a training command: measure what it cost, record how it ended.

The order of operations here is load-bearing and is explained inline. The
short version: sample the idle baseline before doing anything expensive, take
the wall clock as close to the child as possible, and treat the moment the
child is reaped as the end of the run.
"""

import getpass
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sparks import index, summary
from sparks.emit import RunMetrics
from sparks.energy import EnergyReading, Sampler
from sparks.metrics import LIFECYCLE, METRICS
from sparks.process import Supervisor
from sparks.run import git_sha, new_run_id

LOG = logging.getLogger("sparks")

BASELINE_SECONDS = 60.0
"""Long enough to average out ~1.9 W of jitter, short enough nobody minds.

Sampled in-process at 1 Hz rather than read back from Prometheus: at this
window length the 15 s scrape integral was measured up to 7% wrong.
"""


@dataclass(frozen=True)
class Launched:
    run_id: str
    status: str
    wrapper_exit: int
    run_dir: Path


def launch(
    command: list[str],
    name: str,
    shared_dir: Path,
    url: str | None,
    baseline_seconds: float = BASELINE_SECONDS,
) -> Launched:
    """Run `command` as a training run and leave a permanent record of it.

    `url` is the Prometheus to push to, or None to record to disk only, which
    is what the unit tests use and what a box without monitoring gets.
    """
    run_id = new_run_id(name)
    run_dir = shared_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    sampler = Sampler.detect()
    # Before anything expensive. A 1 Hz sampling loop alone was measured
    # inflating an idle reading by 6%, so the launcher must not pay for its own
    # startup out of the run's marginal energy.
    idle_watts = sampler.baseline_watts(baseline_seconds)
    energy_start = sampler.total_joules()
    gpu_nvml_start = sampler.gpu_nvml_joules()
    gpu_firmware_start = sampler.gpu_firmware_joules()

    metrics = _supervisor_metrics(run_id, name, url)
    if metrics is not None:
        metrics.begin()

    env = {"SPARKS_RUN_ID": run_id, "PYTHONUNBUFFERED": "1"}
    if url:
        env["SPARKS_PROMETHEUS_URL"] = url

    completed = Supervisor(command, log_path=run_dir / "output.log", env=env).run()

    reading = EnergyReading(
        total_joules=max(0.0, sampler.total_joules() - energy_start),
        gpu_nvml_joules=max(0.0, sampler.gpu_nvml_joules() - gpu_nvml_start),
        gpu_firmware_joules=max(
            0.0, sampler.gpu_firmware_joules() - gpu_firmware_start
        ),
        idle_watts=idle_watts,
        seconds=completed.duration_seconds,
    )

    record = summary.Summary(
        run_id=run_id,
        run_name=name,
        user=_user(),
        git_sha=git_sha(),
        command=list(command),
        started_unix=completed.started_unix,
        ended_unix=completed.ended_unix,
        duration_seconds=completed.duration_seconds,
        status=completed.outcome.status,
        exit_code=completed.outcome.exit_code,
        signal=completed.outcome.signal_name,
        escalated_to_sigkill=completed.outcome.escalated_to_sigkill,
        energy=summary.Energy(
            total_joules=reading.total_joules,
            marginal_joules=reading.marginal_joules,
            gpu_nvml_joules=reading.gpu_nvml_joules,
            gpu_firmware_joules=reading.gpu_firmware_joules,
            idle_watts=reading.idle_watts,
            sources_agree=reading.sources_agree,
        ),
    )
    summary.save(record, run_dir)

    if metrics is not None:
        if record.status != "finished":
            # The child died without flushing, so it never ended its own
            # curves. Without this its loss flat-lines for the whole lookback
            # window instead of stopping where the run stopped.
            _stale_the_childs_series(metrics)
        metrics.end(record.status)

    index_dir = shared_dir / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    written = index.rebuild(shared_dir / "runs", index_dir / index.FILENAME)
    LOG.info("sparks: %d runs in the index", written)

    return Launched(run_id, record.status, completed.outcome.wrapper_exit, run_dir)


def _supervisor_metrics(run_id: str, name: str, url: str | None) -> RunMetrics | None:
    """The supervisor's emitter, which owns only metrics.LIFECYCLE.

    The child holds its own via `emit.from_env` and owns everything else. Two
    writers on one series interleave timestamps, which is a 400 that rolls back
    the whole request.
    """
    if not url:
        return None
    return RunMetrics(
        run_id=run_id,
        url=url,
        info={"run_name": name, "git_sha": git_sha()},
    )


def _stale_the_childs_series(metrics: RunMetrics) -> None:
    orphaned = [n for n in METRICS if n.startswith("training_") and n not in LIFECYCLE]
    batch = metrics.stale_batch_for(orphaned)
    if batch:
        metrics.send_now(batch)


def _user() -> str:
    """Who to ask about this run. Never raises: getpass consults the password
    database and then the environment, and both can be absent in a container."""
    try:
        return getpass.getuser()
    except Exception:  # deliberately broad: a missing account is not a failure
        return os.environ.get("USER", "unknown")
