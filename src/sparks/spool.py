import contextlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Self

from sparks import shared, summary

LOG = logging.getLogger("sparks")

JOB_FILE = "job.json"
STATE_FILE = "state.json"
CONTEXT_DIR = "context"
DATA_DIR = "data"
PULL_LOG = "pull.log"
LAUNCH_LOG = "launch.log"
CID_FILE = "container.id"
RUN_ID_FILE = "run_id"
REQUESTS_DIR = "requests"

QUEUE_MODE = 0o3775

QUEUED = "queued"
BUILDING = "building"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
ABORTED = "aborted"
UNKNOWN = "unknown"

TERMINAL = frozenset({FINISHED, FAILED, CANCELLED, ABORTED})

CANCEL = "cancel"
ABORT = "abort"
ACTIONS = frozenset({CANCEL, ABORT})

RETENTION_SECONDS = 6 * 3600.0


@dataclass(frozen=True)
class Job:
    job_id: str
    name: str
    user: str
    command: list[str]
    submitted_unix: float
    image: str
    git_sha: str = "unknown"
    git_dirty: bool = False
    retry_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            job_id=data["job_id"],
            name=shared.clean(data["name"], "job"),
            user=shared.clean(data["user"], "unknown"),
            command=list(data["command"]),
            submitted_unix=float(data["submitted_unix"]),
            image=data["image"],
            git_sha=data.get("git_sha", "unknown"),
            git_dirty=bool(data.get("git_dirty", False)),
            retry_of=data.get("retry_of"),
        )


@dataclass(frozen=True)
class State:
    state: str = QUEUED
    image: str | None = None
    run_id: str | None = None
    container_id: str | None = None
    started_unix: float | None = None
    finished_unix: float | None = None
    exit_code: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            state=str(data.get("state", QUEUED)),
            image=data.get("image"),
            run_id=data.get("run_id"),
            container_id=data.get("container_id"),
            started_unix=data.get("started_unix"),
            finished_unix=data.get("finished_unix"),
            exit_code=data.get("exit_code"),
            detail=data.get("detail"),
        )


@dataclass(frozen=True)
class Request:
    action: str
    uid: int
    path: Path


@dataclass(frozen=True)
class Entry:
    job: Job
    state: State
    path: Path
    owner_uid: int

    @property
    def is_terminal(self) -> bool:
        return self.state.state in TERMINAL

    @property
    def context_dir(self) -> Path:
        return self.path / CONTEXT_DIR

    @property
    def data_dir(self) -> Path:
        return self.path / DATA_DIR

    def may_be_controlled_by(self, uid: int) -> bool:
        return uid in (0, self.owner_uid)


def make_queue_dir(queue_dir: Path) -> Path:
    return shared.make_dir(queue_dir, mode=QUEUE_MODE)


def reserve(
    queue_dir: Path, name: str, user: str, when: float | None = None
) -> tuple[str, Path]:
    make_queue_dir(queue_dir)
    return shared.reserve_dir(queue_dir, name, user, prefix="job", when=when)


def commit(path: Path, job: Job) -> Entry:
    summary.write_atomically(
        path / JOB_FILE, lambda: json.dumps(job.to_dict(), indent=2) + "\n"
    )
    return load(path)


def submit(
    queue_dir: Path,
    name: str,
    user: str,
    command: list[str],
    image: str,
    when: float | None = None,
    git_sha: str = "unknown",
    git_dirty: bool = False,
    retry_of: str | None = None,
) -> Entry:
    submitted = time.time() if when is None else when
    job_id, path = reserve(queue_dir, name, user, when=when)
    return commit(
        path,
        Job(
            job_id=job_id,
            name=shared.clean(name, "job"),
            user=shared.clean(user, "unknown"),
            command=[shared.clean(arg, "", limit=4000) for arg in command],
            submitted_unix=submitted,
            image=image,
            git_sha=git_sha,
            git_dirty=git_dirty,
            retry_of=retry_of,
        ),
    )


def load(path: Path) -> Entry:
    manifest = path / JOB_FILE
    with manifest.open(encoding="utf-8") as handle:
        job = Job.from_dict(json.load(handle))
    return Entry(
        job=job,
        state=read_state(path),
        path=path,
        owner_uid=manifest.stat().st_uid,
    )


def entries(queue_dir: Path) -> list[Entry]:
    found = []
    for path in sorted(queue_dir.glob(f"*/{JOB_FILE}")):
        try:
            found.append(load(path.parent))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            LOG.warning("sparks: skipping unreadable job %s: %s", path.parent, exc)
    return sorted(found, key=lambda e: (e.job.submitted_unix, e.job.job_id))


def next_queued(queue_dir: Path) -> Entry | None:
    return next(
        (entry for entry in entries(queue_dir) if entry.state.state == QUEUED),
        None,
    )


def publishable(
    queue_dir: Path, now: float | None = None, retention: float = RETENTION_SECONDS
) -> list[Entry]:
    moment = time.time() if now is None else now
    keep = []
    for entry in entries(queue_dir):
        if not entry.is_terminal:
            keep.append(entry)
            continue
        finished = entry.state.finished_unix
        # An undated terminal job cannot be aged out honestly, so it stays.
        if finished is None or moment - finished <= retention:
            keep.append(entry)
    return keep


def set_state(path: Path, state: State) -> None:
    summary.write_atomically(
        path / STATE_FILE, lambda: json.dumps(state.to_dict(), indent=2) + "\n"
    )


# `**changes` goes straight into dataclasses.replace, whose field types are
# heterogeneous; `object` would break that call under mypy strict.
def advance(path: Path, **changes: Any) -> State:  # noqa: ANN401
    state = replace(read_state(path), **changes)
    set_state(path, state)
    return state


def request(path: Path, action: str) -> Path:
    if action not in ACTIONS:
        raise ValueError(f"{action!r} is not one of {sorted(ACTIONS)}")
    directory = shared.make_dir(path / REQUESTS_DIR)
    target = directory / f"{action}.{os.getuid()}"
    # Touched, not written atomically: the file's ownership is the message, and
    # a rename would land it owned by whoever wrote the temp file.
    target.touch(mode=shared.FILE_MODE, exist_ok=True)
    return target


def requests(path: Path) -> list[Request]:
    directory = path / REQUESTS_DIR
    found = []
    for candidate in sorted(directory.glob("*")):
        action = candidate.name.split(".", 1)[0]
        if action not in ACTIONS:
            continue
        try:
            uid = candidate.stat().st_uid
        except OSError:
            continue
        found.append(Request(action=action, uid=uid, path=candidate))
    return found


def clear_requests(path: Path) -> None:
    for pending in requests(path):
        with contextlib.suppress(OSError):
            pending.path.unlink()


def remove(path: Path) -> None:
    shutil.rmtree(path)


def read_state(path: Path) -> State:
    state_path = path / STATE_FILE
    try:
        with state_path.open(encoding="utf-8") as handle:
            state = State.from_dict(json.load(handle))
    except FileNotFoundError:
        state = State()
    except (OSError, ValueError, TypeError) as exc:
        LOG.warning(
            "sparks: %s has an unreadable state, so it will not be started: %s",
            path.name,
            exc,
        )
        state = State(state=UNKNOWN)
    return state
