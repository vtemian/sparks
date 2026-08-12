import contextlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field, replace
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
ENV_FILE = "env"

QUEUE_MODE = 0o3775
ENV_MODE = 0o600

MAX_VALUE = 4000

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


class EnvError(Exception): ...


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
    # Published: to_dict is what `status --json` hands every account on the
    # box. Secret VALUES live in ENV_FILE at 0600 and only their names here.
    env: dict[str, str] = field(default_factory=dict)
    secret_names: list[str] = field(default_factory=list)

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
            env=dict(data.get("env") or {}),
            secret_names=list(data.get("secret_names") or []),
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

    @property
    def env_file(self) -> Path:
        return self.path / ENV_FILE

    def may_be_controlled_by(self, uid: int) -> bool:
        return uid in (0, self.owner_uid)


def make_queue_dir(queue_dir: Path) -> Path:
    return shared.make_dir(queue_dir, mode=QUEUE_MODE)


def reserve(
    queue_dir: Path, name: str, user: str, when: float | None = None
) -> tuple[str, Path]:
    make_queue_dir(queue_dir)
    return shared.reserve_dir(queue_dir, name, user, prefix="job", when=when)


SSH_UID_ENV = "SPARKS_SSH_UID"


def authenticated_uid() -> int | None:
    # Set by fire-ctl from `id -u` on the far side of the ssh connection. It is
    # the only identity in the chain sshd vouched for; a --user on the command
    # line is whatever the client typed, and is display only.
    raw = os.environ.get(SSH_UID_ENV)
    if raw is None or not raw.isdigit():
        return None

    return int(raw)


def commit(path: Path, job: Job) -> Entry:
    summary.write_atomically(
        path / JOB_FILE, lambda: json.dumps(job.to_dict(), indent=2) + "\n"
    )
    # After the write, never before: write_atomically replaces job.json with a
    # fresh file owned by whoever ran commit, and job.json is what owner_uid
    # reads. Without this the queue container's root owns the job, the
    # privilege drop has nothing to drop to, and the run is recorded as root.
    uid = authenticated_uid()
    if uid is not None and os.geteuid() == 0:
        for target in (path, path / JOB_FILE):
            with contextlib.suppress(OSError):
                os.chown(target, uid, -1)

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
    env: dict[str, str] | None = None,
    secret_names: list[str] | None = None,
) -> Entry:
    submitted = time.time() if when is None else when
    job_id, path = reserve(queue_dir, name, user, when=when)
    return commit(
        path,
        Job(
            job_id=job_id,
            name=shared.clean(name, "job"),
            user=shared.clean(user, "unknown"),
            command=[shared.clean(arg, "", limit=MAX_VALUE) for arg in command],
            submitted_unix=submitted,
            image=image,
            git_sha=git_sha,
            git_dirty=git_dirty,
            retry_of=retry_of,
            env=dict(env or {}),
            secret_names=list(secret_names or []),
        ),
    )


def env_from(pairs: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for pair in pairs:
        name, _, value = pair.partition("=")
        settings[shared.clean(name, "", limit=MAX_VALUE)] = shared.clean(
            value, "", limit=MAX_VALUE
        )
    return settings


def write_env(job_path: Path, values: dict[str, str]) -> Path:
    target = job_path / ENV_FILE
    summary.write_atomically(
        target, lambda: json.dumps(values, indent=2) + "\n", mode=ENV_MODE
    )
    # The same reason commit chowns job.json: write_atomically leaves the file
    # owned by whoever ran the verb, which inside the queue container is root,
    # and contain reads it after dropping to the submitter.
    uid = authenticated_uid()
    if uid is not None and os.geteuid() == 0:
        with contextlib.suppress(OSError):
            os.chown(target, uid, -1)
    return target


def read_env(env_path: Path) -> dict[str, str]:
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvError(
            f"{env_path} is this job's environment and could not be read: {exc}. "
            "Submit the job again rather than running it without one"
        ) from exc

    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise EnvError(f"{env_path} is not the JSON object sparks wrote") from exc

    if not isinstance(loaded, dict):
        raise EnvError(f"{env_path} is not the JSON object sparks wrote")

    return {str(name): str(value) for name, value in loaded.items()}


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
    return sorted(found, key=lambda entry: (entry.job.submitted_unix, entry.job.job_id))


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
