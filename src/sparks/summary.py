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


@dataclass(frozen=True)
class Energy:
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
    git_sha: str
    command: list[str]
    started_unix: float
    ended_unix: float
    duration_seconds: float
    status: str
    exit_code: int | None
    signal: str | None
    escalated_to_sigkill: bool
    energy: Energy
    final_loss: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in STATUSES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.final_loss is None or not math.isfinite(self.final_loss):
            data.pop("final_loss", None)
        for field, value in data["energy"].items():
            if isinstance(value, float) and not math.isfinite(value):
                data["energy"][field] = None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
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
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / FILENAME
    write_atomically(path, lambda: json.dumps(summary.to_dict(), indent=2) + "\n")
    return path


def load(path: Path) -> Summary:
    with path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return Summary.from_dict(data)


def write_atomically(
    target: Path, render: Callable[[], str], mode: int = 0o644
) -> None:
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
