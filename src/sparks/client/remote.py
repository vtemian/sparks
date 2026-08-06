"""Submitting to the queue and asking it what is going on.

The laptop client always talks to the box over ssh. User-facing verbs require
`SPARKS_HOST` (or `--host`): `sparks queue --host spark.local` is
`ssh spark.local sparks _queue`, and the same pattern holds for cancel, abort,
retry and remove. Only `submit` is more than a thin ssh, because only submit
has to build an image, push it, and carry a data folder across.

Submitting is five steps and they are in this order for a reason:

1. build the image on the laptop (unless `--image` was given)
2. push it to the box registry
3. reserve a job directory on the box, which is `mkdir` and therefore the atomic
   part - two people submitting in the same second get two directories
4. rsync `--data` into `job/data/`, which takes as long as it takes
5. write the manifest with the image ref, which is what makes the job visible

A runner that saw the job at step 4 would mount a half-copied data tree and
record the result as a real run.
"""

import getpass
import logging
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sparks import spool
from sparks.run import current_user, git_sha

LOG = logging.getLogger("sparks")

HOST_ENV = "SPARKS_HOST"
REMOTE_BOX_CONFIG = "/etc/sparks/box.toml"

DOCKERIGNORE = ".dockerignore"

ALWAYS_EXCLUDED = (".git/", "__pycache__/", "*.pyc", ".venv/")
"""Never uploaded with `--data`, whatever the tree says.

`.git` in particular: it is the largest thing in most checkouts, jobs never need
it, and the provenance that would justify carrying it is recorded as a field
instead.
"""

RSYNC_TIMEOUT_SECONDS = 1800.0
SSH_TIMEOUT_SECONDS = 120.0


class ClientError(Exception):
    """Something the person can fix, reported without a traceback."""


@dataclass(frozen=True)
class Submitted:
    job_id: str
    path: str


def tag_for(registry_url: str, user: str, name: str, ref: str) -> str:
    """Docker tag `host:port/user/name:ref`, scheme stripped from registry_url."""
    parsed = urlparse(registry_url)
    host = parsed.netloc or parsed.path
    host = host.rstrip("/")
    if not host:
        raise ClientError(f"registry_url {registry_url!r} has no host")
    return f"{host}/{user}/{name}:{ref}"


def build(context: Path, tag: str) -> None:
    if not (context / "Dockerfile").is_file():
        raise ClientError(f"{context}/Dockerfile is missing")
    done = subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=False,
    )
    if done.returncode != 0:
        raise ClientError(f"docker build failed for {tag}")


def push(tag: str) -> None:
    done = subprocess.run(["docker", "push", tag], check=False)
    if done.returncode != 0:
        raise ClientError(
            f"docker push failed for {tag}. Is the registry in "
            f"insecure-registries and is SPARKS_HOST reachable?"
        )


def fetch_registry_url(host: str) -> str:
    """Read `registry_url` from the box contract over ssh."""
    try:
        done = subprocess.run(
            ["ssh", host, "cat", REMOTE_BOX_CONFIG],
            capture_output=True,
            timeout=SSH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as e:
        raise ClientError("ssh is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"timed out reading {REMOTE_BOX_CONFIG} from {host}") from e
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).decode(errors="replace").strip()
        raise ClientError(f"{host} refused: {detail}")
    try:
        data = tomllib.loads(done.stdout.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ClientError(f"{host}:{REMOTE_BOX_CONFIG} is not valid TOML: {e}") from e
    url = data.get("registry_url")
    if not isinstance(url, str) or not url.strip():
        raise ClientError(f"{host}:{REMOTE_BOX_CONFIG} has no registry_url")
    return url.strip()


def submit(
    queue_dir: Path,
    name: str,
    command: list[str],
    data: Path,
    context: Path | None = None,
    image: str | None = None,
    registry_url: str | None = None,
    retry_of: str | None = None,
    user: str | None = None,
) -> spool.Entry:
    """Put a job on a queue this machine can see.

    Requires `--data`. Pass `--image` to skip build/push (useful when testing on
    the box without a registry); otherwise build from `--context` and push to
    `registry_url`.
    """
    if not data.is_dir():
        raise ClientError(f"--data {data} is not a directory")
    who = user or current_user()
    sha, dirty = "unknown", False
    if context is not None:
        sha, dirty = provenance(context)
    tag = _resolve_tag(
        image=image,
        context=context,
        registry_url=registry_url,
        user=who,
        name=name,
        sha=sha,
    )
    job_id, path = spool.reserve(queue_dir, name, who)
    ship(data, path / spool.DATA_DIR)
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
            image=tag,
        ),
    )


def _resolve_tag(
    *,
    image: str | None,
    context: Path | None,
    registry_url: str | None,
    user: str,
    name: str,
    sha: str,
) -> str:
    if image is not None:
        return image
    if context is None:
        raise ClientError(
            "a job needs either an image to run (--image) or a project to "
            "build (--context, which defaults to the current directory)"
        )
    if not registry_url:
        raise ClientError(
            "building an image needs a registry_url (from the box contract) "
            "or pass --image to skip build/push"
        )
    ref = sha if sha != "unknown" else "latest"
    tag = tag_for(registry_url, user, name, ref)
    build(context, tag)
    push(tag)
    return tag


def ship(source: Path, destination: Path) -> None:
    """Copy a local tree (usually `--data`) into a job directory.

    rsync rather than `shutil.copytree` so `.dockerignore` exclusions still
    apply when a data tree happens to carry one, and so always-excluded paths
    like `.git/` never ride along.
    """
    if not source.is_dir():
        raise ClientError(f"{source} is not a directory")
    destination.mkdir(parents=True, exist_ok=True)
    argv = rsync_argv(source, str(destination))
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as e:
        raise ClientError("rsync is not installed, and submitting needs it") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"copying {source} took over 30 minutes") from e
    if done.returncode != 0:
        raise ClientError(
            f"could not copy {source}: {done.stderr.decode(errors='replace').strip()}"
        )


def rsync_argv(source: Path, destination: str) -> list[str]:
    """The trailing slash on the source is load-bearing: without it rsync
    creates `<destination>/<source name>/` instead of copying the contents."""
    argv = ["rsync", "--archive", "--delete"]
    for pattern in ALWAYS_EXCLUDED:
        argv += ["--exclude", pattern]
    ignore = source / DOCKERIGNORE
    if ignore.is_file():
        argv += [f"--exclude-from={ignore}"]
    return [*argv, f"{source}/", destination]


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
    """Submit the same job again, reusing the image and data already on the box.

    A new job rather than a second attempt recorded inside the old one: "what
    did this job do" has to have one answer, and the link runs the other way,
    through `retry_of`.
    """
    if not entry.is_terminal:
        raise ClientError(
            f"{entry.job.job_id} is {entry.state.state}. Retrying a job that has "
            "not finished would run the same thing twice at once"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ClientError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they "
            "can retry it (retry clones their data directory)"
        )
    who = current_user()
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

    A data folder can be gigabytes and a retry does not change it, so copying
    the bytes again is pure waste. Hardlinks are safe here because nothing ever
    writes into a committed job's `data/` after submit.
    """
    try:
        shutil.copytree(source, destination, copy_function=os.link)
    except OSError as e:
        LOG.info("sparks: could not hardlink data (%s); copying instead", e)
        shutil.copytree(source, destination, dirs_exist_ok=True)


# -- reaching a box you are not sitting at -----------------------------------


def host_from(explicit: str | None) -> str | None:
    return explicit or os.environ.get(HOST_ENV) or None


def ssh_argv(host: str, argv: list[str]) -> list[str]:
    """`sparks <argv>` on `host`, quoted to survive the trip.

    ssh does not take a command as a list. Whatever it is given it joins with
    spaces and hands to a shell on the far side, so passing arguments
    separately only LOOKS safe: `-c 'echo a; echo b'` arrives as three words and
    the semicolon is the remote shell's. The first job submitted from a laptop
    with a quoted command came back as a bash syntax error.

    Quoting here rather than trusting the caller, because the argument that
    breaks is always the one somebody typed.
    """
    return ["ssh", host, shlex.join(["sparks", *argv])]


def remote(host: str, argv: list[str]) -> int:
    """Run the same command on the box.

    One implementation for every verb, rather than each growing its own remote
    path.
    """
    try:
        return subprocess.run(ssh_argv(host, argv), check=False).returncode
    except FileNotFoundError as e:
        raise ClientError("ssh is not installed") from e


def remote_capture(host: str, argv: list[str]) -> str:
    done = subprocess.run(
        ssh_argv(host, argv),
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
    *,
    name: str,
    command: list[str],
    context: Path,
    data: Path,
    image: str | None = None,
    registry_url: str | None = None,
) -> str:
    """Build/push (unless image given), reserve, ship data, then commit.

    See the module docstring for why the manifest goes last. The Docker build
    context stays on the laptop; only `--data` is rsynced — the runner pulls
    the image.
    """
    if not data.is_dir():
        raise ClientError(f"--data {data} is not a directory")
    who = local_user()
    sha, dirty = provenance(context)
    url = registry_url
    if image is None and url is None:
        url = fetch_registry_url(host)
    tag = _resolve_tag(
        image=image,
        context=context,
        registry_url=url,
        user=who,
        name=name,
        sha=sha,
    )
    reserved = remote_capture(host, ["reserve", "--name", name])
    if not reserved:
        raise ClientError(f"{host} did not say where to put the job")
    dest = f"{host}:{reserved}/{spool.DATA_DIR}/"
    argv = rsync_argv(data, dest)
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as e:
        raise ClientError("rsync is not installed, and submitting needs it") from e
    except subprocess.TimeoutExpired as e:
        raise ClientError(f"copying {data} to {host} took over 30 minutes") from e
    if done.returncode != 0:
        raise ClientError(
            f"could not copy --data to {host}: "
            f"{done.stderr.decode(errors='replace').strip()}"
        )
    return remote_capture(host, commit_argv(reserved, name, command, sha, dirty, tag))


def commit_argv(
    reserved: str,
    name: str,
    command: list[str],
    sha: str,
    dirty: bool,
    image: str,
) -> list[str]:
    """The last step of a remote submit, as a pure function.

    Note what is NOT here: `--user`. Whoever ssh'd in owns the job's files, and
    ownership is what decides who may abort it; the account on this laptop
    decides nothing and is frequently a different name. Passing it made the
    queue list a job as `whitemonk` while the run underneath said `vlad` - one
    person showing up as two, in the record that says who did what.
    """
    argv = ["commit", reserved, "--name", name, "--git-sha", sha]
    if dirty:
        argv.append("--git-dirty")
    argv += ["--image", image]
    return [*argv, "--", *command]


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
