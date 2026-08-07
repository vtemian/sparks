import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sparks import shared, summary

if TYPE_CHECKING:
    from sparks import spool

LOG = logging.getLogger("sparks")

FILENAME = "sparks_runs.prom"

INFO = "sparks_run_info"
INFO_HELP = "Identity of a completed training run. Always 1."

QUEUE_FILENAME = "sparks_queue.prom"

QUEUE_INFO = "sparks_queue_job_info"
QUEUE_INFO_HELP = "Identity and state of a job in the queue. Always 1."
QUEUE_DEPTH = "sparks_queue_depth"
QUEUE_DEPTH_HELP = "Jobs in each state."
QUEUE_SUBMITTED = "sparks_queue_job_submitted_timestamp_seconds"
QUEUE_SUBMITTED_HELP = "Unix time the job was submitted."
QUEUE_STARTED = "sparks_queue_job_started_timestamp_seconds"
QUEUE_STARTED_HELP = "Unix time the job's container started, absent until it does."
QUEUE_HEARTBEAT = "sparks_queue_runner_heartbeat_timestamp_seconds"
QUEUE_HEARTBEAT_HELP = "Unix time the runner last completed a pass over the queue."

LIVE_STATES = ("queued", "building", "running")


@dataclass(frozen=True)
class Numeric:
    name: str
    help: str
    value: Callable[[summary.Summary], float | None]


NUMERIC: tuple[Numeric, ...] = (
    Numeric(
        "sparks_run_start_timestamp_seconds",
        "Unix time the run started.",
        lambda s: s.started_unix,
    ),
    Numeric(
        "sparks_run_duration_seconds",
        "Wall-clock duration of the run.",
        lambda s: s.duration_seconds,
    ),
    Numeric(
        "sparks_run_energy_window_seconds",
        "Window the energy counters bracketed, wider than the run's duration.",
        lambda s: s.energy.window_seconds,
    ),
    Numeric(
        "sparks_run_energy_joules",
        "Total energy drawn over the run.",
        lambda s: s.energy.total_joules,
    ),
    Numeric(
        "sparks_run_marginal_energy_joules",
        "Energy above idle, attributable to the run.",
        lambda s: s.energy.marginal_joules,
    ),
    Numeric(
        "sparks_run_idle_watts",
        "Idle baseline power measured before the run.",
        lambda s: s.energy.idle_watts,
    ),
    Numeric(
        "sparks_run_gpu_nvml_energy_joules",
        "GPU energy as NVML measures it.",
        lambda s: s.energy.gpu_nvml_joules,
    ),
    Numeric(
        "sparks_run_gpu_firmware_energy_joules",
        "GPU energy at the firmware rail.",
        lambda s: s.energy.gpu_firmware_joules,
    ),
    Numeric(
        "sparks_run_final_loss",
        "Training loss last reported by the run.",
        lambda s: s.final_loss,
    ),
)


def rebuild(runs_dir: Path, target: Path) -> int:
    with shared.exclusive(target.parent):
        runs = [run for run in load_all(runs_dir) if run.is_terminal]
        summary.write_atomically(target, lambda: render(runs))
    return len(runs)


def load_all(runs_dir: Path) -> list[summary.Summary]:
    runs = []
    for path in sorted(runs_dir.glob(f"*/{summary.FILENAME}")):
        try:
            runs.append(summary.load(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("sparks: skipping unreadable %s: %s", path, exc)
    return runs


def render(runs: Iterable[summary.Summary]) -> str:
    terminal = sorted(
        (run for run in runs if run.is_terminal), key=lambda run: run.run_id
    )
    lines: list[str] = []
    if terminal:
        lines += family(INFO, INFO_HELP)
        lines += [
            sample(
                INFO,
                {
                    "run_id": run.run_id,
                    "user": run.user,
                    "run_name": run.run_name,
                    "status": run.status,
                    "energy_sources": run.energy.gpu_sources,
                },
                1.0,
            )
            for run in terminal
        ]
    for numeric in NUMERIC:
        rows = [(run.run_id, numeric.value(run)) for run in terminal]
        present = [(run_id, value) for run_id, value in rows if value is not None]
        if not present:
            continue
        lines += family(numeric.name, numeric.help)
        lines += [
            sample(numeric.name, {"run_id": run_id}, value) for run_id, value in present
        ]
    # Trailing newline: without it node_exporter drops the whole file.
    return "".join(f"{line}\n" for line in lines)


def render_queue(entries: Iterable["spool.Entry"], heartbeat: float) -> str:
    jobs = list(entries)
    lines = job_rows(jobs) + depth_rows(jobs) + stamp_rows(jobs)
    lines += family(QUEUE_HEARTBEAT, QUEUE_HEARTBEAT_HELP)
    lines += [sample(QUEUE_HEARTBEAT, {}, heartbeat)]
    # Trailing newline: without it node_exporter drops the whole file.
    return "".join(f"{line}\n" for line in lines)


def publish_queue(
    entries: Iterable["spool.Entry"], target: Path, heartbeat: float
) -> None:
    summary.write_atomically(target, lambda: render_queue(entries, heartbeat))


def job_rows(jobs: list["spool.Entry"]) -> list[str]:
    if not jobs:
        return []

    lines = family(QUEUE_INFO, QUEUE_INFO_HELP)
    lines += [
        sample(
            QUEUE_INFO,
            {
                "job_id": entry.job.job_id,
                "name": entry.job.name,
                "user": entry.job.user,
                "state": entry.state.state,
                # Empty rather than omitted: a changed label set is a new series.
                "image": entry.state.image or "",
                "run_id": entry.state.run_id or "",
            },
            1.0,
        )
        for entry in jobs
    ]
    return lines


def depth_rows(jobs: list["spool.Entry"]) -> list[str]:
    lines = family(QUEUE_DEPTH, QUEUE_DEPTH_HELP)
    counted = Counter(entry.state.state for entry in jobs)
    for state in (*LIVE_STATES, *sorted(set(counted) - set(LIVE_STATES))):
        lines += [sample(QUEUE_DEPTH, {"state": state}, counted[state])]
    return lines


def stamp_rows(jobs: list["spool.Entry"]) -> list[str]:
    stamps: tuple[tuple[str, str, Callable[[spool.Entry], float | None]], ...] = (
        (QUEUE_SUBMITTED, QUEUE_SUBMITTED_HELP, lambda entry: entry.job.submitted_unix),
        (QUEUE_STARTED, QUEUE_STARTED_HELP, lambda entry: entry.state.started_unix),
    )
    lines: list[str] = []
    for name, help_text, when in stamps:
        stamped = [(entry.job.job_id, when(entry)) for entry in jobs]
        present = [(job_id, value) for job_id, value in stamped if value is not None]
        if not present:
            continue
        lines += family(name, help_text)
        lines += [sample(name, {"job_id": job_id}, value) for job_id, value in present]
    return lines


def family(name: str, help_text: str) -> list[str]:
    help_line = f"# HELP {name} {help_text}"
    type_line = f"# TYPE {name} gauge"
    return [help_line, type_line]


def sample(name: str, labels: dict[str, str], value: float) -> str:
    if not labels:
        return f"{name} {number(value)}"

    pairs = ",".join(f'{key}="{escape(value)}"' for key, value in labels.items())
    return f"{name}{{{pairs}}} {number(value)}"


def escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    return escaped.replace("\n", "\\n")


def number(value: float) -> str:
    if math.isnan(value):
        return "NaN"

    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"

    if value.is_integer():
        return str(int(value))

    return repr(value)
