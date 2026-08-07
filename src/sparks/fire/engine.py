"""Docker, as the runner needs it: pull a registry image, then run it under
`python -m sparks.fire.supervise` as the account that submitted it. Runs as root
for the Docker socket, and drops to the submitter's uid for everything under it."""

import contextlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from sparks import dock, spool
from sparks.fire.runner import PullFailedError

LOG = logging.getLogger("sparks")

DOCKER_SOCKET = Path("/var/run/docker.sock")

PULL_TIMEOUT_SECONDS = 3600.0
"""The runner is single-threaded, so a hung pull is the whole queue stopped."""

CONTAINER_PREFIX = "sparks"
"""Job ids already begin with `job-`, so this is not `sparks-job`."""


@dataclass(frozen=True)
class Credentials:
    """Who to become before exec. All None is "stay as we are", which is what
    Popen means by these being unset and all a non-root runner can do."""

    user: int | None = None
    group: int | None = None
    extra_groups: list[int] | None = None


class Process:
    """A job in flight: the supervise process, and the container under it."""

    def __init__(
        self,
        child: subprocess.Popen[bytes],
        run_id_file: Path,
        cidfile: Path,
        log: IO[bytes],
    ) -> None:
        self._child = child
        self._run_id_file = run_id_file
        self._cidfile = cidfile
        self._log = log

    def poll(self) -> int | None:
        return self._child.poll()

    def terminate(self) -> None:
        """SIGTERM to the supervisor, never to the container directly: being
        signalled is what makes a run `cancelled` rather than `finished`."""
        with contextlib.suppress(ProcessLookupError):
            self._child.terminate()

    def run_id(self) -> str | None:
        return first_line(self._run_id_file)

    def container_id(self) -> str | None:
        return first_line(self._cidfile)

    def finish(self) -> None:
        """Release what the job leaves behind. contain removes the container in
        its own `finally`, so this is for a client that died without it and left
        the container holding the GPU."""
        with contextlib.suppress(OSError, ValueError):
            self._log.close()
        container = self.container_id()
        if not container:
            return
        dock.remove_quietly(container)


def pull_line(chunk: dict[str, Any], log: IO[bytes], log_path: Path) -> None:
    """The write comes before the raise: the failure message points the reader at
    the log, so the error line has to be in it."""
    line = chunk.get("status") or chunk.get("error") or str(chunk)
    log.write((line + "\n").encode())
    if chunk.get("error") or chunk.get("errorDetail"):
        raise PullFailedError(f"docker pull failed; the output is in {log_path.name}")


@dataclass
class Docker:
    shared_dir: Path
    url: str
    """Prometheus as reachable FROM A CONTAINER: the box contract's URL is
    loopback, which in here is this container, so the queue passes down the
    host-gateway form."""
    supervise_module: str = "sparks.fire.supervise"
    gpus: str = "all"
    """Passed to contain as `--gpus`, or empty to omit GPU requests entirely: on
    a daemon with no nvidia runtime `--gpus all` is a hard error, not an ignored
    flag, so a box without a GPU could not run a job at all."""
    extra_groups: list[int] = field(default_factory=list)

    def pull(self, image: str, log_path: Path) -> None:
        """Pull a registry image so a missing or broken ref fails the job here,
        rather than inside contain where the launch log is the only trace."""
        deadline = time.monotonic() + PULL_TIMEOUT_SECONDS
        try:
            client = dock.client()
            with log_path.open("wb") as log:
                stream = client.api.pull(image, stream=True, decode=True)
                for chunk in stream:
                    if time.monotonic() > deadline:
                        raise PullFailedError(
                            f"the pull was still going after "
                            f"{PULL_TIMEOUT_SECONDS / 3600:g}h and was stopped"
                        )
                    pull_line(chunk, log, log_path)
        except dock.DockerException as exc:
            raise PullFailedError(f"could not pull image: {exc}") from exc

    def start(self, entry: spool.Entry, image: str, log_path: Path) -> Process:
        uid = entry.owner_uid
        gid = entry.path.stat().st_gid
        cidfile = entry.path / spool.CID_FILE
        run_id_file = entry.path / spool.RUN_ID_FILE
        # A retried job's leftovers would be read as this attempt's ids.
        for stale in (cidfile, run_id_file):
            stale.unlink(missing_ok=True)
        argv = self.argv(entry, image, cidfile, run_id_file, uid, gid)
        credentials = self.credentials(uid, gid)
        LOG.info("sparks: starting %s as uid %d", entry.job.job_id, uid)
        log = log_path.open("wb")
        child = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                # Not the queue container's root-owned HOME, which the submitter
                # cannot write to and which several toolchains will try to.
                "HOME": str(entry.path),
                "PYTHONUNBUFFERED": "1",
            },
            user=credentials.user,
            group=credentials.group,
            extra_groups=credentials.extra_groups,
        )
        return Process(child, run_id_file, cidfile, log)

    def credentials(self, uid: int, gid: int) -> "Credentials":
        """How to become the submitter, if becoming anybody is possible: only
        root can change uid, and only the queue container runs as root."""
        if os.geteuid() != 0:
            if os.geteuid() != uid:
                LOG.warning(
                    "sparks: running as uid %d, so this job cannot be run as "
                    "its owner (uid %d) and its run will be recorded under the "
                    "wrong account",
                    os.geteuid(),
                    uid,
                )
            return Credentials()
        return Credentials(
            user=uid,
            group=gid,
            # setuid gives them their primary group and no others, so every group
            # the job needs is named here: docker to reach the socket, and the
            # shared group to write the run directory.
            extra_groups=self.groups() or None,
        )

    def groups(self) -> list[int]:
        configured = list(self.extra_groups)
        shared = shared_group(self.shared_dir)
        if shared is not None and shared not in configured:
            configured.append(shared)
        return configured

    def argv(
        self,
        entry: spool.Entry,
        image: str,
        cidfile: Path,
        run_id_file: Path,
        uid: int,
        gid: int,
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            self.supervise_module,
            "--url",
            self.url,
            "--name",
            entry.job.name,
            "--shared-dir",
            str(self.shared_dir),
            # The PROJECT's commit, captured at submit time: the default would be
            # the framework's own, since that is where this process runs from.
            "--git-sha",
            entry.job.git_sha,
            "--run-id-file",
            str(run_id_file),
            "--",
            *self.container_argv(entry, image, cidfile, uid, gid),
        ]

    def container_argv(
        self, entry: spool.Entry, image: str, cidfile: Path, uid: int, gid: int
    ) -> list[str]:
        """Every mount and flag is chosen by the queue; the job file says only
        what image and what command. A job that could name its own `--volume`
        could mount `/` and be root on the box."""
        return [
            sys.executable,
            "-m",
            "sparks.fire.contain",
            "--name",
            f"{CONTAINER_PREFIX}-{entry.job.job_id}",
            "--cidfile",
            str(cidfile),
            *(["--gpus", self.gpus] if self.gpus else []),
            "--user",
            f"{uid}:{gid}",
            "--shared-dir",
            str(self.shared_dir),
            "--data-dir",
            str(entry.data_dir),
            "--workdir",
            str(self.shared_dir),
            # A job image that looks like a flag must not be parsed as one.
            "--",
            image,
            *entry.job.command,
        ]

    def release(self, container_id: str) -> None:
        dock.remove_quietly(container_id)


def shared_group(shared_dir: Path) -> int | None:
    try:
        return shared_dir.stat().st_gid
    except OSError as exc:
        LOG.warning("sparks: cannot read %s: %s", shared_dir, exc)
        return None


def docker_group() -> int | None:
    """Read off the socket rather than looked up by the name `docker`: inside a
    container the group's name may not resolve at all, while its gid always does."""
    try:
        return DOCKER_SOCKET.stat().st_gid
    except OSError as exc:
        LOG.warning("sparks: cannot read %s: %s", DOCKER_SOCKET, exc)
        return None


def first_line(path: Path) -> str | None:
    """The file's first line, or None if it is not there yet: both files are
    written by something else while we watch, so absent is not an error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else None
