"""Submitting to the queue and asking it what is going on.

The laptop client always talks to the box over ssh. User-facing verbs require
`SPARKS_HOST` (or `--host`): `sparks queue --host spark.local` is
`ssh spark.local fire-ctl queue`, and the same pattern holds for cancel, abort,
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
REMOTE_BIN_ENV = "SPARKS_REMOTE"
DEFAULT_REMOTE_BIN = "fire-ctl"
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


# -- reaching a box you are not sitting at -----------------------------------


def host_from(explicit: str | None) -> str | None:
    return explicit or os.environ.get(HOST_ENV) or None


def remote_bin() -> str:
    return os.environ.get(REMOTE_BIN_ENV) or DEFAULT_REMOTE_BIN


def ssh_argv(host: str, argv: list[str]) -> list[str]:
    """`fire-ctl <argv>` on `host`, quoted to survive the trip.

    ssh does not take a command as a list. Whatever it is given it joins with
    spaces and hands to a shell on the far side, so passing arguments
    separately only LOOKS safe: `-c 'echo a; echo b'` arrives as three words and
    the semicolon is the remote shell's. The first job submitted from a laptop
    with a quoted command came back as a bash syntax error.

    Quoting here rather than trusting the caller, because the argument that
    breaks is always the one somebody typed.
    """
    return ["ssh", host, shlex.join([remote_bin(), *argv])]


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
    reserved = remote_capture(
        host, ["reserve", "--name", name, "--user", who]
    )
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
    """The last step of a remote submit, as a pure function."""
    argv = [
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
