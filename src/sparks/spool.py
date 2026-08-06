"""The queue on disk: one directory per job, readable by anything, owned by the
account that submitted it.

A submitted job outlives the laptop that submitted it, the runner that picks it
up and a reboot in between, so the queue is a directory tree rather than a
process's memory. Everything a runner needs to decide what to do next is in
these files, and a runner that starts cold reconstructs the whole queue by
reading them.

Two files per job, with exactly one writer each:

- `job.json` is the submission. The client writes it once and nothing ever
  modifies it, so what was asked for stays legible after any amount of retrying.
- `state.json` is everything that changes. Only the runner writes it.

That split is why nothing here locks. The alternative - one file both sides
update - would need `shared.exclusive` on every state change and on every poll
of every job, and would still leave a client able to corrupt a running job's
record by racing the runner.

`job.json` is written LAST, after `--data` has finished streaming into the
directory, and its presence is what makes the job visible. A runner that
picked a job up mid-rsync would mount a half-copied data tree and record the
result as a real run.
"""

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
"""Legacy name. Jobs no longer ship a build context; kept so tests can assert
the path stays absent and old spool trees remain recognisable."""
DATA_DIR = "data"
PULL_LOG = "pull.log"
LAUNCH_LOG = "launch.log"
"""What the supervisor said when the job container started. Separate from
`pull.log` (registry pull) and from the run's own `output.log`, which lives
with the run rather than with the job."""
CID_FILE = "container.id"
RUN_ID_FILE = "run_id"
REQUESTS_DIR = "requests"

QUEUE_MODE = 0o3775
"""setgid, group-writable, and STICKY.

The sticky bit is the whole reason this is not `shared.DIR_MODE`: group-write on
a directory is permission to unlink anything inside it, whoever owns it, so
without sticky either colleague could delete the other's queued job by accident.
With it, only the owner (and root, which is the runner) can.
"""

QUEUED = "queued"
BUILDING = "building"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
ABORTED = "aborted"
UNKNOWN = "unknown"

TERMINAL = frozenset({FINISHED, FAILED, CANCELLED, ABORTED})
"""Where a job stops.

Deliberately not `summary.STATUSES`. A job that fails to build produced no run
at all, and no run status can say that. `failed` covers a build that did not
produce an image and a container that exited non-zero; `aborted` is somebody
stopping a job that had already started, which the run underneath records as
`cancelled` because that is what happened to the *process*.
"""

CANCEL = "cancel"
ABORT = "abort"
ACTIONS = frozenset({CANCEL, ABORT})

RETENTION_SECONDS = 6 * 3600.0
"""How long a finished job keeps a row in the metrics file.

Short on purpose. The run it produced is in `sparks_runs.prom` permanently, so
nothing is lost by ageing this out, and the queue's job is to answer "what is
happening and what just happened" rather than to be a second history.
"""


@dataclass(frozen=True)
class Job:
    """The submission. Immutable in the strongest sense available: written once,
    by the client, and never opened for writing again."""

    job_id: str
    name: str
    user: str
    """Who submitted it, for display. Never for authorisation - see `owner_uid`,
    which the filesystem vouches for and this does not."""
    command: list[str]
    submitted_unix: float
    image: str
    """Registry ref the runner will pull."""
    git_sha: str = "unknown"
    git_dirty: bool = False
    """Whether the tree had uncommitted edits when it was shipped. Recorded
    rather than refused: experiments run dirty, and a submit that demanded a
    commit first would be the friction this whole design exists to remove."""
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
    """Everything about a job that changes, written only by the runner."""

    state: str = QUEUED
    image: str | None = None
    """The digest actually pulled, which is the reproducibility record: a tag
    can be re-pointed, a digest cannot."""
    run_id: str | None = None
    """The run the supervisor created, which is the join to `sparks_run_info` and
    to everything the dashboards already know how to show."""
    container_id: str | None = None
    started_unix: float | None = None
    finished_unix: float | None = None
    exit_code: int | None = None
    detail: str | None = None
    """Why it ended this way, when the state alone does not say. A pull error,
    or who aborted it."""

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
    """Somebody asking the runner to do something to a job."""

    action: str
    uid: int
    """From `stat`, never from the file's name or contents. You cannot create a
    file owned by another account, so this is the one identity claim here that
    does not have to be trusted."""
    path: Path


@dataclass(frozen=True)
class Entry:
    """A job directory, as read."""

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
        """Whether `uid` may cancel, abort or remove this job.

        root passes because the runner acts on everyone's behalf. Everybody else
        must own the job: a shared queue where a colleague can kill your
        overnight training by mistake is worse than no queue.
        """
        return uid in (0, self.owner_uid)


def make_queue_dir(queue_dir: Path) -> Path:
    return shared.make_dir(queue_dir, mode=QUEUE_MODE)


def reserve(
    queue_dir: Path, name: str, user: str, when: float | None = None
) -> tuple[str, Path]:
    """Claim a job id by creating its directory, before anything is written."""
    make_queue_dir(queue_dir)
    return shared.reserve_dir(queue_dir, name, user, prefix="job", when=when)


def commit(path: Path, job: Job) -> Entry:
    """Make a reserved job visible by writing its manifest.

    Called after the build context is fully in place. Atomic, so a runner
    scanning mid-write sees no job rather than an unparseable one.
    """
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
    """Reserve and commit in one step, for a job whose context is already there
    or which does not need one."""
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
    """One job directory. Raises if the manifest is missing or unreadable, which
    is how `entries` tells a job from any other directory."""
    manifest = path / JOB_FILE
    with manifest.open(encoding="utf-8") as f:
        job = Job.from_dict(json.load(f))
    return Entry(
        job=job,
        state=_read_state(path),
        path=path,
        owner_uid=manifest.stat().st_uid,
    )


def entries(queue_dir: Path) -> list[Entry]:
    """Every readable job, oldest submission first.

    Unreadable ones are skipped rather than raised on. This is called on every
    tick of the runner's loop, so an exception here is a queue that stops
    picking anything up because of one bad directory - and the job that broke it
    would be the one nobody could remove.
    """
    found = []
    for path in sorted(queue_dir.glob(f"*/{JOB_FILE}")):
        try:
            found.append(load(path.parent))
        except (OSError, ValueError, KeyError, TypeError) as e:
            LOG.warning("sparks: skipping unreadable job %s: %s", path.parent, e)
    return sorted(found, key=lambda e: (e.job.submitted_unix, e.job.job_id))


def next_queued(queue_dir: Path) -> Entry | None:
    """The job to start now, or None.

    Strictly `QUEUED`: a job whose state file could not be parsed is skipped,
    because the states it might be hiding include `running`.
    """
    return next((e for e in entries(queue_dir) if e.state.state == QUEUED), None)


def publishable(
    queue_dir: Path, now: float | None = None, retention: float = RETENTION_SECONDS
) -> list[Entry]:
    """The jobs worth a row in the metrics file.

    Everything live, whatever its age - a week-old run still going is the most
    important row there is - plus anything that finished recently enough to
    still be the answer to "what just happened".
    """
    moment = time.time() if now is None else now
    keep = []
    for entry in entries(queue_dir):
        if not entry.is_terminal:
            keep.append(entry)
            continue
        finished = entry.state.finished_unix
        # An undated terminal job is kept: it cannot be aged out honestly, and
        # one stuck row is cheaper than silently dropping a real outcome.
        if finished is None or moment - finished <= retention:
            keep.append(entry)
    return keep


def set_state(path: Path, state: State) -> None:
    """Record where a job has got to. The runner is the only caller."""
    summary.write_atomically(
        path / STATE_FILE, lambda: json.dumps(state.to_dict(), indent=2) + "\n"
    )


def advance(path: Path, **changes: Any) -> State:
    """Update some fields of a job's state, leaving the rest alone."""
    state = replace(_read_state(path), **changes)
    set_state(path, state)
    return state


def request(path: Path, action: str) -> Path:
    """Ask the runner to do something to this job.

    One file per requester per action, so two people asking at once do not
    collide and neither can block the other by getting there first. Repeating a
    request is a no-op: somebody who does not see the job stop will press it
    again, and that must not be an error.
    """
    if action not in ACTIONS:
        raise ValueError(f"{action!r} is not one of {sorted(ACTIONS)}")
    directory = shared.make_dir(path / REQUESTS_DIR)
    target = directory / f"{action}.{os.getuid()}"
    # Not write_atomically: the content is irrelevant, the file's existence and
    # its ownership are the whole message, and a rename would land it owned by
    # whoever wrote the temp file - which is the same account, but relying on
    # that is how this stops being obvious.
    target.touch(mode=shared.FILE_MODE, exist_ok=True)
    return target


def requests(path: Path) -> list[Request]:
    """Everything asked of this job and not yet handled."""
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
    """Delete a job and its build context."""
    shutil.rmtree(path)


def _read_state(path: Path) -> State:
    """The runner's file, or a default.

    Absent means queued: the runner has not touched the job yet, which is
    exactly what queued means, and writing a state file at submit time would
    give the client a second file to own.

    Unreadable means `UNKNOWN`, and emphatically not queued. Defaulting a
    corrupt state to queued would start a second container for a job already
    running, which is the one mistake here with a hardware cost.
    """
    try:
        with (path / STATE_FILE).open(encoding="utf-8") as f:
            return State.from_dict(json.load(f))
    except FileNotFoundError:
        return State()
    except (OSError, ValueError, TypeError) as e:
        LOG.warning(
            "sparks: %s has an unreadable state, so it will not be started: %s",
            path.name,
            e,
        )
        return State(state=UNKNOWN)
