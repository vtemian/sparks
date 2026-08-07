r"""One `.prom` file holding every completed run, rebuilt from the summaries.

node_exporter's textfile collector re-scrapes this file for as long as it
exists, so these rows never go stale and never age out of retention, and the
`summary.json` files stay the source of truth even if the TSDB is lost.

One aggregated file, not one per run: the collector reads every file on every
scrape, and measured per-file overhead is ~73us locally and ~150us on the box,
perfectly linear. 5000 files would cost ~0.75s of every scrape forever, against
~20ms for the aggregate. The single file stops being comfortable around 100k
series, which is 3-4x the 5000-run horizon.

Five format rules, each verified against node_exporter 1.12.1 and each fatal:

- the file MUST end with a newline, or the whole file is dropped
- `# TYPE ... info` is REJECTED; only counter, gauge, summary, untyped and
  histogram exist, so an info metric is declared `gauge`
- TYPE appears once per family and before that family's first sample, so the
  samples of one family are grouped rather than a header repeated per run
- only three escapes in a label value: `\\`, `\"` and `\n`. A `\t` is a parse
  error, so a literal tab is written through untouched
- an explicit timestamp makes the collector skip the ENTIRE file, so a run can
  never be backdated to when it really happened. History lives in the *values*,
  which is why `sparks_run_start_timestamp_seconds` exists

Everything is a gauge. These are final measurements, `rate()` over them is
nonsense, and a `_total` suffix would invite exactly that.
"""

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sparks import shared, summary

if TYPE_CHECKING:  # only for the annotation; spool imports nothing from here
    from sparks import spool

LOG = logging.getLogger("sparks")

FILENAME = "sparks_runs.prom"
"""Written into node_exporter's textfile directory."""

INFO = "sparks_run_info"
INFO_HELP = "Identity of a completed training run. Always 1."

QUEUE_FILENAME = "sparks_queue.prom"
"""The live queue, beside the permanent run index and deliberately not in it."""

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
"""Always published, even at zero. Spelled out rather than imported from spool
to keep this module's dependency one-way: spool is about the filesystem, this is
about the text format, and the run index half of this file predates both."""


@dataclass(frozen=True)
class Numeric:
    """One numeric family, keyed only on `run_id` so the info metric is the one
    place identity labels live and a join is the only way to get at them."""

    name: str
    help: str
    value: Callable[[summary.Summary], float | None]
    """None means this run has no sample for the family, which is how a run
    without a final loss avoids being recorded as having lost zero."""


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
    # Published unconditionally, even at 0.0, so a blank marginal column has a
    # visible cause: an idle_watts of 0 says no baseline was measured.
    Numeric(
        "sparks_run_idle_watts",
        "Idle baseline power measured before the run.",
        lambda s: s.energy.idle_watts,
    ),
    # Separate families rather than one split by a `source` label: the derived
    # gpu/total ratio moves from 0.30 to 0.37 purely by switching source, so a
    # single unlabelled number is a trap.
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
"""Joules, never watt-hours: `promtool check metrics` warns on watt-hours and
Prometheus naming guidance is base units. Divide by 3600 in the panel."""


def rebuild(runs_dir: Path, target: Path) -> int:
    """Rewrite `target` from every terminal run under `runs_dir`, and return how
    many runs the index now holds.

    `load_all` and the write are locked *together*: `write_atomically` makes the
    write atomic but not the read-modify-write around it, and the read window
    grows with history (0.9ms at 10 runs, 363ms at 5000), so two runs finishing
    within it drop each other's rows. The lock is on the index directory rather
    than a lock file, which under umask 077 the other user could never open.
    """
    with shared.exclusive(target.parent):
        runs = [run for run in load_all(runs_dir) if run.is_terminal]
        summary.write_atomically(target, lambda: render(runs))
    return len(runs)


def load_all(runs_dir: Path) -> list[summary.Summary]:
    """Every readable `summary.json` under `runs_dir`.

    One unreadable file must not cost every other run its row: the rebuild
    happens at the end of a run, so an exception raised here would surface as
    the run itself having failed.
    """
    runs = []
    for path in sorted(runs_dir.glob(f"*/{summary.FILENAME}")):
        try:
            runs.append(summary.load(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("sparks: skipping unreadable %s: %s", path, exc)
    return runs


def render(runs: Iterable[summary.Summary]) -> str:
    """The whole file, or an empty string when there is nothing to write.

    Non-terminal runs are left out. A run recorded as `running` and then
    rewritten as `finished` stales one series and creates another, doubling
    churn for nothing.
    """
    terminal = sorted(
        (run for run in runs if run.is_terminal), key=lambda run: run.run_id
    )
    lines: list[str] = []
    if terminal:
        lines += _family(INFO, INFO_HELP)
        lines += [
            _sample(
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
    for family in NUMERIC:
        rows = [(run.run_id, family.value(run)) for run in terminal]
        # A family nothing has a value for is not declared at all: a bare TYPE
        # header with no samples under it is noise a scrape pays for forever.
        present = [(run_id, value) for run_id, value in rows if value is not None]
        if not present:
            continue
        lines += _family(family.name, family.help)
        lines += [
            _sample(family.name, {"run_id": run_id}, value) for run_id, value in present
        ]
    return "".join(f"{line}\n" for line in lines)


def render_queue(entries: Iterable["spool.Entry"], heartbeat: float) -> str:
    """The queue as it stands right now.

    Unlike the run index this is a *live* view that is rewritten every tick, so
    a job appears and disappears from it. That is the opposite of the run index,
    whose whole point is that rows never go away, and the two are separate files
    for exactly that reason: mixing a churning series into the permanent one
    would put both on the same retention.

    `heartbeat` is written unconditionally, including on an empty queue. It is
    the only thing that can distinguish a box with nothing to do from a runner
    that died, and those need different people looking at them.
    """
    jobs = list(entries)
    lines = _job_rows(jobs) + _depth_rows(jobs) + _stamp_rows(jobs)
    lines += _family(QUEUE_HEARTBEAT, QUEUE_HEARTBEAT_HELP)
    lines += [_sample(QUEUE_HEARTBEAT, {}, heartbeat)]
    return "".join(f"{line}\n" for line in lines)


def publish_queue(
    entries: Iterable["spool.Entry"], target: Path, heartbeat: float
) -> None:
    """Rewrite the queue's `.prom` file.

    No lock, unlike `rebuild`: there is exactly one runner and it is the only
    writer of this file, so the read-modify-write hazard the run index has does
    not exist here. The write is still atomic, because node_exporter reads it
    concurrently.
    """
    summary.write_atomically(target, lambda: render_queue(entries, heartbeat))


def _job_rows(jobs: list["spool.Entry"]) -> list[str]:
    """The info family: one row per job, absent entirely on an empty queue."""
    if not jobs:
        return []
    lines = _family(QUEUE_INFO, QUEUE_INFO_HELP)
    lines += [
        _sample(
            QUEUE_INFO,
            {
                "job_id": entry.job.job_id,
                "name": entry.job.name,
                "user": entry.job.user,
                "state": entry.state.state,
                # Present and empty rather than omitted before a build:
                # changing the label SET between scrapes creates a second
                # series, and every join against the first then breaks.
                "image": entry.state.image or "",
                "run_id": entry.state.run_id or "",
            },
            1.0,
        )
        for entry in jobs
    ]
    return lines


def _depth_rows(jobs: list["spool.Entry"]) -> list[str]:
    """The depth family, published even for an empty queue."""
    lines = _family(QUEUE_DEPTH, QUEUE_DEPTH_HELP)
    counted = Counter(entry.state.state for entry in jobs)
    # The live states always, so an alert can say "queued > 0 and running == 0"
    # without `or vector(0)` in the one situation the expression exists for.
    for state in (*LIVE_STATES, *sorted(set(counted) - set(LIVE_STATES))):
        lines += [_sample(QUEUE_DEPTH, {"state": state}, counted[state])]
    return lines


def _stamp_rows(jobs: list["spool.Entry"]) -> list[str]:
    """The timestamp families, each declared only when some job has a value."""
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
        lines += _family(name, help_text)
        lines += [_sample(name, {"job_id": job_id}, value) for job_id, value in present]
    return lines


def _family(name: str, help_text: str) -> list[str]:
    help_line = f"# HELP {name} {help_text}"
    type_line = f"# TYPE {name} gauge"
    return [help_line, type_line]


def _sample(name: str, labels: dict[str, str], value: float) -> str:
    """A sample line. Empty braces are omitted rather than written as `{}`,
    which not every parser in the chain accepts."""
    if not labels:
        return f"{name} {_number(value)}"
    pairs = ",".join(f'{key}="{_escape(value)}"' for key, value in labels.items())
    return f"{name}{{{pairs}}} {_number(value)}"


def _escape(value: str) -> str:
    """The only three escapes the text format defines. Backslash first, or the
    backslashes introduced by the other two get escaped a second time."""
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    return escaped.replace("\n", "\\n")


def _number(value: float) -> str:
    """Prometheus's float syntax. Whole numbers drop the `.0`, and a diverged
    run's NaN loss is spelled the way the parser spells it."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer():
        return str(int(value))
    return repr(value)
