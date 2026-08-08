import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import IO, Any, BinaryIO, cast

from sparks.shared import FILE_MODE

LOG = logging.getLogger("sparks")

GRACE_SECONDS = 30.0

POLL_SECONDS = 0.2

DRAIN_SECONDS = 5.0

SIGKILL_SECONDS = 5.0

FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

Handler = Callable[[int, FrameType | None], Any] | int | None


def clamp_exit(code: int) -> int:
    if code == 0:
        return 0

    return min(255, max(1, code))


@dataclass(frozen=True)
class Outcome:
    status: str
    exit_code: int | None
    signal_name: str | None = None
    wrapper_exit: int = 0
    escalated_to_sigkill: bool = False


@dataclass(frozen=True)
class Completed:
    outcome: Outcome
    started_unix: float
    ended_unix: float
    duration_seconds: float


def classify(returncode: int, interrupted_by: int | None) -> Outcome:
    if returncode >= 0:
        if interrupted_by is not None:
            # Cancellation is ours: however it exited, this run did not finish.
            return Outcome(
                "cancelled",
                exit_code=returncode,
                signal_name=signal.Signals(interrupted_by).name,
                wrapper_exit=clamp_exit(128 + interrupted_by),
            )

        if returncode == 0:
            return Outcome("finished", exit_code=0, wrapper_exit=0)

        return Outcome(
            "crashed", exit_code=returncode, wrapper_exit=clamp_exit(returncode)
        )

    signum = -returncode
    return Outcome(
        status_for_signal(signum, interrupted_by),
        exit_code=None,
        signal_name=signal.Signals(signum).name,
        wrapper_exit=clamp_exit(128 + signum),
    )


def status_for_signal(signum: int, interrupted_by: int | None) -> str:
    if interrupted_by is not None:
        return "cancelled"  # an escalated Ctrl-C lands as SIGKILL; still ours

    if signum == signal.SIGKILL:
        return "killed"  # may be OOM; only the cgroup counter decides

    return "crashed"


def oom_kills(cgroup: Path | None) -> int:
    text = events_text(cgroup)
    for line in text.splitlines():
        # partition, not split: a line with no space falls through, not raises.
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            try:
                return int(value)
            except ValueError:
                return 0

    return 0


def events_text(cgroup: Path | None) -> str:
    if cgroup is None:
        return ""

    events = cgroup / "memory.events.local"
    try:
        return events.read_text()
    except OSError:
        return ""


@dataclass(frozen=True)
class _Reaped:
    returncode: int
    ended_wall: float
    ended_mono: float
    interrupted_by: int | None
    escalated: bool
    oom_after: int


class Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        log_path: Path,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        cgroup: Path | None = None,
        grace_seconds: float = GRACE_SECONDS,
        drain_seconds: float = DRAIN_SECONDS,
        sweep_seconds: float = SIGKILL_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.command = list(command)
        self.log_path = log_path
        self.env = dict(env or {})
        self.cwd = cwd
        self.cgroup = cgroup
        self.grace_seconds = grace_seconds
        self.drain_seconds = drain_seconds
        self.sweep_seconds = sweep_seconds
        self.poll_seconds = poll_seconds
        self._blank_state()

    def _blank_state(self) -> None:
        self.child: subprocess.Popen[bytes] | None = None
        self.pgid: int | None = None
        self.interrupted_by: int | None = None
        self.escalated = False
        self._deadline: float | None = None
        self._previous: dict[int, Handler] = {}

    def run(self) -> Completed:
        started_wall, started_mono = time.time(), time.monotonic()
        oom_before = oom_kills(self.cgroup)
        log = self.log_path.open("wb")
        # Group-readable whatever the umask, so the other account can read a
        # run's log; EPERM if the directory is someone else's.
        with contextlib.suppress(OSError):
            os.chmod(self.log_path, FILE_MODE)
        reader: io.BufferedReader | None = None
        tee: threading.Thread | None = None
        try:
            reader = self._start()
            tee = threading.Thread(
                target=self._tee, args=(reader, log), name="sparks-tee", daemon=True
            )
            tee.start()
            reaped = self._reap()
            # Before the join: Detail 7, there is no EOF while strays hold it.
            self._sweep()
            tee.join(timeout=self.drain_seconds)
            if tee.is_alive():
                LOG.warning(
                    "sparks: something still holds %s open after %.0fs; "
                    "the log stops here",
                    self.log_path,
                    self.drain_seconds,
                )
            else:
                LOG.debug("sparks: the tee drains; the log is complete")
        finally:
            self._restore()
            # Only under a dead tee: close() waits on the lock a read() holds.
            if reader is not None and (tee is None or not tee.is_alive()):
                reader.close()
            log.close()

        outcome = self._finish(reaped, oom_before)
        LOG.debug(
            "sparks: the run is %s (exit=%s signal=%s)",
            outcome.status,
            outcome.exit_code,
            outcome.signal_name,
        )
        return Completed(
            outcome=outcome,
            started_unix=started_wall,
            ended_unix=reaped.ended_wall,
            duration_seconds=reaped.ended_mono - started_mono,
        )

    def _start(self) -> io.BufferedReader:
        # Before the child exists: a signal in the gap would orphan it.
        self._install()
        LOG.debug("sparks: launching %s", self.command)
        self.child = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # one stream, interleaved in order
            stdin=subprocess.DEVNULL,  # a training run must never wait on a read
            start_new_session=True,
            bufsize=0,
            cwd=self.cwd,
            # A pipe switches CPython to block buffering; output would burst.
            env={**os.environ, "PYTHONUNBUFFERED": "1", **self.env},
        )
        self._capture_pgid()
        LOG.debug(
            "sparks: the child runs as pid %s in process group %s",
            self.child.pid,
            self.pgid,
        )
        if self.interrupted_by is not None:
            LOG.debug("sparks: a stop is pending from before the launch; forwarding it")
            self._deliver(signal.SIGKILL if self.escalated else self.interrupted_by)

        stdout = self.child.stdout
        assert stdout is not None  # noqa: S101 -- narrowing; stdout=PIPE
        # `bufsize=0` hands back a raw FileIO: one syscall per byte unbuffered.
        return io.BufferedReader(cast("io.RawIOBase", stdout))

    def _reap(self) -> _Reaped:
        # Its own statement: the order must not rest on argument evaluation.
        returncode = self._wait_for_child()
        reaped = _Reaped(
            returncode=returncode,
            ended_wall=time.time(),
            ended_mono=time.monotonic(),
            interrupted_by=self.interrupted_by,
            escalated=self.escalated,
            oom_after=oom_kills(self.cgroup),
        )
        LOG.debug("sparks: the child is reaped with returncode %d", returncode)
        return reaped

    def _finish(self, reaped: _Reaped, oom_before: int) -> Outcome:
        outcome = replace(
            classify(reaped.returncode, reaped.interrupted_by),
            escalated_to_sigkill=reaped.escalated,
        )
        if outcome.status == "killed" and reaped.oom_after > oom_before:
            LOG.debug("sparks: the oom_kill counter moved; killed becomes oom")
            outcome = replace(outcome, status="oom")
        return outcome

    def _forward(self, signum: int, _frame: FrameType | None) -> None:
        if self.interrupted_by is None:
            self.interrupted_by = signum
            self._deadline = time.monotonic() + self.grace_seconds
            LOG.debug(
                "sparks: %s arrives; forwarding it and waiting %.0fs",
                signal.Signals(signum).name,
                self.grace_seconds,
            )
            self._deliver(signum)
        else:
            self.escalated = True  # second Ctrl-C: stop waiting
            LOG.debug("sparks: a second stop arrives; escalating to SIGKILL")
            self._deliver(signal.SIGKILL)

    def _deliver(self, signum: int) -> None:
        self._raw(signum)
        if signum not in (signal.SIGKILL, signal.SIGCONT):
            self._raw(signal.SIGCONT)

    def _raw(self, signum: int) -> None:
        self._group(signum)
        child = self.child
        if child is None or child.returncode is not None:
            return

        if self._in_group(child.pid):
            return  # killpg already delivered it
        # send_signal already guards the pid-reuse race by re-polling.
        with contextlib.suppress(ProcessLookupError, ValueError):
            child.send_signal(signum)

    def _in_group(self, pid: int) -> bool:
        if self.pgid is None:
            return False

        try:
            return os.getpgid(pid) == self.pgid
        except OSError:
            return False

    def _group(self, signum: int) -> None:
        if self.pgid is None:
            return

        with contextlib.suppress(OSError):
            os.killpg(self.pgid, signum)

    def _install(self) -> None:
        for signum in FORWARDED:
            self._previous[signum] = signal.signal(signum, self._forward)

    def _restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()

    def _capture_pgid(self) -> None:
        child = self.child
        assert child is not None  # noqa: S101 -- narrowing; only called after Popen
        try:
            pgid: int = os.getpgid(child.pid)
        except ProcessLookupError:
            # Gone already; start_new_session made it a leader, so pgid == pid.
            pgid = child.pid
        if pgid == os.getpgrp():
            # Impossible under start_new_session; else we signal ourselves.
            LOG.error("sparks: child shares the wrapper's process group")
            self.pgid = None
            return

        self.pgid = pgid

    def _wait_for_child(self) -> int:
        child = self.child
        assert child is not None  # noqa: S101 -- narrowing; only called after Popen
        while True:
            try:
                return child.wait(timeout=self.poll_seconds)
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:
                continue  # our handler already forwarded it

            if (
                self._deadline is not None
                and not self.escalated
                and time.monotonic() >= self._deadline
            ):
                LOG.warning(
                    "sparks: the child ignores its %.0fs grace period; sending SIGKILL",
                    self.grace_seconds,
                )
                self.escalated = True
                self._deliver(signal.SIGKILL)

    def _sweep(self) -> None:
        if not self._group_alive():
            return

        LOG.info("sparks: sweeping survivors in process group %s", self.pgid)
        self._group(signal.SIGTERM)
        self._group(signal.SIGCONT)
        deadline = time.monotonic() + self.sweep_seconds
        while time.monotonic() < deadline:
            if not self._group_alive():
                LOG.debug("sparks: the group is empty; nothing survived the sweep")
                return

            time.sleep(self.poll_seconds)
        LOG.warning("sparks: survivors ignore the sweep; sending SIGKILL")
        self._group(signal.SIGKILL)

    def _group_alive(self) -> bool:
        if self.pgid is None:
            return False

        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # someone is there; we may just not signal them

        return True

    def _tee(self, stream: IO[bytes], log: IO[bytes]) -> None:
        try:
            echo: BinaryIO | None = sys.stdout.buffer
        except AttributeError:
            echo = None
        try:
            for line in stream:
                if echo is not None:
                    # The terminal can go away; losing the echo is not fatal.
                    with contextlib.suppress(OSError, ValueError):
                        echo.write(line)
                        echo.flush()
                with contextlib.suppress(OSError, ValueError):
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            LOG.debug("sparks: the drain timed out and closed the pipe under the tee")
