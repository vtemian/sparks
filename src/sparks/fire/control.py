"""Queue control helpers — resolve, ask, retry, remove, render."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from sparks import box, spool

LOG = logging.getLogger("sparks")


class ControlError(Exception):
    """Something the caller can fix, reported without a traceback."""


HEADINGS = ("JOB", "USER", "STATE", "AGE", "RUN")


def queue_dir(shared_dir: Path | None = None) -> Path:
    if shared_dir is not None:
        return Path(shared_dir) / "queue"
    contract = box.load()
    if contract is None:
        raise ControlError(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )
    qd = contract.queue_dir
    if not qd.is_dir():
        raise ControlError(
            f"this box is provisioned for sparks but not for the queue: "
            f"{qd} does not exist"
        )
    return qd


def retry(queue_dir: Path, entry: spool.Entry) -> spool.Entry:
    """Submit the same job again, reusing the image and data already on the box.

    A new job rather than a second attempt recorded inside the old one: "what
    did this job do" has to have one answer, and the link runs the other way,
    through `retry_of`.
    """
    if not entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} is {entry.state.state}. Retrying a job that has "
            "not finished would run the same thing twice at once"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they "
            "can retry it (retry clones their data directory)"
        )
    who = entry.job.user
    job_id, path = spool.reserve(queue_dir, entry.job.name, who)
    source = entry.data_dir
    if source.is_dir():
        _clone(source, path / spool.DATA_DIR)
    return spool.commit(
        path,
        spool.Job(
            job_id=job_id,
            name=entry.job.name,
            user=who,
            command=list(entry.job.command),
            submitted_unix=time.time(),
            git_sha=entry.job.git_sha,
            git_dirty=entry.job.git_dirty,
            retry_of=entry.job.job_id,
            image=entry.job.image,
        ),
    )


def resolve(queue_dir: Path, needle: str) -> spool.Entry:
    """Find one job from whatever the person typed.

    A full id, a unique suffix of one, or a unique job name. Job ids are long
    and nobody retypes them; ambiguity is refused rather than guessed at,
    because the wrong guess here aborts somebody's training.
    """
    found = spool.entries(queue_dir)
    if not found:
        raise ControlError("there are no jobs in the queue")
    exact = [e for e in found if e.job.job_id == needle]
    if exact:
        return exact[0]
    matches = [e for e in found if needle in e.job.job_id or e.job.name == needle]
    if not matches:
        raise ControlError(f"no job matches {needle!r}")
    if len(matches) > 1:
        live = [e for e in matches if not e.is_terminal]
        # A name that matches one running job and six finished ones is not
        # ambiguous in any way the person meant it.
        if len(live) == 1:
            return live[0]
        ids = "\n".join(f"    {e.job.job_id}  {e.state.state}" for e in matches)
        raise ControlError(f"{needle!r} matches several jobs:\n{ids}")
    return matches[0]


def ask(queue_dir: Path, needle: str, action: str) -> spool.Entry:
    """Cancel or abort, whichever applies once the runner looks at it."""
    entry = resolve(queue_dir, needle)
    if entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} has already {entry.state.state}; there is "
            "nothing to stop"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they can stop it"
        )
    spool.request(entry.path, action)
    return entry


def remove(queue_dir: Path, needle: str) -> spool.Entry:
    entry = resolve(queue_dir, needle)
    if not entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} is {entry.state.state}. Stop it first: "
            f"sparks abort {entry.job.job_id}"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(f"{entry.job.job_id} belongs to {entry.job.user}")
    spool.remove(entry.path)
    return entry


def render(entries: list[spool.Entry], now: float | None = None) -> str:
    """The queue as a person reads it."""
    if not entries:
        return "the queue is empty\n"
    moment = time.time() if now is None else now
    rows = [
        (
            e.job.job_id,
            e.job.user,
            e.state.state,
            _age(e, moment),
            e.state.run_id or "",
        )
        for e in entries
    ]
    widths = [
        max(len(str(row[i])) for row in (HEADINGS, *rows)) for i in range(len(HEADINGS))
    ]
    lines = [_row(HEADINGS, widths), *(_row(r, widths) for r in rows)]
    return "".join(f"{line}\n" for line in lines)


def _row(values: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True)).rstrip()


def _age(entry: spool.Entry, now: float) -> str:
    """How long it has been in its current phase: waiting, or running."""
    since = entry.state.started_unix or entry.job.submitted_unix
    if entry.is_terminal and entry.state.finished_unix:
        since = entry.state.finished_unix
    return _duration(max(0.0, now - since))


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds // 86400)}d"


def _clone(source: Path, destination: Path) -> None:
    """Hardlink the tree where the filesystem allows it.

    A data folder can be gigabytes and a retry does not change it, so copying
    the bytes again is pure waste. Hardlinks are safe here because nothing ever
    writes into a committed job's `data/` after submit.
    """
    try:
        shutil.copytree(source, destination, copy_function=os.link)
    except OSError as e:
        LOG.info("sparks: could not hardlink data (%s); copying instead", e)
        shutil.copytree(source, destination, dirs_exist_ok=True)
