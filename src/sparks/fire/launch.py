"""Wrap a training command: measure what it cost, record how it ended.

The order of operations here is load-bearing and is explained inline. The
short version: sample the idle baseline before doing anything expensive, take
the wall clock as close to the child as possible, and treat the moment the
child is reaped as the end of the run.
"""

import contextlib
import logging
import signal
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from sparks import box, energy, index, shared, summary
from sparks.emit import RunMetrics
from sparks.energy import (
    SOURCES_DISAGREE,
    SOURCES_UNMEASURED,
    EnergyReading,
    Sampler,
)
from sparks.fire.process import Completed, Supervisor
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
    on_reserved: Callable[[str, Path], None] | None = None,
    sha: str | None = None,
) -> Launched:
    """Run `command` as a training run and leave a permanent record of it.

    `url` is the Prometheus to push to, or None to record to disk only, which
    is what the unit tests use and what a box without monitoring gets.

    `sampler` is injectable so a test can drive real energy arithmetic against a
    fake sysfs tree: on a development machine `Sampler.detect()` reads zeros and
    every energy assertion is 0.0 == 0.0, which let three mutations survive.

    `on_reserved` is called with the run id as soon as it exists, which is
    before the baseline sampling and long before this returns. A supervisor of
    this process cannot otherwise learn the id until the run is over - the id is
    printed at the end - and "which run is this job" is a question worth
    answering while it is still running.

    `sha` overrides the commit recorded for the run. It exists because the
    default - `git_sha()` of the working directory - is right for a person
    running this in their checkout and WRONG for the queue, which runs it from
    the framework's own directory and would otherwise record sparks' commit as
    though it were the training code's.
    """
    rec, run_id, run_dir = _reserved(command, name, shared_dir, on_reserved, sha)
    started = _started(run_dir, run_id, rec, shared_dir, sampler, baseline_seconds)
    if isinstance(started, Launched):
        return started
    metrics, completed = _supervised(command, run_dir, run_id, rec, shared_dir, url)
    if isinstance(completed, Launched):
        return completed

    # The child is reaped. From here to the end everything runs under one
    # try/finally so that the record cannot be lost: a full disk, a quota, or
    # the other user owning the directory must not stop metrics.end(status)
    # firing, or training_run_info (LIFECYCLE-exempt from stale marking) sits on
    # the dashboard forever with no end and no status.
    status = completed.outcome.status
    try:
        _record(
            run_dir,
            run_id,
            rec,
            completed,
            _close_window(*started, completed, baseline_seconds),
        )
    except Exception:
        # The child completed and its exit code is faithful; losing the record
        # must neither strand a phantom on the dashboard nor lie about $?.
        LOG.exception("sparks: could not record %s", run_id)
    finally:
        _end(metrics, status)
        _rebuild(shared_dir)

    return Launched(run_id, status, completed.outcome.wrapper_exit, run_dir)


@dataclass(frozen=True)
class _Record:
    """The durable copies of who ran what: cleaned once, persisted everywhere."""

    user: str
    name: str
    sha: str
    command: list[str]


def _cleaned(command: list[str], name: str, user: str, sha: str | None) -> _Record:
    """Sanitise the durable copies at this one boundary. The child is still
    exec'd with the ORIGINAL argv, which round-trips through execve correctly;
    only what we persist and push is cleaned, so a non-UTF-8 --name can neither
    poison the shared index nor crash the wrapper.
    """
    return _Record(
        user=shared.clean(user),
        name=shared.clean(name, "run"),
        sha=git_sha() if sha is None else shared.clean(sha, "unknown"),
        command=[shared.clean(arg, "", limit=4000) for arg in command],
    )


def _announce(
    on_reserved: Callable[[str, Path], None] | None, run_id: str, run_dir: Path
) -> None:
    if on_reserved is None:
        return
    # Never allowed to sink the run: whoever wanted to be told has a worse
    # view of the world if this fails, but the run itself is unaffected.
    try:
        on_reserved(run_id, run_dir)
    except Exception as e:  # noqa: BLE001 — an observer's failure is not the run's
        LOG.warning("sparks: could not announce %s: %s", run_id, e)


def _reserved(
    command: list[str],
    name: str,
    shared_dir: Path,
    on_reserved: Callable[[str, Path], None] | None,
    sha: str | None,
) -> tuple[_Record, str, Path]:
    """The run's durable identity: cleaned record, reserved directory, id."""
    rec = _cleaned(command, name, current_user(), sha)
    # mkdir raising EEXIST is the only atomic uniqueness guarantee, and the
    # chmod inside heals a tree left group-unreadable by an earlier umask.
    run_id, run_dir = shared.reserve_dir(shared_dir / "runs", rec.name, rec.user)
    _announce(on_reserved, run_id, run_dir)
    return rec, run_id, run_dir


@dataclass(frozen=True)
class _Window:
    """The measurement window at the moment it opened.

    Every field is the opening value of the identically named `EnergyReading`
    field: the counters are the raw readings their deltas will close, `seconds`
    is the monotonic instant the window opened, and the idle rates pass through
    unchanged. Identical names mean pairing a start with the wrong end read
    cannot happen silently.
    """

    total_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    gpu_idle_watts: float
    seconds: float


def _open_window(
    sampler: Sampler | None, baseline_seconds: float
) -> tuple[_Window, Sampler]:
    """Sample the baseline and take the start reads, interruptible throughout.

    Returns the sampler alongside the window: when none was injected the one
    detected here must also take the end reads, or the delta's two endpoints
    come from different counters.
    """
    if sampler is None:
        sampler = Sampler.detect()
    with _interruptible():
        # Before anything expensive, and read as a counter delta from the
        # same counters the run is measured against. The GPU rail during
        # this window is what tells a quiet box from a contended one whose
        # baseline is not ours.
        base = sampler.baseline(baseline_seconds)
        # Monotonic, not time.time(): NTP can step the wall clock mid-run,
        # the exact hazard duration_seconds already avoids. The window
        # brackets the whole run.
        opened_at = time.monotonic()
        return _Window(
            total_joules=sampler.total_joules(),
            gpu_nvml_joules=sampler.gpu_nvml_joules(),
            gpu_firmware_joules=sampler.gpu_firmware_joules(),
            idle_watts=base.idle_watts,
            gpu_idle_watts=base.gpu_watts,
            seconds=opened_at,
        ), sampler


def _started(
    run_dir: Path,
    run_id: str,
    rec: _Record,
    shared_dir: Path,
    sampler: Sampler | None,
    baseline_seconds: float,
) -> tuple[_Window, Sampler] | Launched:
    """The opened measurement window, or the run recorded as cancelled.

    The minute before the child exists is the one window this wrapper does not
    otherwise defend. `Supervisor` installs its handlers when it runs, so a
    SIGTERM arriving during the baseline takes the default disposition and
    kills the wrapper outright -- leaving a reserved run directory with no
    summary, no index row and no explanation. A queued job aborted 25 seconds
    after it started did exactly that on the box.
    """
    try:
        return _open_window(sampler, baseline_seconds)
    except _AbortError as e:
        return _cancelled(run_dir, run_id, rec, shared_dir, baseline_seconds, e)


def _cancelled(
    run_dir: Path,
    run_id: str,
    rec: _Record,
    shared_dir: Path,
    baseline_seconds: float,
    abort: "_AbortError",
) -> Launched:
    LOG.warning("sparks: %s before the run started; recording it stopped", abort)
    _record_failed_launch(
        run_dir,
        run_id,
        rec,
        f"stopped during the {baseline_seconds:.0f}s baseline",
        status="cancelled",
        # Nothing ran, so there is no exit code to report. `cancelled` with
        # the signal that caused it is the whole truth about this run.
        exit_code=None,
        signal_name=str(abort),
    )
    _rebuild(shared_dir)
    return Launched(run_id, "cancelled", 128 + abort.signum, run_dir)


def _begin(metrics: RunMetrics | None) -> None:
    """None is a box without monitoring, and the test lives here rather than
    behind a do-nothing RunMetrics: a constructed emitter is a second writer
    on the run's series (see `_supervisor_metrics`)."""
    if metrics is not None:
        metrics.begin()


def _end(metrics: RunMetrics | None, status: str) -> None:
    """The end-of-run twin of `_begin`, safe on a box without monitoring."""
    if metrics is not None:
        metrics.end(status)


def _child_env(run_id: str, url: str | None) -> dict[str, str]:
    env = {"SPARKS_RUN_ID": run_id, "PYTHONUNBUFFERED": "1"}
    if url:
        env["SPARKS_PROMETHEUS_URL"] = url
    return env


def _supervised(
    command: list[str],
    run_dir: Path,
    run_id: str,
    rec: _Record,
    shared_dir: Path,
    url: str | None,
) -> tuple[RunMetrics | None, Completed | Launched]:
    """The supervisor's emitter and how the run ended: its `Completed`, or the
    crashed record when the child could not run at all."""
    metrics = _supervisor_metrics(run_id, rec.name, url, rec.sha)
    _begin(metrics)
    try:
        return metrics, Supervisor(
            command, log_path=run_dir / "output.log", env=_child_env(run_id, url)
        ).run()
    except Exception as e:  # noqa: BLE001 — any failure to run still owes a record
        return metrics, _crashed(run_dir, run_id, rec, shared_dir, metrics, command, e)


def _crashed(
    run_dir: Path,
    run_id: str,
    rec: _Record,
    shared_dir: Path,
    metrics: RunMetrics | None,
    command: list[str],
    error: Exception,
) -> Launched:
    """A command that does not exist, or a failure inside the supervisor.

    Without this the run has no summary.json, no index row, and a
    training_run_info already pushed that is LIFECYCLE-exempt and so never
    staled: a permanent phantom run on the dashboard that never ends and never
    gets a status.
    """
    LOG.exception("sparks: could not run %s", command)
    _record_failed_launch(run_dir, run_id, rec, str(error))
    _end(metrics, "crashed")
    _rebuild(shared_dir)
    return Launched(run_id, "crashed", 127, run_dir)


def _close_window(
    window: _Window, sampler: Sampler, completed: Completed, baseline_seconds: float
) -> summary.Energy:
    """The window's end reads, folded with its start into the run's energy."""
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
        completed.duration_seconds, time.monotonic() - window.seconds
    )
    # A delta needs BOTH endpoints. Coalescing a failed read to 0.0 made a
    # glitched start read report the entire accumulator as this run's
    # energy, and a backwards delta means the counter reset mid-run; both
    # are non-measurements, and energy.delta returns None for them.
    reading = EnergyReading(
        total_joules=energy.delta(window.total_joules, sampler.total_joules()),
        gpu_nvml_joules=energy.delta(window.gpu_nvml_joules, sampler.gpu_nvml_joules()),
        gpu_firmware_joules=energy.delta(
            window.gpu_firmware_joules, sampler.gpu_firmware_joules()
        ),
        idle_watts=window.idle_watts,
        gpu_idle_watts=window.gpu_idle_watts,
        seconds=measured_seconds,
    )
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
    return summary.Energy(
        total_joules=reading.total_joules,
        marginal_joules=reading.marginal_joules,
        gpu_nvml_joules=reading.gpu_nvml_joules,
        gpu_firmware_joules=reading.gpu_firmware_joules,
        idle_watts=reading.idle_watts,
        idle_gpu_watts=reading.gpu_idle_watts,
        window_seconds=reading.seconds,
        baseline_seconds=baseline_seconds,
        gpu_sources=reading.gpu_sources,
    )


def _record(
    run_dir: Path,
    run_id: str,
    rec: _Record,
    completed: Completed,
    energy: summary.Energy,
) -> None:
    record = summary.Summary(
        run_id=run_id,
        run_name=rec.name,
        user=rec.user,
        git_sha=rec.sha,
        command=rec.command,
        started_unix=completed.started_unix,
        ended_unix=completed.ended_unix,
        duration_seconds=completed.duration_seconds,
        status=completed.outcome.status,
        exit_code=completed.outcome.exit_code,
        signal=completed.outcome.signal_name,
        escalated_to_sigkill=completed.outcome.escalated_to_sigkill,
        energy=energy,
    )
    summary.save(record, run_dir)


def _supervisor_metrics(
    run_id: str, name: str, url: str | None, sha: str
) -> RunMetrics | None:
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
        info={"run_name": name, "git_sha": sha},
    )


def _rebuild(shared_dir: Path) -> None:
    """Refresh the run index. Never raises: a corrupt summary somewhere else, or
    a box with no textfile directory to publish into, must not surface as this
    run having failed.

    The CLI has already refused to start on an unprovisioned box, so reaching
    the warning below means either a library caller that never asked for a
    contract, or provisioning that changed mid-run.
    """
    try:
        target = box.textfile_dir() / index.FILENAME
        shared.make_dir(target.parent)
        written = index.rebuild(shared_dir / "runs", target)
        LOG.info("sparks: %d runs in %s", written, target)
    except Exception as e:  # noqa: BLE001 — the run itself still succeeded
        LOG.warning("sparks: could not rebuild the run index: %s", e)


class _AbortError(Exception):
    """A stop signal arrived before there was a child to forward it to."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signal.Signals(signum).name)


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """Turn SIGINT and SIGTERM into an exception for the duration.

    Raising from a handler is safe here and nowhere near `Supervisor`: there is
    no child to orphan, nothing to reap, and the main thread is asleep in the
    baseline. It is the opposite choice from `process.py`'s handler, which must
    never raise, and the difference is exactly that one has a child and this
    does not.

    Handlers are restored on the way out, so `Supervisor` installs its own over
    a clean slate.
    """

    def stop(signum: int, _frame: object) -> None:
        raise _AbortError(signum)

    previous = {
        signum: signal.signal(signum, stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _record_failed_launch(
    run_dir: Path,
    run_id: str,
    rec: _Record,
    error: str,
    status: str = "crashed",
    exit_code: int | None = 127,
    signal_name: str | None = None,
) -> None:
    now = time.time()
    summary.save(
        summary.Summary(
            run_id=run_id,
            run_name=rec.name,
            user=rec.user,
            git_sha=rec.sha,
            command=list(rec.command),
            started_unix=now,
            ended_unix=now,
            duration_seconds=0.0,
            status=status,
            exit_code=exit_code,
            signal=signal_name,
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
