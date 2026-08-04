"""Wrap a training command: measure what it cost, record how it ended.

The order of operations here is load-bearing and is explained inline. The
short version: sample the idle baseline before doing anything expensive, take
the wall clock as close to the child as possible, and treat the moment the
child is reaped as the end of the run.
"""

import getpass
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sparks import index, summary
from sparks.emit import RunMetrics
from sparks.energy import EnergyReading, Sampler
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
    energy_read_at = time.time()
    energy_start = sampler.total_joules()
    gpu_nvml_start = sampler.gpu_nvml_joules()
    gpu_firmware_start = sampler.gpu_firmware_joules()

    metrics = _supervisor_metrics(run_id, name, url)
    if metrics is not None:
        metrics.begin()

    env = {"SPARKS_RUN_ID": run_id, "PYTHONUNBUFFERED": "1"}
    if url:
        env["SPARKS_PROMETHEUS_URL"] = url

    try:
        completed = Supervisor(command, log_path=run_dir / "output.log", env=env).run()
    except Exception as e:
        # A command that does not exist, or a failure inside the supervisor.
        # Without this the run has no summary.json, no index row, and a
        # training_run_info already pushed that is LIFECYCLE-exempt and so
        # never staled: a permanent phantom run on the dashboard that never
        # ends and never gets a status.
        LOG.error("sparks: could not run %s: %s", command, e)
        _record_failed_launch(run_dir, run_id, name, command, str(e))
        if metrics is not None:
            metrics.end("crashed")
        _rebuild(shared_dir)
        return Launched(run_id, "crashed", 127, run_dir)

    # The counters bracket a wider window than the child ran for: the end read
    # happens after the group sweep and the tee drain, which the plan measured
    # at up to 10s. Subtracting the idle baseline over the child's duration
    # instead would bill that cleanup to the run's marginal energy: on a 1.5s
    # run with 5s of cleanup, at 13W, that is 65J of pure idle reported as
    # marginal, which is more than 100% wrong.
    measured_seconds = max(completed.duration_seconds, time.time() - energy_read_at)
    reading = EnergyReading(
        total_joules=max(0.0, sampler.total_joules() - energy_start),
        gpu_nvml_joules=max(0.0, sampler.gpu_nvml_joules() - gpu_nvml_start),
        gpu_firmware_joules=max(
            0.0, sampler.gpu_firmware_joules() - gpu_firmware_start
        ),
        idle_watts=idle_watts,
        seconds=measured_seconds,
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
        metrics.end(record.status)

    if not reading.sources_agree:
        # The two GPU counters are out of their usual ~1.22 relationship, which
        # means one of them reset mid-run. The energy figure for this run is
        # not trustworthy, and saying so is the whole point of measuring twice.
        LOG.warning(
            "sparks: GPU energy sources disagree (nvml %.0fJ, firmware %.0fJ); "
            "one counter probably reset mid-run",
            reading.gpu_nvml_joules,
            reading.gpu_firmware_joules,
        )

    _rebuild(shared_dir)
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


def _user() -> str:
    """Who to ask about this run. Never raises: getpass consults the password
    database and then the environment, and both can be absent in a container."""
    try:
        return getpass.getuser()
    except Exception:  # deliberately broad: a missing account is not a failure
        return os.environ.get("USER", "unknown")


def _rebuild(shared_dir: Path) -> None:
    """Refresh the run index. Never raises: a corrupt summary somewhere else
    must not surface as this run having failed."""
    target = textfile_dir(shared_dir) / index.FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = index.rebuild(shared_dir / "runs", target)
        LOG.info("sparks: %d runs in %s", written, target)
    except Exception as e:  # deliberately broad: the run itself still succeeded
        LOG.warning("sparks: could not rebuild the run index: %s", e)


def textfile_dir(shared_dir: Path) -> Path:
    """Where node_exporter reads .prom files from.

    Overridable because it is a property of the box, not of this repo, and the
    default is node_exporter's own. Writing the index anywhere else means it is
    never scraped, `count(sparks_run_info)` stays empty and the
    SparksRunIndexEmpty alert fires forever.
    """
    override = os.environ.get("SPARKS_TEXTFILE_DIR")
    if override:
        return Path(override)
    default = Path("/var/lib/node_exporter/textfile")
    return default if default.is_dir() else shared_dir / "index"


def _record_failed_launch(
    run_dir: Path, run_id: str, name: str, command: list[str], error: str
) -> None:
    now = time.time()
    summary.save(
        summary.Summary(
            run_id=run_id,
            run_name=name,
            user=_user(),
            git_sha=git_sha(),
            command=list(command),
            started_unix=now,
            ended_unix=now,
            duration_seconds=0.0,
            status="crashed",
            exit_code=127,
            signal=None,
            escalated_to_sigkill=False,
            energy=summary.Energy(0.0, 0.0, 0.0, 0.0, 0.0, True),
        ),
        run_dir,
    )
    (run_dir / "error.txt").write_text(error + "\n")
