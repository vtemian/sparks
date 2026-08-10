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
    run = reserved(command, name, shared_dir, on_reserved, sha)
    if sampler is None:
        sampler = Sampler.detect()
    LOG.debug("sparks: sampling %.0fs of idle baseline", baseline_seconds)
    try:
        with interruptible():
            base = sampler.baseline(baseline_seconds)
            # Monotonic, not time.time(): NTP can step the wall clock mid-run.
            # Taken before the start reads, so the window brackets every joule.
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
        return cancelled(run, baseline_seconds, abort)

    # The supervisor's emitter owns metrics.LIFECYCLE and nothing else; the child
    # gets the rest from SPARKS_PROMETHEUS_URL. Two writers on one series is a 400
    # that rolls back the whole request, so no url must leave `metrics` None.
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
        completed = Supervisor(
            command, log_path=run.dir / summary.OUTPUT_LOG, env=env
        ).run()
    except Exception as exc:  # noqa: BLE001 -- any failure to run still owes a record
        return crashed(run, metrics, command, exc)

    LOG.debug(
        "sparks: the child ended %s after %.1fs",
        completed.outcome.status,
        completed.duration_seconds,
    )
    return recorded(run, completed, window, sampler, metrics, baseline_seconds)


@dataclass(frozen=True)
class _Run:
    id: str
    dir: Path
    shared_dir: Path
    user: str
    name: str
    sha: str
    command: list[str]


def reserved(
    command: list[str],
    name: str,
    shared_dir: Path,
    on_reserved: Callable[[str, Path], None] | None,
    sha: str | None,
) -> _Run:
    user = shared.clean(current_user())
    run_name = shared.clean(name, "run")
    # mkdir raising EEXIST is the only atomic uniqueness guarantee.
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
        on_reserved(run.id, run.dir)
    except Exception as exc:  # noqa: BLE001 -- an observer's failure is not the run's
        LOG.warning("sparks: could not announce %s: %s", run.id, exc)
    return run


@dataclass(frozen=True)
class _Window:
    total_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    gpu_idle_watts: float
    opened_at: float


def recorded(
    run: _Run,
    completed: Completed,
    window: _Window,
    sampler: Sampler,
    metrics: RunMetrics | None,
    baseline_seconds: float,
) -> Launched:
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
            energy=close_window(window, sampler, completed, baseline_seconds),
        )
        summary.save(record, run.dir)
    except Exception:
        LOG.exception("sparks: could not record %s", run.id)
    finally:
        # Both fire on every path or the run is stranded: training_run_info is
        # exempt from stale marking, so a missed end() is a phantom run on the
        # dashboard forever, and a missed rebuild is a run missing from the index.
        if metrics is not None:
            metrics.end(status)
        rebuild(run.shared_dir)
    return Launched(run.id, status, completed.outcome.wrapper_exit, run.dir)


def close_window(
    window: _Window,
    sampler: Sampler,
    completed: Completed,
    baseline_seconds: float,
) -> summary.Energy:
    measured_seconds = max(
        completed.duration_seconds, time.monotonic() - window.opened_at
    )
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


def crashed(
    run: _Run, metrics: RunMetrics | None, command: list[str], error: Exception
) -> Launched:
    LOG.exception("sparks: could not run %s: %s", command, error)
    try:
        record_failed_launch(run, str(error))
    except Exception:
        LOG.exception("sparks: could not record the failed launch of %s", run.id)
    finally:
        if metrics is not None:
            metrics.end("crashed")
        rebuild(run.shared_dir)
    return Launched(run.id, "crashed", 127, run.dir)


def cancelled(run: _Run, baseline_seconds: float, abort: "_AbortError") -> Launched:
    LOG.warning("sparks: %s before the run started; recording it stopped", abort)
    try:
        record_failed_launch(
            run,
            f"stopped during the {baseline_seconds:.0f}s baseline",
            status="cancelled",
            exit_code=None,
            signal_name=str(abort),
        )
    except Exception:
        LOG.exception("sparks: could not record the stop of %s", run.id)
    finally:
        rebuild(run.shared_dir)
    return Launched(run.id, "cancelled", 128 + abort.signum, run.dir)


def record_failed_launch(
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
            # Nothing ran, so nothing was measured: None, never 0.0, which would
            # claim a measured zero.
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
    # FileNotFoundError's message carries the bad byte straight from argv, so an
    # uncleaned write_text would raise UnicodeEncodeError inside launch()'s except.
    (run.dir / summary.ERROR_FILE).write_text(
        shared.clean(error, "", limit=4000) + "\n"
    )


def rebuild(shared_dir: Path) -> None:
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
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signal.Signals(signum).name)

    @staticmethod
    def raise_from_signal(signum: int, _frame: object) -> None:
        raise _AbortError(signum)


@contextlib.contextmanager
def interruptible() -> Iterator[None]:

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
