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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from sparks import summary

LOG = logging.getLogger("sparks")

FILENAME = "sparks_runs.prom"
"""Written into node_exporter's textfile directory."""

INFO = "sparks_run_info"
INFO_HELP = "Identity of a completed training run. Always 1."


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
        "sparks_run_energy_joules",
        "Total energy drawn over the run.",
        lambda s: s.energy.total_joules,
    ),
    Numeric(
        "sparks_run_marginal_energy_joules",
        "Energy above idle, attributable to the run.",
        lambda s: s.energy.marginal_joules,
    ),
    # Both GPU sources, as separate families rather than one family split by a
    # `source` label: they disagree by a stable ~22.5% because they measure at
    # different boundaries, and the derived gpu/total ratio moves from 0.30 to
    # 0.37 purely by switching source. A single unlabelled number is a trap.
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
    many runs the index now holds."""
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
        except (OSError, ValueError, KeyError, TypeError) as e:
            LOG.warning("sparks: skipping unreadable %s: %s", path, e)
    return runs


def render(runs: Iterable[summary.Summary]) -> str:
    """The whole file, or an empty string when there is nothing to write.

    Non-terminal runs are left out. A run recorded as `running` and then
    rewritten as `finished` stales one series and creates another, doubling
    churn for nothing.
    """
    terminal = sorted((r for r in runs if r.is_terminal), key=lambda r: r.run_id)
    lines: list[str] = []
    if terminal:
        lines += _family(INFO, INFO_HELP)
        lines += [
            _sample(
                INFO,
                {
                    "run_id": r.run_id,
                    "user": r.user,
                    "run_name": r.run_name,
                    "status": r.status,
                },
                1.0,
            )
            for r in terminal
        ]
    for family in NUMERIC:
        rows = [(r.run_id, family.value(r)) for r in terminal]
        # A family nothing has a value for is not declared at all: a bare TYPE
        # header with no samples under it is noise a scrape pays for forever.
        present = [(run_id, v) for run_id, v in rows if v is not None]
        if not present:
            continue
        lines += _family(family.name, family.help)
        lines += [_sample(family.name, {"run_id": run_id}, v) for run_id, v in present]
    return "".join(f"{line}\n" for line in lines)


def _family(name: str, help_text: str) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]


def _sample(name: str, labels: dict[str, str], value: float) -> str:
    pairs = ",".join(f'{key}="{_escape(v)}"' for key, v in labels.items())
    return f"{name}{{{pairs}}} {_number(value)}"


def _escape(value: str) -> str:
    """The only three escapes the text format defines. Backslash first, or the
    backslashes introduced by the other two get escaped a second time."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


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
