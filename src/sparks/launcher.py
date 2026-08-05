"""Wrap a training command: measure what it cost, record how it ended.

The order of operations here is load-bearing and is explained inline. The
short version: sample the idle baseline before doing anything expensive, take
the wall clock as close to the child as possible, and treat the moment the
child is reaped as the end of the run.
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sparks import energy, index, shared, summary
from sparks.emit import RunMetrics
from sparks.energy import (
    SOURCES_DISAGREE,
    SOURCES_UNMEASURED,
    EnergyReading,
    Sampler,
)
from sparks.process import Supervisor
from sparks.run import current_user, git_sha

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
    sampler: Sampler | None = None,
) -> Launched:
    """Run `command` as a training run and leave a permanent record of it.

    `url` is the Prometheus to push to, or None to record to disk only, which
    is what the unit tests use and what a box without monitoring gets.

    `sampler` is injectable so a test can drive real energy arithmetic against a
    fake sysfs tree: on a development machine `Sampler.detect()` reads zeros and
    every energy assertion is 0.0 == 0.0, which let three mutations survive.
    """
    # Sanitise the durable copies at this one boundary. The child is still
    # exec'd with the ORIGINAL argv below, which round-trips through execve
    # correctly; only what we persist and push is cleaned, so a non-UTF-8
    # --name can neither poison the shared index nor crash the wrapper.
    user = shared.clean(current_user())
    name = shared.clean(name, "run")
    command_record = [shared.clean(arg, "", limit=4000) for arg in command]

    # mkdir raising EEXIST is the only atomic uniqueness guarantee, and the
    # chmod inside heals a tree left group-unreadable by an earlier umask.
    run_id, run_dir = shared.reserve_run_dir(shared_dir / "runs", name, user)

    if sampler is None:
        sampler = Sampler.detect()
    # Before anything expensive, and read as a counter delta from the same
    # counters the run is measured against. The GPU rail during this window is
    # what tells a quiet box from a contended one whose baseline is not ours.
    base = sampler.baseline(baseline_seconds)
    # Monotonic, not time.time(): NTP can step the wall clock mid-run, the exact
    # hazard duration_seconds already avoids. The window brackets the whole run.
    energy_read_at = time.monotonic()
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
        _record_failed_launch(run_dir, run_id, name, user, command_record, str(e))
        if metrics is not None:
            metrics.end("crashed")
        _rebuild(shared_dir)
        return Launched(run_id, "crashed", 127, run_dir)

    # The child is reaped. From here to the end everything runs under one
    # try/finally so that the record cannot be lost: a full disk, a quota, or
    # the other user owning the directory must not stop metrics.end(status)
    # firing, or training_run_info (LIFECYCLE-exempt from stale marking) sits on
    # the dashboard forever with no end and no status.
    status = completed.outcome.status
    try:
        # The counters bracket a wider window than the child ran for: the end
        # read happens after the group sweep and the tee drain, measured at up
        # to 10s. Subtracting the idle baseline over the child's duration
        # instead would bill that cleanup to the run's marginal energy: on a
        # 1.5s run with 5s of cleanup, at 13W, that is 65J of pure idle reported
        # as marginal, which is more than 100% wrong.
        # Monotonic, and the max() is now dead code: the window opened before
        # the child and closes after it is reaped, so it always brackets the
        # duration. Kept as a cheap guard against a misbehaving clock.
        measured_seconds = max(
            completed.duration_seconds, time.monotonic() - energy_read_at
        )
        # A delta needs BOTH endpoints. Coalescing a failed read to 0.0 made a
        # glitched start read report the entire accumulator as this run's
        # energy, and a backwards delta means the counter reset mid-run; both
        # are non-measurements, and energy.delta returns None for them.
        reading = EnergyReading(
            total_joules=energy.delta(energy_start, sampler.total_joules()),
            gpu_nvml_joules=energy.delta(gpu_nvml_start, sampler.gpu_nvml_joules()),
            gpu_firmware_joules=energy.delta(
                gpu_firmware_start, sampler.gpu_firmware_joules()
            ),
            idle_watts=base.idle_watts,
            gpu_idle_watts=base.gpu_watts,
            seconds=measured_seconds,
        )

        record = summary.Summary(
            run_id=run_id,
            run_name=name,
            user=user,
            git_sha=git_sha(),
            command=command_record,
            started_unix=completed.started_unix,
            ended_unix=completed.ended_unix,
            duration_seconds=completed.duration_seconds,
            status=status,
            exit_code=completed.outcome.exit_code,
            signal=completed.outcome.signal_name,
            escalated_to_sigkill=completed.outcome.escalated_to_sigkill,
            energy=summary.Energy(
                total_joules=reading.total_joules,
                marginal_joules=reading.marginal_joules,
                gpu_nvml_joules=reading.gpu_nvml_joules,
                gpu_firmware_joules=reading.gpu_firmware_joules,
                idle_watts=reading.idle_watts,
                idle_gpu_watts=reading.gpu_idle_watts,
                window_seconds=reading.seconds,
                baseline_seconds=baseline_seconds,
                gpu_sources=reading.gpu_sources,
            ),
        )
        summary.save(record, run_dir)

        if reading.gpu_sources == SOURCES_DISAGREE:
            # The two GPU counters are out of their usual ~1.22 relationship,
            # which means one reset mid-run. The energy figure is not
            # trustworthy, and saying so is the point of measuring twice.
            LOG.warning(
                "sparks: GPU energy sources disagree (nvml %.0fJ, firmware "
                "%.0fJ); one counter probably reset mid-run",
                reading.gpu_nvml_joules,
                reading.gpu_firmware_joules,
            )
    except Exception as e:
        # The child completed and its exit code is faithful; losing the record
        # must neither strand a phantom on the dashboard nor lie about $?.
        LOG.error("sparks: could not record %s: %s", run_id, e)
    finally:
        if metrics is not None:
            metrics.end(status)
        _rebuild(shared_dir)

    return Launched(run_id, status, completed.outcome.wrapper_exit, run_dir)


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


def _rebuild(shared_dir: Path) -> None:
    """Refresh the run index. Never raises: a corrupt summary somewhere else
    must not surface as this run having failed."""
    target = textfile_dir(shared_dir) / index.FILENAME
    try:
        shared.make_dir(target.parent)
        written = index.rebuild(shared_dir / "runs", target)
        LOG.info("sparks: %d runs in %s", written, target)
    except Exception as e:  # deliberately broad: the run itself still succeeded
        LOG.warning("sparks: could not rebuild the run index: %s", e)


def textfile_dir(shared_dir: Path) -> Path:
    """Where node_exporter reads .prom files from.

    Overridable because it is a property of the box, not of this repo, and the
    default is node_exporter's own. Writing the index anywhere else means it is
    never scraped and `count(sparks_run_info)` stays empty. The
    SparksRunIndexEmpty rule in alerts/sparks.yml would catch that, but nothing
    loads that file yet: it is a specification, not a live alert (see its
    header). The autouse fixture in tests/conftest.py overrides this so the unit
    suite never rewrites the box's real index.
    """
    override = os.environ.get("SPARKS_TEXTFILE_DIR")
    if override:
        return Path(override)
    default = Path("/var/lib/node_exporter/textfile")
    return default if default.is_dir() else shared_dir / "index"


def _record_failed_launch(
    run_dir: Path,
    run_id: str,
    name: str,
    user: str,
    command: list[str],
    error: str,
) -> None:
    now = time.time()
    summary.save(
        summary.Summary(
            run_id=run_id,
            run_name=name,
            user=user,
            git_sha=git_sha(),
            command=list(command),
            started_unix=now,
            ended_unix=now,
            duration_seconds=0.0,
            status="crashed",
            exit_code=127,
            signal=None,
            escalated_to_sigkill=False,
            # The command never ran, so nothing was measured: every counter is
            # unknown (None, not 0.0, which would claim a measured zero) and the
            # sources are unmeasured, not "agree", the old hard-coded lie.
            energy=summary.Energy(
                total_joules=None,
                marginal_joules=None,
                gpu_nvml_joules=None,
                gpu_firmware_joules=None,
                idle_watts=0.0,
                idle_gpu_watts=0.0,
                window_seconds=0.0,
                baseline_seconds=0.0,
                gpu_sources=SOURCES_UNMEASURED,
            ),
        ),
        run_dir,
    )
    # FileNotFoundError's message carries the bad byte straight from argv, so
    # write_text(error) would raise UnicodeEncodeError inside launch()'s except
    # and escape with a traceback. clean() is the same one-boundary fix.
    (run_dir / "error.txt").write_text(shared.clean(error, "", limit=4000) + "\n")
