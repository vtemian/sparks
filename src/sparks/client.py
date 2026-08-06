"""Submitting to the queue and asking it what is going on.

Everything here runs on whichever machine the person is sitting at. When that is
not the box, one flag redirects the whole command over ssh rather than each verb
growing its own remote path: `sparks queue --host spark.local` is
`ssh spark.local sparks queue`, and the same is true of cancel, abort and
remove. Only `submit` is more than that, because only submit has to carry code
across.

Submitting is three steps and they are in this order for a reason:

1. reserve a job directory on the box, which is `mkdir` and therefore the atomic
   part - two people submitting in the same second get two directories
2. rsync the project into it, which takes as long as it takes
3. write the manifest, which is what makes the job visible to the runner

A runner that saw the job at step 2 would build half a source tree and record
the result as a real run.
"""

import getpass
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sparks import spool
from sparks.run import current_user, git_sha

LOG = logging.getLogger("sparks")

HOST_ENV = "SPARKS_HOST"

DOCKERIGNORE = ".dockerignore"

ALWAYS_EXCLUDED = (".git/", "__pycache__/", "*.pyc", ".venv/")
"""Never shipped, whatever the project says.

`.git` in particular: it is the largest thing in most checkouts, docker builds
never need it, and the provenance that would justify carrying it is recorded as
a field instead.
"""

RSYNC_TIMEOUT_SECONDS = 1800.0
SSH_TIMEOUT_SECONDS = 120.0


class ClientError(Exception):
    """Something the person can fix, reported without a traceback."""


@dataclass(frozen=True)
class Submitted:
    job_id: str
    path: str


def submit(
    queue_dir: Path,
    name: str,
    command: list[str],
    context: Path | None = None,
    image: str | None = None,
    retry_of: str | None = None,
    user: str | None = None,
) -> spool.Entry:
    """Put a job on a queue this machine can see."""
    if context is None and image is None:
        raise ClientError(
            "a job needs either a project to build (--context, which defaults "
            "to the current directory) or an image to run (--image)"
        )
    who = user or current_user()
    job_id, path = spool.reserve(queue_dir, name, who)
    sha, dirty = "unknown", False
    if context is not None:
        sha, dirty = provenance(context)
        ship(context, path / spool.CONTEXT_DIR)
    return spool.commit(
        path,
        spool.Job(
            job_id=job_id,
            name=name,
            user=who,
            command=command,
            submitted_unix=time.time(),
            git_sha=sha,
            git_dirty=dirty,
            retry_of=retry_of,
            image=image,
        ),
    )


def ship(context: Path, destination: Path) -> None:
    """Copy the project into the job directory.

    rsync rather than `shutil.copytree` for one reason that matters: it honours
    `.dockerignore`, so what is shipped is what the build would have used, and a
    project that already excludes its datasets does not have them copied.
    """
    if not context.is_dir():
        raise ClientError(f"{context} is not a directory")
    destination.mkdir(parents=True, exist_ok=True)
    argv = rsync_argv(context, str(destination))
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as e:
        raise ClientError("rsync is not installed, and submitting needs it") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"copying {context} took over 30 minutes") from e
    if done.returncode != 0:
        raise ClientError(
            f"could not copy {context}: {done.stderr.decode(errors='replace').strip()}"
        )


def rsync_argv(context: Path, destination: str) -> list[str]:
    """The trailing slash on the source is load-bearing: without it rsync
    creates `<destination>/<context name>/` instead of copying the contents."""
    argv = ["rsync", "--archive", "--delete"]
    for pattern in ALWAYS_EXCLUDED:
        argv += ["--exclude", pattern]
    ignore = context / DOCKERIGNORE
    if ignore.is_file():
        argv += [f"--exclude-from={ignore}"]
    return [*argv, f"{context}/", destination]


def provenance(context: Path) -> tuple[str, bool]:
    """The commit this was built from, and whether it had been edited since.

    Recorded, never enforced. Refusing to submit a dirty tree would be the
    friction this whole design exists to remove: experiments run dirty.
    """
    sha = git_sha(context)
    if sha == "unknown":
        return sha, False
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=context,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return sha, False
    return sha, bool(done.stdout.strip())


def retry(queue_dir: Path, entry: spool.Entry) -> spool.Entry:
    """Submit the same job again, reusing the context already on the box.

    A new job rather than a second attempt recorded inside the old one: "what
    did this job do" has to have one answer, and the link runs the other way,
    through `retry_of`.
    """
    if not entry.is_terminal:
        raise ClientError(
            f"{entry.job.job_id} is {entry.state.state}. Retrying a job that has "
            "not finished would run the same thing twice at once"
        )
    who = current_user()
    job_id, path = spool.reserve(queue_dir, entry.job.name, who)
    source = entry.context_dir
    if source.is_dir():
        _clone(source, path / spool.CONTEXT_DIR)
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
        raise ClientError("there are no jobs in the queue")
    exact = [e for e in found if e.job.job_id == needle]
    if exact:
        return exact[0]
    matches = [e for e in found if needle in e.job.job_id or e.job.name == needle]
    if not matches:
        raise ClientError(f"no job matches {needle!r}")
    if len(matches) > 1:
        live = [e for e in matches if not e.is_terminal]
        # A name that matches one running job and six finished ones is not
        # ambiguous in any way the person meant it.
        if len(live) == 1:
            return live[0]
        ids = "\n".join(f"    {e.job.job_id}  {e.state.state}" for e in matches)
        raise ClientError(f"{needle!r} matches several jobs:\n{ids}")
    return matches[0]


def ask(queue_dir: Path, needle: str, action: str) -> spool.Entry:
    """Cancel or abort, whichever applies once the runner looks at it."""
    entry = resolve(queue_dir, needle)
    if entry.is_terminal:
        raise ClientError(
            f"{entry.job.job_id} has already {entry.state.state}; there is "
            "nothing to stop"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ClientError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they can stop it"
        )
    spool.request(entry.path, action)
    return entry


def remove(queue_dir: Path, needle: str) -> spool.Entry:
    entry = resolve(queue_dir, needle)
    if not entry.is_terminal:
        raise ClientError(
            f"{entry.job.job_id} is {entry.state.state}. Stop it first: "
            f"sparks abort {entry.job.job_id}"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ClientError(f"{entry.job.job_id} belongs to {entry.job.user}")
    spool.remove(entry.path)
    return entry


HEADINGS = ("JOB", "USER", "STATE", "AGE", "RUN")


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

    A build context can be gigabytes and a retry does not change it, so copying
    the bytes again is pure waste. Hardlinks are safe here because nothing ever
    writes into a context after it is committed.
    """
    try:
        shutil.copytree(source, destination, copy_function=os.link)
    except OSError as e:
        LOG.info("sparks: could not hardlink the context (%s); copying instead", e)
        shutil.copytree(source, destination, dirs_exist_ok=True)


# -- reaching a box you are not sitting at -----------------------------------


def host_from(explicit: str | None) -> str | None:
    return explicit or os.environ.get(HOST_ENV) or None


def remote(host: str, argv: list[str]) -> int:
    """Run the same command on the box.

    One implementation for every verb, rather than each growing its own remote
    path. The command is passed as separate arguments so the remote shell does
    no word-splitting of its own on anything a person typed.
    """
    try:
        return subprocess.run(["ssh", host, "sparks", *argv], check=False).returncode
    except FileNotFoundError as e:
        raise ClientError("ssh is not installed") from e


def remote_capture(host: str, argv: list[str]) -> str:
    done = subprocess.run(
        ["ssh", host, "sparks", *argv],
        capture_output=True,
        text=True,
        timeout=SSH_TIMEOUT_SECONDS,
        check=False,
    )
    if done.returncode != 0:
        raise ClientError(f"{host} refused: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def submit_remote(
    host: str,
    name: str,
    command: list[str],
    context: Path,
    image: str | None = None,
) -> str:
    """Reserve on the box, ship the code, then commit. See the module docstring
    for why the manifest goes last."""
    reserved = remote_capture(host, ["reserve", "--name", name])
    if not reserved:
        raise ClientError(f"{host} did not say where to put the job")
    sha, dirty = provenance(context)
    argv = rsync_argv(context, f"{host}:{reserved}/{spool.CONTEXT_DIR}/")
    done = subprocess.run(
        argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
    )
    if done.returncode != 0:
        raise ClientError(
            f"could not copy the project to {host}: "
            f"{done.stderr.decode(errors='replace').strip()}"
        )
    commit = [
        "commit",
        reserved,
        "--name",
        name,
        "--user",
        local_user(),
        "--git-sha",
        sha,
    ]
    if dirty:
        commit.append("--git-dirty")
    if image:
        commit += ["--image", image]
    return remote_capture(host, [*commit, "--", *command])


def local_user() -> str:
    """Who is submitting, as known here.

    Passed to the box rather than read there: over ssh the box would see the ssh
    account, which is usually the same but is not the same *fact*. What decides
    privilege on the box is the uid that owns the files, not this.
    """
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")
