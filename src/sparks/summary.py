"""What a run was and how it ended, as one JSON file per run.

`$SPARKS_SHARED_DIR/runs/<run_id>/summary.json` is the durable record. The run
index is rebuilt from these files, so after losing the TSDB entirely the
overview table can be regenerated from disk alone. Anything the index needs must
therefore live here, and nothing here may depend on Prometheus still holding the
run.

Every field is required except `final_loss`. A durable record with a silently
defaulted field is worse than a TypeError at the one call site that writes it.
"""

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self

from sparks.shared import clean

FILENAME = "summary.json"

STATUSES = frozenset({"finished", "crashed", "cancelled", "killed", "oom"})
"""How a run ended.

- `finished` exited 0 and the wrapper was never signalled
- `crashed` non-zero exit, or a signal death the wrapper did not cause
- `cancelled` the *wrapper* was signalled, whatever the child then chose to do
- `killed` a SIGKILL the wrapper did not send, cause unproven
- `oom` `killed` plus cgroup proof

`cancelled` is decided by whether the wrapper was signalled, never by how the
child exited: a script that traps SIGTERM, checkpoints and exits 0 is not
`finished`. Every one of these is terminal, and only these five reach the index.
"""


@dataclass(frozen=True)
class Energy:
    """What the run cost, with the derived figures frozen in rather than
    recomputed, because the index must not have to know the arithmetic.

    Both GPU sources are kept. NVML and the firmware rail counter disagree by a
    stable ~22.5% because they measure at different boundaries, so a single
    "GPU energy" number is meaningless without saying which one it is, and
    `gpu_sources` records whether their ratio still says both are trustworthy.

    Every measured quantity is None, never 0.0, when it could not be measured:
    the three counters when the box has no such sensor or an endpoint of their
    delta failed to read, and `marginal_joules` additionally when the baseline
    could not carry the subtraction. A zero would claim the run drew nothing,
    which is a different and false statement. The window and baseline seconds
    are persisted so the record reproduces its own arithmetic, and
    `idle_gpu_watts` is kept so the busy-GPU threshold can be recalibrated from
    records rather than guessed again.
    """

    total_joules: float | None
    marginal_joules: float | None
    gpu_nvml_joules: float | None
    gpu_firmware_joules: float | None
    idle_watts: float
    idle_gpu_watts: float
    window_seconds: float
    baseline_seconds: float
    gpu_sources: str


@dataclass(frozen=True)
class Summary:
    run_id: str
    run_name: str
    user: str
    """Who to ask about it."""
    git_sha: str
    command: list[str]
    started_unix: float
    """`time.time()`, which is what puts the run on the dashboard's time axis."""
    ended_unix: float
    duration_seconds: float
    """A `time.monotonic()` delta, immune to NTP stepping the clock mid-run.

    It will disagree with `ended_unix - started_unix` whenever the clock steps,
    and that is CORRECT: the wall-clock pair positions the run, the monotonic
    delta measures it. Do not "reconcile" them.
    """
    status: str
    """One of STATUSES. Kept as a plain string rather than validated here, so
    that a file this version does not understand still loads and is simply left
    out of the index instead of crashing the rebuild."""
    exit_code: int | None
    """The child's exit status, or null when it died to a signal."""
    signal: str | None
    """The signal's name, `SIGKILL` rather than 9, or null when none was
    involved.

    Orthogonal to `exit_code`, never collapsed into it: a child that genuinely
    calls `exit(137)` and a SIGKILLed child both give the caller `$? = 137`, and
    these two fields are the only thing that can tell them apart. Both are set
    at once for a `cancelled` run, where the wrapper was signalled and the child
    still exited under its own power.
    """
    escalated_to_sigkill: bool
    """The child ignored its grace period and had to be killed."""
    energy: Energy
    final_loss: float | None = None
    """Absent from the JSON when there is none. Never null, and never zero: a
    run that reported no loss did not report a loss of zero."""

    @property
    def is_terminal(self) -> bool:
        return self.status in STATUSES

    def to_dict(self) -> dict[str, Any]:
        # Sanitise non-finite floats at this boundary rather than passing
        # allow_nan=False to json.dumps: the dumps raises, launch() does not
        # wrap the save, and the result is a run that completed and then crashed
        # the wrapper writing its own record. Losing one number beats the file.
        data = asdict(self)
        if self.final_loss is None or not math.isfinite(self.final_loss):
            data.pop("final_loss", None)
        for field, value in data["energy"].items():
            if isinstance(value, float) and not math.isfinite(value):
                data["energy"][field] = None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        # Clean run_name and user on the way back in, not just on the way out:
        # this is the line that unfreezes a box whose index is already poisoned
        # by a summary written before the boundary sanitised inputs, without
        # anyone hand-editing files. Both ride the index as label values.
        return cls(
            run_id=data["run_id"],
            run_name=clean(data["run_name"], ""),
            user=clean(data["user"], ""),
            git_sha=data["git_sha"],
            command=list(data["command"]),
            started_unix=data["started_unix"],
            ended_unix=data["ended_unix"],
            duration_seconds=data["duration_seconds"],
            status=data["status"],
            exit_code=data["exit_code"],
            signal=data["signal"],
            escalated_to_sigkill=data["escalated_to_sigkill"],
            energy=Energy(**data["energy"]),
            final_loss=data.get("final_loss"),
        )


def save(summary: Summary, run_dir: Path) -> Path:
    """Write `summary.json` into the run's own directory, and return its path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / FILENAME
    write_atomically(path, lambda: json.dumps(summary.to_dict(), indent=2) + "\n")
    return path


def load(path: Path) -> Summary:
    """Read one `summary.json`.

    Takes the file rather than the run directory, because the index globs for
    the files directly."""
    with path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return Summary.from_dict(data)


def write_atomically(
    target: Path, render: Callable[[], str], mode: int = 0o644
) -> None:
    """Write `render()` to `target` so a reader sees the old file or the new
    one, never half of either.

    `render` is called with the temp file already open, so a failure while
    building the content leaves nothing behind either.

    The mode is the trap and is not incidental: `mkstemp` creates 0600, and
    renaming that into place produces exactly the unreadable file node_exporter
    skips in silence. The temp name must also not end in `.prom`, or the
    collector reads it half-written. Measured on the index, a naive
    `open(target, "w")` lost 30 of 60 concurrent scrapes to truncation; this
    sequence lost 0 of 53.

    Same directory as the target, both for rename atomicity and to inherit the
    shared directory's setgid group. No fsync: after a power loss the index is
    regenerated from the summaries anyway.
    """
    fd, tmp = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
