"""Docker, as the runner needs it: pull a registry image, then run it under
`python -m sparks.fire.supervise` as the account that submitted it.

The nesting is deliberate and reads oddly the first time:

    python -m sparks.fire.supervise -- python -m sparks.fire.contain ...
        <image> <command>

Supervision stays OUTSIDE the training container so that a project's image does
not have to contain sparks. The alternative - sparks as every image's entrypoint
- would put this framework in the dependency list of every Dockerfile anyone
writes, which fails the only test that matters for the thing people have to
write themselves.

The price is that this process needs what the energy sampler reads: `/sys`
mounted read-only for hwmon and `--gpus all` for NVML. Both are read-only uses,
and the queue container gets them from sparkup.

Privilege runs the other way from what the nesting suggests. This module runs as
root, because talking to the Docker socket is root-equivalent, and it drops to
the submitter's uid for the supervisor and everything under it. The uid comes
from `stat()` on the job manifest, never from a field inside it.
"""

import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from sparks import dock, spool
from sparks.fire.runner import PullFailedError

LOG = logging.getLogger("sparks")

DOCKER_SOCKET = Path("/var/run/docker.sock")

PULL_TIMEOUT_SECONDS = 3600.0
"""A pull that has taken an hour is not going to finish. Bounded because the
runner is single-threaded: a hung pull is the whole queue stopped."""

CONTAINER_PREFIX = "sparks"
"""Job ids already begin with `job-`, so this is not `sparks-job`: that produced
containers called `sparks-job-job-...`."""


@dataclass(frozen=True)
class Credentials:
    """Who to become before exec, or nobody.

    All None is the honest "stay as we are", which is what Popen means by these
    being unset, and is what a runner not running as root has to do.
    """

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
        """SIGTERM to the supervisor, and nothing else.

        Deliberately not to the container directly: the supervisor's whole
        contract is that being signalled is what makes a run `cancelled` rather
        than `finished`, and killing the container behind its back would record
        a training that stopped for no reason anyone can see. It forwards the
        signal down, escalates to SIGKILL on its own 30s schedule, and sweeps
        whatever is left.
        """
        with contextlib.suppress(ProcessLookupError):
            self._child.terminate()

    def run_id(self) -> str | None:
        return _first_line(self._run_id_file)

    def container_id(self) -> str | None:
        return _first_line(self._cidfile)

    def finish(self) -> None:
        """Release what the job leaves behind.

        contain removes the container in its own `finally`, so this is for the
        case where the client died without it: the container then outlives its
        supervisor and keeps the GPU. That is the same class of problem as the
        orphaned workers `process.py` sweeps for, one level up.
        """
        with contextlib.suppress(OSError, ValueError):
            self._log.close()
        container = self.container_id()
        if not container:
            return
        dock.remove_quietly(dock.client(), container)


@dataclass
class Docker:
    """The real engine. Holds only what every job needs told to it."""

    shared_dir: Path
    url: str
    """Prometheus as reachable FROM A CONTAINER. The box contract's URL is
    loopback, which inside here is this container, so the queue is configured
    with the host-gateway form and passes the same one down."""
    supervise_module: str = "sparks.fire.supervise"
    gpus: str = "all"
    """Passed to contain as `--gpus`, or empty to omit GPU requests entirely.

    Omitting matters: on a daemon with no nvidia runtime `--gpus all` is not
    ignored, it is a hard error, so a box without a GPU could not run a job at
    all. That box is a real one - it is the laptop this was written on.
    """
    extra_groups: list[int] = field(default_factory=list)

    def pull(self, image: str, log_path: Path) -> None:
        """Pull a registry image so a missing or broken ref fails the job here.

        Without this, contain is what discovers the problem, and the launch
        log is a worse place to look than a dedicated pull failure.
        """
        deadline = time.monotonic() + PULL_TIMEOUT_SECONDS
        try:
            client = dock.client()
            with log_path.open("wb") as log:
                stream = client.api.pull(image, stream=True, decode=True)
                for chunk in _bounded(stream, deadline):
                    _pull_line(chunk, log, log_path)
        except dock.DockerException as e:
            raise PullFailedError(f"could not pull image: {e}") from e

    def start(self, entry: spool.Entry, image: str, log_path: Path) -> Process:
        """Start the job, as its owner."""
        uid = entry.owner_uid
        gid = entry.path.stat().st_gid
        cidfile = entry.path / spool.CID_FILE
        run_id_file = entry.path / spool.RUN_ID_FILE
        # Clear stale control files from a retried or resumed job so readers
        # never see a previous container id or run id.
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
        """How to become the submitter, if becoming anybody is possible.

        Only root can change uid, and in the queue container we are root -
        talking to the Docker socket requires it. Everywhere else this returns
        nothing and the job runs as whoever started the runner.

        Passed to Popen rather than done in a `preexec_fn`: CPython performs it
        between fork and exec itself, which is the same moment without the
        fork-safety caveats of running arbitrary Python there.
        """
        if os.geteuid() != 0:
            if os.geteuid() != uid:
                # Not fatal, but the run directory will be owned by the wrong
                # person, and on a shared box that is how a colleague ends up
                # unable to read their own run.
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
            # setuid gives them their primary group and no others, so every
            # group the job needs has to be named here: docker, to reach the
            # socket and start its own container, and the shared group, to write
            # the run directory.
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
        """The whole nested command, as a pure function so it can be read and
        tested without a daemon."""
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
            # The PROJECT's commit, captured by the client at submit time. The
            # default would be the framework's own, because that is the
            # directory this process runs from.
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
        """`python -m sparks.fire.contain` for one job.

        Every mount and flag here is chosen by the queue. The job file says what
        image and what command, and nothing else: a job that could name its own
        `--volume` could mount `/` and be root on the box, which is the single
        thing this list is guarding.
        """
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
            # End of contain's own options: a job image that looks like a flag
            # must not be parsed as one.
            "--",
            image,
            *entry.job.command,
        ]

    def release(self, container_id: str) -> None:
        """Remove a container nothing is supervising any more.

        Used when a runner starts and finds a job the previous one was in the
        middle of: a container outlives the client that started it, so without
        this a restart of the queue leaves the GPU held by a run whose record
        already says it ended.
        """
        dock.remove_quietly(dock.client(), container_id)


def shared_group(shared_dir: Path) -> int | None:
    """The group that owns the shared tree.

    Needed because `setuid` to a submitter gives them their PRIMARY group and
    nothing else: the supplementary groups their account has on the box do not
    come along, so membership of the shared group is lost exactly when it
    matters. Without this a job cannot write its own run directory.

    Found on the box and not before it: every test until then ran as a user who
    already owned the directory, where the missing group makes no difference.
    """
    try:
        return shared_dir.stat().st_gid
    except OSError as e:
        LOG.warning("sparks: cannot read %s: %s", shared_dir, e)
        return None


def docker_group() -> int | None:
    """The group that owns the Docker socket.

    Read from the socket rather than looked up by the name `docker`: the name is
    a convention and the ownership is the fact, and inside a container the
    group's *name* may not resolve at all while its gid always does.
    """
    try:
        return DOCKER_SOCKET.stat().st_gid
    except OSError as e:
        LOG.warning("sparks: cannot read %s: %s", DOCKER_SOCKET, e)
        return None


def _first_line(path: Path) -> str | None:
    """The file's first line, or None if it is not there yet.

    Both files this reads are written by something else while we watch, so "not
    yet" is the normal case and not an error.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.strip().splitlines()[0] if text.strip() else None


def _bounded(
    stream: Iterable[dict[str, Any]], deadline: float
) -> Iterator[dict[str, Any]]:
    """Yield the pull stream's chunks until the deadline says stop.

    The check runs per chunk, so this bounds a pull that is still streaming; a
    stream that has gone silent is only noticed when its next chunk lands.
    """
    for chunk in stream:
        if time.monotonic() > deadline:
            raise PullFailedError(
                f"the pull was still going after "
                f"{PULL_TIMEOUT_SECONDS / 3600:g}h and was stopped"
            )
        yield chunk


def _pull_line(chunk: dict[str, Any], log: IO[bytes], log_path: Path) -> None:
    """Write one chunk's line to the pull log, failing on an error chunk.

    The write comes before the raise on purpose: the failure message points the
    reader at the log, so the error line has to be in it.
    """
    line = chunk.get("status") or chunk.get("error") or str(chunk)
    log.write((line + "\n").encode())
    if chunk.get("error") or chunk.get("errorDetail"):
        raise PullFailedError(f"docker pull failed; the output is in {log_path.name}")
