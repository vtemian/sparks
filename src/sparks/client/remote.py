"""Submitting to the queue and asking it what is going on. Every user-facing
verb is `ssh <host> fire-ctl <verb>`; only submit is more than that, because
only submit builds an image, pushes it and carries `--data` across."""

import getpass
import logging
import os
import shlex
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sparks import dock, spool
from sparks.run import current_user, git_sha

LOG = logging.getLogger("sparks")

HOST_ENV = "SPARKS_HOST"
REMOTE_BIN_ENV = "SPARKS_REMOTE"
DEFAULT_REMOTE_BIN = "fire-ctl"
REMOTE_BOX_CONFIG = "/etc/sparks/box.toml"

DOCKERIGNORE = ".dockerignore"

ALWAYS_EXCLUDED = (".git/", "__pycache__/", "*.pyc", ".venv/")
"""Never uploaded with `--data`, whatever the tree says."""

RSYNC_TIMEOUT_SECONDS = 1800.0
SSH_TIMEOUT_SECONDS = 120.0

PUSH_HINT = "Is the registry in insecure-registries and is SPARKS_HOST reachable?"


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


def split_tag(tag: str) -> tuple[str, str]:
    """Repository and image tag, `latest` when the tag names none. Only a colon
    in the last segment splits: a registry port (`spark.local:5000/u/n`) is not
    a tag separator."""
    if ":" not in tag.rsplit("/", 1)[-1]:
        return tag, "latest"
    repository, image_tag = tag.rsplit(":", 1)
    return repository, image_tag


def build(context: Path, tag: str) -> None:
    if not (context / "Dockerfile").is_file():
        raise ClientError(f"{context}/Dockerfile is missing")
    try:
        docker_client = dock.client()
        stream = docker_client.api.build(
            path=str(context),
            tag=tag,
            decode=True,
            rm=True,
        )
        for chunk in stream:
            if "stream" in chunk:
                # No newline of our own: the chunks carry theirs.
                print(chunk["stream"], end="")
            raise_if_docker_failed(chunk, f"docker build failed for {tag}")
    except dock.DockerException as exc:
        raise ClientError(f"docker build failed for {tag}: {exc}") from exc


def push(tag: str) -> None:
    repository, image_tag = split_tag(tag)
    try:
        docker_client = dock.client()
        for line in docker_client.images.push(
            repository, tag=image_tag, stream=True, decode=True
        ):
            raise_if_docker_failed(line, f"docker push failed for {tag}. {PUSH_HINT}")
            status = line.get("status")
            if status:
                print(status)
    except dock.DockerException as exc:
        raise ClientError(f"docker push failed for {tag}. {PUSH_HINT}") from exc


def raise_if_docker_failed(chunk: dict[str, Any], message: str) -> None:
    """Docker streams report failure as chunks, not exceptions."""
    error = chunk.get("error")
    detail = chunk.get("errorDetail")
    if error or detail:
        raise ClientError(message)


def fetch_registry_url(host: str) -> str:
    raw = box_config(host)
    return registry_url(raw, f"{host}:{REMOTE_BOX_CONFIG}")


def box_config(host: str) -> bytes:
    try:
        done = subprocess.run(
            ["ssh", host, "cat", REMOTE_BOX_CONFIG],
            capture_output=True,
            timeout=SSH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ClientError("ssh is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClientError(f"timed out reading {REMOTE_BOX_CONFIG} from {host}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).decode(errors="replace").strip()
        raise ClientError(f"{host} refused: {detail}")
    return done.stdout


def registry_url(raw: bytes, where: str) -> str:
    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ClientError(f"{where} is not valid TOML: {exc}") from exc
    url = data.get("registry_url")
    if not isinstance(url, str) or not url.strip():
        raise ClientError(f"{where} has no registry_url")
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
    """Put a job on a queue this machine can see. `--image` skips build/push;
    otherwise build from `--context` and push to `registry_url`."""
    if not data.is_dir():
        raise ClientError(f"--data {data} is not a directory")
    who = user or current_user()
    sha, dirty = "unknown", False
    if context is not None:
        sha, dirty = provenance(context)
    tag = resolve_tag(
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


def resolve_tag(
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
    """Copy a local tree (usually `--data`) into a job directory on this
    machine."""
    if not source.is_dir():
        raise ClientError(f"{source} is not a directory")
    destination.mkdir(parents=True, exist_ok=True)
    argv = rsync_argv(source, str(destination))
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise ClientError("rsync is not installed, and submitting needs it") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClientError(f"copying {source} took over 30 minutes") from exc
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
    Recorded, never enforced."""
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


def host_from(explicit: str | None) -> str | None:
    return explicit or os.environ.get(HOST_ENV) or None


def is_configured(explicit: str | None = None) -> bool:
    return host_from(explicit) is not None


def remote_bin() -> str:
    return os.environ.get(REMOTE_BIN_ENV) or DEFAULT_REMOTE_BIN


def ssh_argv(host: str, argv: list[str]) -> list[str]:
    """`fire-ctl <argv>` on `host`, shlex-quoted here rather than by the caller.
    ssh joins whatever it is given with spaces and hands it to a shell on the
    far side, so an unquoted `;` would be the REMOTE shell's."""
    return ["ssh", host, shlex.join([remote_bin(), *argv])]


def run(host: str, argv: list[str]) -> int:
    """SSH `fire-ctl <argv>` on `host`; return the remote exit code."""
    try:
        return subprocess.run(ssh_argv(host, argv), check=False).returncode
    except FileNotFoundError as exc:
        raise ClientError("ssh is not installed") from exc


def capture(host: str, argv: list[str]) -> str:
    """SSH `fire-ctl <argv>` on `host`; return stdout (stripped)."""
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
    """Build/push (unless image given), reserve, ship data, then commit. The
    manifest goes last, so the runner never sees a half-copied `--data`. The
    build context stays on the laptop; the runner pulls the image."""
    if not data.is_dir():
        raise ClientError(f"--data {data} is not a directory")

    who = local_user()
    sha, dirty = provenance(context)
    if image is None and registry_url is None:
        registry_url = fetch_registry_url(host)
    tag = resolve_tag(
        image=image,
        context=context,
        registry_url=registry_url,
        user=who,
        name=name,
        sha=sha,
    )
    reserved = reserve(host, name, who)
    ship_to(data, host, reserved)
    return capture(host, commit_argv(reserved, name, command, sha, dirty, tag))


def reserve(host: str, name: str, who: str) -> str:
    reserved = capture(host, ["reserve", "--name", name, "--user", who])
    if not reserved:
        raise ClientError(f"{host} did not say where to put the job")
    return reserved


def ship_to(data: Path, host: str, reserved: str) -> None:
    """rsync `--data` into a job directory reserved on the box. The remote
    counterpart of `ship`, not a variant of it."""
    dest = f"{host}:{reserved}/{spool.DATA_DIR}/"
    argv = rsync_argv(data, dest)
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=RSYNC_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise ClientError("rsync is not installed, and submitting needs it") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClientError(f"copying {data} to {host} took over 30 minutes") from exc
    if done.returncode != 0:
        raise ClientError(
            f"could not copy --data to {host}: "
            f"{done.stderr.decode(errors='replace').strip()}"
        )


def commit_argv(
    reserved: str,
    name: str,
    command: list[str],
    sha: str,
    dirty: bool,
    image: str,
) -> list[str]:
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
    """Who is submitting, as known here, and passed to the box rather than read
    there. Display only: privilege on the box is the uid that owns the files."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- a missing account is not a failure
        return os.environ.get("USER", "unknown")
