"""Docker, as the runner needs it: build a project, then run it under `sparks
run` as the account that submitted it.

The nesting is deliberate and reads oddly the first time:

    sparks run -- docker run ... <image> <command>

`sparks run` stays OUTSIDE the training container so that a project's image does
not have to contain sparks. The alternative - sparks as every image's entrypoint
- would put this framework in the dependency list of every Dockerfile anyone
writes, which fails the only test that matters for the thing people have to
write themselves.

The price is that this process needs what the energy sampler reads: `/sys`
mounted read-only for hwmon and `--gpus all` for NVML. Both are read-only uses,
and the queue container gets them from sparkup.

Privilege runs the other way from what the nesting suggests. This module runs as
root, because talking to the Docker socket is root-equivalent, and it drops to
the submitter's uid for `sparks run` and everything under it. The uid comes from
`stat()` on the job manifest, never from a field inside it.
"""

import contextlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from sparks import spool
from sparks.runner import BuildFailed

LOG = logging.getLogger("sparks")

DOCKER_SOCKET = Path("/var/run/docker.sock")

BUILD_TIMEOUT_SECONDS = 3600.0
"""A build that has taken an hour is not going to finish. Bounded because the
runner is single-threaded: a hung build is the whole queue stopped."""

INSPECT_TIMEOUT_SECONDS = 30.0
CLEANUP_TIMEOUT_SECONDS = 60.0

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
    """A job in flight: the `sparks run` process, and the container under it."""

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
        """SIGTERM to `sparks run`, and nothing else.

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

        `--rm` removes the container when `docker run` exits normally, so this
        is for the case where the client died without it: the container then
        outlives its supervisor and keeps the GPU. That is the same class of
        problem as the orphaned workers `process.py` sweeps for, one level up.
        """
        with contextlib.suppress(OSError, ValueError):
            self._log.close()
        container = self.container_id()
        if not container:
            return
        result = _run_quietly(
            ["docker", "rm", "--force", "--volumes", container],
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
        if result is not None and result.returncode == 0:
            # Not necessarily "left behind": --rm removal is asynchronous, so
            # this usually just wins a harmless race with the daemon. Worth a
            # line at debug, not a claim at info that something went wrong.
            LOG.debug("sparks: cleaned up container %s", container[:12])


@dataclass
class Docker:
    """The real engine. Holds only what every job needs told to it."""

    shared_dir: Path
    url: str
    """Prometheus as reachable FROM A CONTAINER. The box contract's URL is
    loopback, which inside here is this container, so the queue is configured
    with the host-gateway form and passes the same one down."""
    sparks_bin: str = "sparks"
    gpus: str = "all"
    """Passed to `docker run --gpus`, or empty to omit the flag entirely.

    Omitting matters: on a daemon with no nvidia runtime `--gpus all` is not
    ignored, it is a hard error, so a box without a GPU could not run a job at
    all. That box is a real one - it is the laptop this was written on.
    """
    docker_bin: str = "docker"
    extra_groups: list[int] = field(default_factory=list)

    def build(self, context: Path, tag: str, log_path: Path) -> str:
        """Build the project's Dockerfile and return the image id.

        The id, not a tag: a tag can be re-pointed at another image later and
        the record would silently change meaning. This is a locally built image
        so it has no registry digest to quote instead.
        """
        dockerfile = context / "Dockerfile"
        if not dockerfile.is_file():
            raise BuildFailed(
                f"{context.name} has no Dockerfile. sparks builds your project "
                "into an image and runs that; a project without one has not "
                "said how it should be built"
            )
        with log_path.open("wb") as log:
            try:
                done = subprocess.run(
                    [
                        self.docker_bin,
                        "build",
                        "--tag",
                        tag,
                        "--file",
                        str(dockerfile),
                        str(context),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=BUILD_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                raise BuildFailed(
                    f"the build was still going after "
                    f"{BUILD_TIMEOUT_SECONDS / 3600:g}h and was stopped"
                ) from e
            except OSError as e:
                raise BuildFailed(f"could not run docker build: {e}") from e
        if done.returncode != 0:
            raise BuildFailed(
                f"docker build exited {done.returncode}; the output is in "
                f"{log_path.name}"
            )
        return self._image_id(tag)

    def start(self, entry: spool.Entry, image: str, log_path: Path) -> Process:
        """Start the job, as its owner."""
        uid = entry.owner_uid
        gid = entry.path.stat().st_gid
        cidfile = entry.path / spool.CID_FILE
        run_id_file = entry.path / spool.RUN_ID_FILE
        # docker refuses to start if the cidfile already exists, which on a
        # retried or resumed job it would.
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
            self.sparks_bin,
            # BEFORE the subcommand, because it is a global flag and argparse
            # rejects it after one. This cost a live job to find, so the test
            # that pins the position is the point of it being a pure function.
            "--url",
            self.url,
            "run",
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
        """`docker run` for one job.

        Every mount and flag here is chosen by the queue. The job file says what
        image and what command, and nothing else: a job that could name its own
        `--volume` could mount `/` and be root on the box, which is the single
        thing this list is guarding.
        """
        return [
            self.docker_bin,
            "run",
            "--rm",
            # PID 1 in a container does not get default signal dispositions, so
            # without an init a training script that installs no SIGTERM handler
            # ignores the abort entirely and waits out the grace period.
            "--init",
            "--name",
            f"{CONTAINER_PREFIX}-{entry.job.job_id}",
            "--cidfile",
            str(cidfile),
            *(["--gpus", self.gpus] if self.gpus else []),
            "--user",
            f"{uid}:{gid}",
            "--volume",
            f"{self.shared_dir}:{self.shared_dir}",
            "--workdir",
            str(self.shared_dir),
            # THE TRAP, the same one monitoring's compose file documents: inside
            # a container `localhost` is the container, so a loopback Prometheus
            # URL reaches nothing. On Linux this alias does not exist unless
            # this line makes it.
            "--add-host",
            "host.docker.internal:host-gateway",
            # Value-less form: forwards what `sparks run` put in our environment,
            # so the run id the container reports under is the one the wrapper
            # reserved rather than a second one invented inside.
            "--env",
            "SPARKS_RUN_ID",
            "--env",
            "SPARKS_PROMETHEUS_URL",
            "--env",
            "PYTHONUNBUFFERED=1",
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
        _run_quietly(
            [self.docker_bin, "rm", "--force", "--volumes", container_id],
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )

    def _image_id(self, tag: str) -> str:
        result = _run_quietly(
            [self.docker_bin, "image", "inspect", "--format", "{{.Id}}", tag],
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0:
            # The build succeeded, so this is worth recording but not worth
            # failing the job over. The tag still names the image that will run.
            LOG.warning("sparks: built %s but could not read its id", tag)
            return tag
        return result.stdout.decode().strip() or tag


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


def _run_quietly(
    argv: list[str], timeout: float
) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("sparks: %s failed: %s", " ".join(argv[:3]), e)
        return None
