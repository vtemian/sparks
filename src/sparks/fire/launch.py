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
    run = _reserved(command, name, shared_dir, on_reserved, sha)
    if sampler is None:
        sampler = Sampler.detect()
    LOG.debug("sparks: sampling %.0fs of idle baseline", baseline_seconds)
    # The minute before the child exists is the one window this wrapper does not
    # otherwise defend. `Supervisor` installs its handlers when it runs, so a
    # SIGTERM arriving during the baseline takes the default disposition and
    # kills the wrapper outright -- leaving a reserved run directory with no
    # summary, no index row and no explanation. A queued job aborted 25 seconds
    # after it started did exactly that on the box.
    try:
        with _interruptible():
            # Read as a counter delta from the same counters the run is measured
            # against. The GPU rail over this window is what tells a quiet box
            # from a contended one whose baseline is not ours.
            base = sampler.baseline(baseline_seconds)
            # Monotonic, not time.time(): NTP can step the wall clock mid-run,
            # the exact hazard duration_seconds already avoids. Taken before the
            # start reads, so the window brackets every joule they will report.
            opened_at = time.monotonic()
            window = _Window(
                total_joules=sampler.total_joules(),
                gpu_nvml_joules=sampler.gpu_nvml_joules(),
                gpu_firmware_joules=sampler.gpu_firmware_joules(),
                idle_watts=base.idle_watts,
                gpu_idle_watts=base.gpu_watts,
                opened_at=opened_at,
            )
    except _AbortError as abort:
        return _cancelled(run, baseline_seconds, abort)

    # The supervisor's emitter owns metrics.LIFECYCLE and nothing else; the
    # child gets its own from SPARKS_PROMETHEUS_URL through `emit.from_env` and
    # owns everything else. Two writers on one series interleave timestamps,
    # which is a 400 that rolls back the whole request. No url is a box without
    # monitoring, and it leaves `metrics` None rather than a do-nothing
    # RunMetrics, because a constructed emitter is that second writer.
    env = {"SPARKS_RUN_ID": run.id, "PYTHONUNBUFFERED": "1"}
    metrics: RunMetrics | None = None
    if url:
        env["SPARKS_PROMETHEUS_URL"] = url
        metrics = RunMetrics(
            run_id=run.id, url=url, info={"run_name": run.name, "git_sha": run.sha}
        )
        metrics.begin()

    LOG.debug("sparks: starting the child %s", command)
    try:
        completed = Supervisor(command, log_path=run.dir / "output.log", env=env).run()
    except Exception as exc:  # noqa: BLE001 -- any failure to run still owes a record
        return _crashed(run, metrics, command, exc)

    LOG.debug(
        "sparks: the child ended %s after %.1fs",
        completed.outcome.status,
        completed.duration_seconds,
    )
    return _recorded(run, completed, window, sampler, metrics, baseline_seconds)


@dataclass(frozen=True)
class _Run:
    """Who ran what, and where it lands: fixed the moment the run is reserved.

    Built frozen in one expression, so a run holding an id but no directory, or
    a cleaned name beside a raw command, cannot exist. Every failure path needs
    all of it to write a record: the id and directory to write into, the cleaned
    identity to write, and the shared root whose index is rebuilt afterwards.
    """

    id: str
    dir: Path
    shared_dir: Path
    user: str
    name: str
    sha: str
    command: list[str]


def _reserved(
    command: list[str],
    name: str,
    shared_dir: Path,
    on_reserved: Callable[[str, Path], None] | None,
    sha: str | None,
) -> _Run:
    """The run's durable identity: cleaned record, reserved directory, id.

    Sanitising the durable copies happens at this one boundary. The child is
    still exec'd with the ORIGINAL argv, which round-trips through execve
    correctly; only what we persist and push is cleaned, so a non-UTF-8
    --name can neither poison the shared index nor crash the wrapper.
    """
    user = shared.clean(current_user())
    run_name = shared.clean(name, "run")
    # mkdir raising EEXIST is the only atomic uniqueness guarantee, and the
    # chmod inside heals a tree left group-unreadable by an earlier umask.
    run_id, run_dir = shared.reserve_dir(shared_dir / "runs", run_name, user)
    run = _Run(
        id=run_id,
        dir=run_dir,
        shared_dir=shared_dir,
        user=user,
        name=run_name,
        sha=git_sha() if sha is None else shared.clean(sha, "unknown"),
        command=[shared.clean(arg, "", limit=4000) for arg in command],
    )
    LOG.debug("sparks: reserved %s in %s", run.id, run.dir)
    if on_reserved is None:
        return run
    try:
        # Never allowed to sink the run: whoever wanted to be told has a
        # worse view of the world if this fails, but the run itself is not.
        on_reserved(run.id, run.dir)
    except Exception as exc:  # noqa: BLE001 -- an observer's failure is not the run's
        LOG.warning("sparks: could not announce %s: %s", run.id, exc)
    return run


@dataclass(frozen=True)
class _Window:
    """The measurement window at the moment it opened.

    The counters and the idle rates carry the name of the `EnergyReading` field
    they will close, so pairing a start with the wrong end read cannot happen
    silently. `opened_at` is the exception and is named apart deliberately: it
    is a monotonic instant, while `EnergyReading.seconds` is a duration.

    Frozen and built in one expression: a window holding three of its five start
    reads is a wrong energy figure rather than a missing one.
    """

    total_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    gpu_idle_watts: float
    opened_at: float


def _recorded(
    run: _Run,
    completed: Completed,
    window: _Window,
    sampler: Sampler,
    metrics: RunMetrics | None,
    baseline_seconds: float,
) -> Launched:
    """The child is reaped; write everything down.

    One try/finally so the record cannot be lost: a full disk, a quota, or the
    other user owning the directory must not stop metrics.end(status) firing,
    or training_run_info (LIFECYCLE-exempt from stale marking) sits on the
    dashboard forever with no end and no status.
    """
    status = completed.outcome.status
    LOG.debug("sparks: writing the record for %s", run.id)
    try:
        record = summary.Summary(
            run_id=run.id,
            run_name=run.name,
            user=run.user,
            git_sha=run.sha,
            command=run.command,
            started_unix=completed.started_unix,
            ended_unix=completed.ended_unix,
            duration_seconds=completed.duration_seconds,
            status=status,
            exit_code=completed.outcome.exit_code,
            signal=completed.outcome.signal_name,
            escalated_to_sigkill=completed.outcome.escalated_to_sigkill,
            energy=_close_window(window, sampler, completed, baseline_seconds),
        )
        summary.save(record, run.dir)
    except Exception:
        # The child completed and its exit code is faithful; losing the record
        # must neither strand a phantom on the dashboard nor lie about $?.
        LOG.exception("sparks: could not record %s", run.id)
    finally:
        if metrics is not None:
            metrics.end(status)
        _rebuild(run.shared_dir)
    return Launched(run.id, status, completed.outcome.wrapper_exit, run.dir)


def _close_window(
    window: _Window,
    sampler: Sampler,
    completed: Completed,
    baseline_seconds: float,
) -> summary.Energy:
    """The window's end reads, folded with its start into the run's energy.

    `sampler` must be the one that took the start reads: a delta whose two
    endpoints come from different counters is not a measurement.
    """
    # The counters bracket a wider window than the child ran for: the end
    # read happens after the group sweep and the tee drain, measured at up
    # to 10s. Subtracting the idle baseline over the child's duration
    # instead would bill that cleanup to the run's marginal energy: on a
    # 1.5s run with 5s of cleanup, at 13W, that is 65J of pure idle reported
    # as marginal, which is more than 100% wrong.
    measured_seconds = max(
        completed.duration_seconds, time.monotonic() - window.opened_at
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


def _crashed(
    run: _Run, metrics: RunMetrics | None, command: list[str], error: Exception
) -> Launched:
    """A command that does not exist, or a failure inside the supervisor.

    Without this the run has no summary.json, no index row, and a
    training_run_info already pushed that is LIFECYCLE-exempt and so never
    staled: a permanent phantom run on the dashboard that never ends and never
    gets a status.
    """
    LOG.exception("sparks: could not run %s: %s", command, error)
    try:
        _record_failed_launch(run, str(error))
    except Exception:
        # metrics.begin() already pushed training_run_info, which is exempt
        # from stale marking. Failing to write the summary must not also skip
        # the end below, or that series is a phantom run with no end forever.
        LOG.exception("sparks: could not record the failed launch of %s", run.id)
    finally:
        if metrics is not None:
            metrics.end("crashed")
        _rebuild(run.shared_dir)
    return Launched(run.id, "crashed", 127, run.dir)


def _cancelled(run: _Run, baseline_seconds: float, abort: "_AbortError") -> Launched:
    """Stopped during the baseline, before any emitter exists.

    Nothing was pushed, so there is nothing to end and nothing to stale; the
    index rebuild is the whole obligation.
    """
    LOG.warning("sparks: %s before the run started; recording it stopped", abort)
    try:
        _record_failed_launch(
            run,
            f"stopped during the {baseline_seconds:.0f}s baseline",
            status="cancelled",
            # Nothing ran, so there is no exit code to report. `cancelled` with
            # the signal that caused it is the whole truth about this run.
            exit_code=None,
            signal_name=str(abort),
        )
    except Exception:
        # An unwritable run directory must not also cost us the index row.
        LOG.exception("sparks: could not record the stop of %s", run.id)
    finally:
        _rebuild(run.shared_dir)
    return Launched(run.id, "cancelled", 128 + abort.signum, run.dir)


def _record_failed_launch(
    run: _Run,
    error: str,
    status: str = "crashed",
    exit_code: int | None = 127,
    signal_name: str | None = None,
) -> None:
    now = time.time()
    summary.save(
        summary.Summary(
            run_id=run.id,
            run_name=run.name,
            user=run.user,
            git_sha=run.sha,
            command=list(run.command),
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
        run.dir,
    )
    # FileNotFoundError's message carries the bad byte straight from argv, so
    # write_text(error) would raise UnicodeEncodeError inside launch()'s except
    # and escape with a traceback. clean() is the same one-boundary fix.
    (run.dir / "error.txt").write_text(shared.clean(error, "", limit=4000) + "\n")


def _rebuild(shared_dir: Path) -> None:
    """Refresh the run index. Never raises: a corrupt summary somewhere else, or
    a box with no textfile directory to publish into, must not surface as this
    run having failed.

    The CLI has already refused to start on an unprovisioned box, so reaching
    the warning below means either a library caller that never asked for a
    contract, or provisioning that changed mid-run.
    """
    runs_dir = shared_dir / "runs"
    LOG.debug("sparks: rebuilding the run index from %s", runs_dir)
    try:
        target = box.textfile_dir() / index.FILENAME
        shared.make_dir(target.parent)
        written = index.rebuild(runs_dir, target)
    except Exception as exc:  # noqa: BLE001 -- the run itself still succeeded
        LOG.warning("sparks: could not rebuild the run index: %s", exc)
        return
    LOG.info("sparks: %d runs in %s", written, target)


class _AbortError(Exception):
    """A stop signal arrived before there was a child to forward it to."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signal.Signals(signum).name)

    @staticmethod
    def raise_from_signal(signum: int, _frame: object) -> None:
        """Shaped for `signal.signal`, which `_interruptible` registers."""
        raise _AbortError(signum)


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

    signums = (signal.SIGINT, signal.SIGTERM)
    previous = {
        signum: signal.signal(signum, _AbortError.raise_from_signal)
        for signum in signums
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
