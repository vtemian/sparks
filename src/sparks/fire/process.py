"""Running a training command as a child, and reporting honestly how it ended.

The wrapper is the parent, and the only process guaranteed to outlive the run.
An OOM-killed child never runs `atexit`, never flushes and can never write its
own terminal status, so `crashed` and `killed` exist as statuses at all only
because something above the child records them.

The child gets its own session (`start_new_session=True`) and every signal is
forwarded deliberately. Leaving it in the wrapper's process group means the tty
delivers Ctrl-C to it directly and a forwarding wrapper then delivers it twice;
`timeout(1)` treats that tradeoff as unsolvable and exposes `--foreground` for
it. A new session costs the child its controlling terminal, which for a training
run is the right price.

Seven measured details are load-bearing here, and each one is a way the
dashboard lies if it is dropped. They are marked `Detail N` where they appear.
"""

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
"""How long a signalled child has to shut down before SIGKILL. Long enough to
write a checkpoint, which is the only reason to wait at all."""

POLL_SECONDS = 0.2
"""Detail 4: the bound on every wait. PEP 475 auto-retries an interrupted
`wait(timeout=None)`, so a handler's flag would never be observed and the
escalation deadline would be dead code. Measured: handler at t=0.51s, an
unbounded `wait()` returning at t=4.02s only because the child chose to exit."""

DRAIN_SECONDS = 5.0
"""How long the tee gets to finish reading after the child is gone. Bounded
because a stray that inherited the pipe can hold it open forever."""

SIGKILL_SECONDS = 5.0
"""How long survivors of the post-mortem sweep get before SIGKILL."""

FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

Handler = Callable[[int, FrameType | None], Any] | int | None


def clamp_exit(code: int) -> int:
    """Detail 6: an exit status is masked to 8 bits by wait(2), and the mask
    turns some failures into successes -- `sys.exit(256)` is reported to the
    caller as returncode 0. Nothing this wrapper synthesises may go that way, so
    a failure stays a failure and only a real 0 means success."""
    if code == 0:
        return 0
    return min(255, max(1, code))


@dataclass(frozen=True)
class Outcome:
    """Terminal state as three orthogonal fields, which is systemd's model.

    Never collapse them into one integer: a child that genuinely calls
    `exit(137)` and a SIGKILLed child both hand the caller `$? = 137`, and only
    these fields can tell them apart.
    """

    status: str
    """finished | crashed | cancelled | killed | oom"""
    exit_code: int | None
    """None when the child died to a signal."""
    signal_name: str | None = None
    wrapper_exit: int = 0
    """What the wrapper should exit with, so `$?` behaves like the shell's."""
    escalated_to_sigkill: bool = False
    """The child ignored its grace period."""


@dataclass(frozen=True)
class Completed:
    """One finished run: how it ended and when.

    `duration_seconds` comes from `time.monotonic()` while the two wall-clock
    stamps come from `time.time()`. The pair positions the run on the
    dashboard's time axis; the monotonic delta is the duration and is immune to
    NTP stepping the clock mid-run. They will disagree slightly, and that is
    correct.
    """

    outcome: Outcome
    started_unix: float
    ended_unix: float
    duration_seconds: float


def classify(returncode: int, interrupted_by: int | None) -> Outcome:
    """The terminal status, from the reap and from whether *we* were signalled.

    Cancellation is a property of the wrapper, never of the child. A training
    script that traps SIGTERM, checkpoints and exits 0 did not run to
    completion, and recording it `finished` is the dashboard lying about how the
    run ended. That was a real bug in the research drafts before it was caught.
    """
    if returncode >= 0:
        if interrupted_by is not None:
            # We were told to stop and the child complied. However it chose to
            # exit, this run did not run to completion.
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
    if interrupted_by is not None:
        status = "cancelled"
    elif signum == signal.SIGKILL:
        status = "killed"  # may be OOM; only the cgroup counter decides
    else:
        status = "crashed"
    return Outcome(
        status,
        exit_code=None,
        signal_name=signal.Signals(signum).name,
        wrapper_exit=clamp_exit(128 + signum),
    )


def oom_kills(cgroup: Path | None) -> int:
    """`oom_kill` from a cgroup's `memory.events.local`, or 0 if it cannot be
    read. Never raises: development happens on macOS, where there is no cgroup
    filesystem at all, and a run must not fail because it could not be
    attributed."""
    if cgroup is None:
        return 0
    try:
        text = (cgroup / "memory.events.local").read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


class Supervisor:
    """One child process, from launch to terminal status.

    Not reusable: one instance per run, because the signal state it accumulates
    is the run's.
    """

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
        """`env` overlays the wrapper's own environment rather than replacing
        it. `cgroup` is a directory holding `memory.events.local`, which is the
        only thing that can turn `killed` into `oom`."""
        self.command = list(command)
        self.log_path = log_path
        self.env = dict(env or {})
        self.cwd = cwd
        self.cgroup = cgroup
        self.grace_seconds = grace_seconds
        self.drain_seconds = drain_seconds
        self.sweep_seconds = sweep_seconds
        self.poll_seconds = poll_seconds

        self.child: subprocess.Popen[bytes] | None = None
        self.pgid: int | None = None
        self.interrupted_by: int | None = None
        self.escalated = False
        self._deadline: float | None = None
        self._previous: dict[int, Handler] = {}

    # -- public API ----------------------------------------------------------

    def run(self) -> Completed:
        """Launch, stream, wait, classify. Returns for every terminal state; the
        only exceptions that escape are failures to launch at all."""
        started_wall, started_mono = time.time(), time.monotonic()
        oom_before = oom_kills(self.cgroup)
        log = self.log_path.open("wb")
        # "wb" lands 0600 under umask 077, so the colleague whose GPU this run
        # is hogging cannot read its log. chmod is never masked; suppress the
        # EPERM that a directory another user owns would raise.
        with contextlib.suppress(OSError):
            os.chmod(self.log_path, FILE_MODE)
        reader: io.BufferedReader | None = None
        tee: threading.Thread | None = None
        try:
            # Handlers before the child exists, so a signal arriving in the gap
            # is recorded and delivered as soon as there is something to deliver
            # it to. The alternative loses it to the default disposition, which
            # kills the wrapper and orphans the child it just spawned.
            self._install()
            self.child = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one stream, interleaved in order
                stdin=subprocess.DEVNULL,  # a training run must never wait on a read
                start_new_session=True,
                bufsize=0,
                cwd=self.cwd,
                # Detail: a pipe switches CPython from line to block buffering.
                # Five lines printed at 0.3s intervals arrived in one burst at
                # t=1.54s without this, and at 0.02/0.32/0.62/0.92/1.23s with it.
                env={**os.environ, "PYTHONUNBUFFERED": "1", **self.env},
            )
            self._capture_pgid()
            if self.interrupted_by is not None:
                self._deliver(signal.SIGKILL if self.escalated else self.interrupted_by)

            stdout = self.child.stdout
            assert stdout is not None  # stdout=PIPE
            # `bufsize=0` hands back a raw FileIO, whose readline costs one
            # syscall per byte. Buffering here adds no latency -- readline
            # returns as soon as a newline is in the buffer -- and keeps 100k
            # lines of training output from becoming millions of syscalls.
            reader = io.BufferedReader(cast(io.RawIOBase, stdout))
            tee = threading.Thread(
                target=self._tee, args=(reader, log), name="sparks-tee", daemon=True
            )
            tee.start()

            returncode = self._wait_for_child()
            # Detail 5: the terminal facts are taken here, before the sweep and
            # before the tee is joined. A 1.5s run was measured as 6.5s because
            # orphaned workers held the stdout pipe open, so the tee never saw
            # EOF and its join ran the full timeout. Freezing the signal state
            # with them also keeps a Ctrl-C during cleanup from re-labelling a
            # run that had already ended on its own.
            ended_wall, ended_mono = time.time(), time.monotonic()
            interrupted_by, escalated = self.interrupted_by, self.escalated
            oom_after = oom_kills(self.cgroup)

            self._sweep()
            tee.join(timeout=self.drain_seconds)
            if tee.is_alive():
                LOG.warning(
                    "sparks: something still holds %s open after %.0fs; the log "
                    "stops here",
                    self.log_path,
                    self.drain_seconds,
                )
        finally:
            self._restore()
            # Only when the tee has finished with it. `close()` takes the same
            # lock the reader holds while blocked in read(), so closing it under
            # a live tee waits for the writer that outlived the drain -- which
            # is the exact hostage situation the bounded join exists to avoid.
            # Measured: a 0.1s run held here for 60s by three strays.
            if reader is not None and (tee is None or not tee.is_alive()):
                reader.close()
            log.close()

        outcome = replace(
            classify(returncode, interrupted_by), escalated_to_sigkill=escalated
        )
        if outcome.status == "killed" and oom_after > oom_before:
            outcome = replace(outcome, status="oom")
        return Completed(
            outcome=outcome,
            started_unix=started_wall,
            ended_unix=ended_wall,
            duration_seconds=ended_mono - started_mono,
        )

    # -- signals -------------------------------------------------------------

    def _forward(self, signum: int, _frame: FrameType | None) -> None:
        """Runs in the main thread between bytecodes. Keep it tiny.

        Detail 3: NEVER exit from here. A default-disposition SIGTERM skips
        `atexit` entirely, so `emit.py`'s `atexit.register(self._shutdown)`
        backstop would silently never fire. The wrapper must survive to record
        the outcome and flush the emitter, so this sets a flag and forwards, and
        the normal return path calls `end(status)` itself.
        """
        if self.interrupted_by is None:
            self.interrupted_by = signum
            self._deadline = time.monotonic() + self.grace_seconds
            self._deliver(signum)
        else:
            self.escalated = True  # second Ctrl-C: stop waiting
            self._deliver(signal.SIGKILL)

    def _deliver(self, signum: int) -> None:
        """Detail 2: chase every terminating signal with SIGCONT.

        A child stopped by SIGTSTP never runs its SIGTERM handler. Measured: it
        burned its entire grace period stopped, then died to SIGKILL with its
        checkpoint unwritten.
        """
        self._raw(signum)
        if signum not in (signal.SIGKILL, signal.SIGCONT):
            self._raw(signal.SIGCONT)

    def _raw(self, signum: int) -> None:
        """Detail 1: the group AND the pid, because a child that calls
        `setpgid` itself escapes the group.

        Exactly one of the two reaches any one process, though. Measured: a
        child still inside the group received both copies in 6 runs out of 10,
        and a second SIGTERM arriving after its handler has called `exit()`
        kills it during interpreter shutdown, when CPython has already restored
        SIG_DFL. The run then reads `cancelled / null / SIGTERM` when the child
        in fact checkpointed and exited 0, which is the wrapper corrupting the
        very record it exists to keep.
        """
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
        """Whether the child is still inside the group we just signalled."""
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

    # -- the child -----------------------------------------------------------

    def _capture_pgid(self) -> None:
        """Read once and cache: `getpgid` raises as soon as the child is reaped,
        and the group is exactly what the post-mortem sweep still needs."""
        child = self.child
        assert child is not None
        try:
            pgid: int = os.getpgid(child.pid)
        except ProcessLookupError:
            # Already gone. start_new_session made it a group leader, so its pid
            # was its pgid.
            pgid = child.pid
        if pgid == os.getpgrp():
            # start_new_session means this cannot happen. If it ever did, every
            # signal below would land on the wrapper and on whoever launched it.
            LOG.error("sparks: child shares the wrapper's process group")
            self.pgid = None
            return
        self.pgid = pgid

    def _wait_for_child(self) -> int:
        """Detail 4: never blocks unboundedly, so the escalation deadline is
        reachable."""
        child = self.child
        assert child is not None
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
                    "sparks: child ignored its %.0fs grace period; sending SIGKILL",
                    self.grace_seconds,
                )
                self.escalated = True
                self._deliver(signal.SIGKILL)

    def _sweep(self) -> None:
        """Detail 7: kill whatever the child left behind.

        A launcher that exits promptly while its workers ignore SIGTERM is the
        normal case, not an edge one, and the strays hold GPU memory and the
        stdout pipe. The pipe is why this runs before the tee is joined: until
        the last writer closes it, there is no EOF to wait for.
        """
        if not self._group_alive():
            return
        LOG.info("sparks: sweeping survivors in process group %s", self.pgid)
        self._group(signal.SIGTERM)
        self._group(signal.SIGCONT)
        deadline = time.monotonic() + self.sweep_seconds
        while time.monotonic() < deadline:
            if not self._group_alive():
                return
            time.sleep(self.poll_seconds)
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

    # -- output --------------------------------------------------------------

    def _tee(self, stream: IO[bytes], log: IO[bytes]) -> None:
        """Every line to the terminal and to the log, both flushed.

        Output is not lost when the child dies: this keeps draining the pipe
        afterwards, which is how 100000 of 100000 lines survived a SIGKILL.
        """
        echo = _stdout_bytes()
        try:
            for line in stream:
                if echo is not None:
                    _write(echo, line)
                _write(log, line)
        except (OSError, ValueError):
            # The pipe was closed under us because the drain timed out. There is
            # nothing left to read and nothing to report.
            pass


def _stdout_bytes() -> BinaryIO | None:
    """The wrapper's own stdout as bytes, or None if it has none to give."""
    try:
        return sys.stdout.buffer
    except AttributeError:
        return None


def _write(sink: IO[bytes], line: bytes) -> None:
    with contextlib.suppress(OSError, ValueError):
        # BrokenPipeError when the terminal goes away (`python -m … | head`),
        # ValueError when a sink was closed under us. Losing an echo must never
        # kill the run.
        sink.write(line)
        sink.flush()
