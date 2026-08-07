"""How runs end, exercised with real child processes and no mocks anywhere.

Every status here is produced by a real fork, a real signal and a real reap,
because the entire value of `sparks.fire.process` is what the kernel actually does to
a process rather than what a stand-in was told to report. Several of these tests
signal the *test runner itself*, which is the only faithful way to reproduce
the supervisor being killed from another terminal: the child raises the signal
against its own parent, so there is no sleep racing the handler.

The grace and sweep periods come from the constructor. The 30 s production grace
is correct for a checkpointing training script and would make this suite
unusable.
"""

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from sparks.fire.process import Completed, Supervisor, clamp_exit, classify

# Short enough to keep the suite fast, long enough that a loaded laptop does not
# escalate a child that was about to comply.
GRACE = 0.5
SWEEP = 0.5
DRAIN = 3.0
POLL = 0.05


def child(tmp_path: Path, name: str, body: str, *args: str) -> list[str]:
    """A real script, run by this interpreter."""
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(body).lstrip())
    return [sys.executable, str(path), *args]


def supervisor(
    tmp_path: Path,
    command: list[str],
    grace: float = GRACE,
    sweep: float = SWEEP,
    drain: float = DRAIN,
    cgroup: Path | None = None,
) -> Supervisor:
    return Supervisor(
        command,
        tmp_path / "stdout.log",
        cgroup=cgroup,
        grace_seconds=grace,
        drain_seconds=drain,
        sweep_seconds=sweep,
        poll_seconds=POLL,
    )


def run(tmp_path: Path, name: str, body: str, grace: float = GRACE) -> Completed:
    return supervisor(tmp_path, child(tmp_path, name, body), grace=grace).run()


def log_of(tmp_path: Path) -> str:
    return (tmp_path / "stdout.log").read_text()


def group_empty(pgid: int, timeout: float = 5.0) -> bool:
    """True once nothing is left in the process group.

    Polled rather than asserted once: by then the strays are orphans, so they
    are zombies until init reaps them, and init is not instantaneous.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def is_stopped(pid: int) -> bool:
    """`T` in ps state, on both macOS and Linux."""
    out = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,  # ps exits non-zero once the pid is gone, and gone is False
    )
    return out.stdout.strip().startswith("T")


def test_a_clean_exit_is_finished(tmp_path: Path) -> None:
    done = run(tmp_path, "clean", "print('training')")
    assert done.outcome.status == "finished"
    assert done.outcome.exit_code == 0
    assert done.outcome.signal_name is None
    assert done.outcome.wrapper_exit == 0
    assert "training" in log_of(tmp_path)


def test_a_nonzero_exit_is_crashed(tmp_path: Path) -> None:
    done = run(tmp_path, "three", "import sys; sys.exit(3)")
    assert done.outcome.status == "crashed"
    assert done.outcome.exit_code == 3
    assert done.outcome.signal_name is None
    assert done.outcome.wrapper_exit == 3


def test_an_uncaught_exception_is_crashed_and_its_traceback_is_kept(
    tmp_path: Path,
) -> None:
    # stderr=STDOUT is what puts the traceback in the same log as the output it
    # died in the middle of.
    done = run(tmp_path, "boom", "raise RuntimeError('boom')")
    assert done.outcome.status == "crashed"
    assert done.outcome.exit_code == 1
    assert "RuntimeError: boom" in log_of(tmp_path)


def test_a_genuine_exit_137_and_an_external_sigkill_are_told_apart(
    tmp_path: Path,
) -> None:
    """Both hand the caller $? = 137. Only these fields distinguish them, which
    is the entire reason status, exit_code and signal are three fields."""
    genuine = run(tmp_path, "exit137", "import sys; sys.exit(137)")
    killed = run(
        tmp_path,
        "sigkill",
        """
        import os, signal
        print('about to die')
        os.kill(os.getpid(), signal.SIGKILL)
        """,
    )

    assert genuine.outcome.wrapper_exit == killed.outcome.wrapper_exit == 137

    assert genuine.outcome.status == "crashed"
    assert genuine.outcome.exit_code == 137
    assert genuine.outcome.signal_name is None

    assert killed.outcome.status == "killed"
    assert killed.outcome.exit_code is None
    assert killed.outcome.signal_name == "SIGKILL"


def test_a_signalled_wrapper_is_cancelled_even_when_the_child_exits_zero(
    tmp_path: Path,
) -> None:
    """The bug worth naming. A script that traps SIGTERM, checkpoints and exits
    0 did not run to completion, and `finished` would be the dashboard lying."""
    done = run(
        tmp_path,
        "complies",
        """
        import os, signal, sys, time
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(30)
        """,
    )
    assert done.outcome.status == "cancelled"
    assert done.outcome.exit_code == 0
    assert done.outcome.signal_name == "SIGTERM"
    assert done.outcome.wrapper_exit == 143
    assert not done.outcome.escalated_to_sigkill


def test_a_signal_reaches_the_child_exactly_once(tmp_path: Path) -> None:
    """The group and the pid are two routes to the same process, and a child
    that has not left the group is on both. Measured: it received both copies
    in 6 runs out of 10, and the duplicate lands after its handler has called
    exit(), when CPython has restored SIG_DFL -- so a run that checkpointed and
    exited 0 gets recorded `cancelled / null / SIGTERM`. That is the wrapper
    corrupting the record it exists to keep, and it showed up first as the test
    above failing one run in four.

    Sampled rather than asserted once, because a duplicate that arrives before
    the child dequeues the first is coalesced by the kernel and leaves no trace.
    """
    deliveries = 0
    for i in range(5):
        counted = tmp_path / f"deliveries{i}"
        command = child(
            tmp_path,
            f"counts{i}",
            """
            import os, pathlib, signal, sys, time
            log = pathlib.Path(sys.argv[1])
            def count(signum, frame):
                with log.open('a') as f:
                    f.write('SIGTERM\\n')
            signal.signal(signal.SIGTERM, count)
            os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(0.3)   # long enough for a second copy to arrive
            sys.exit(0)
            """,
            str(counted),
        )
        # A grace period longer than the child's wait, so nothing escalates
        # while we are watching for the duplicate.
        done = supervisor(tmp_path, command, grace=5.0).run()
        assert done.outcome.status == "cancelled"
        assert done.outcome.exit_code == 0
        deliveries += counted.read_text().count("SIGTERM")

    assert deliveries == 5, f"5 signals were delivered {deliveries} times"


def test_a_child_that_ignores_sigterm_is_escalated_to_sigkill(tmp_path: Path) -> None:
    done = run(
        tmp_path,
        "ignores",
        """
        import os, signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(30)
        """,
    )
    assert done.outcome.status == "cancelled"
    assert done.outcome.exit_code is None
    assert done.outcome.signal_name == "SIGKILL"
    assert done.outcome.wrapper_exit == 137
    assert done.outcome.escalated_to_sigkill
    # The grace period was honoured rather than skipped.
    assert done.duration_seconds >= GRACE


def test_a_stopped_child_is_revived_by_sigcont_and_shuts_down_cleanly(
    tmp_path: Path,
) -> None:
    """A child stopped by SIGTSTP never runs its SIGTERM handler. Without the
    SIGCONT chaser it burns the whole grace period stopped and dies to SIGKILL
    with its checkpoint unwritten, so `not escalated` is the assertion that
    matters here."""
    marker = tmp_path / "handled"
    command = child(
        tmp_path,
        "stopped",
        """
        import os, signal, sys, time
        marker = sys.argv[1]
        def bye(signum, frame):
            open(marker, 'w').write('handled')
            sys.exit(0)
        signal.signal(signal.SIGTERM, bye)
        os.kill(os.getpid(), signal.SIGSTOP)
        time.sleep(30)
        """,
        str(marker),
    )
    sup = supervisor(tmp_path, command, grace=2.0)

    def signal_once_stopped() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            kid = sup.child
            if kid is not None and is_stopped(kid.pid):
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.02)

    watcher = threading.Thread(target=signal_once_stopped)
    watcher.start()
    done = sup.run()
    watcher.join()

    assert marker.read_text() == "handled", "the child never ran its SIGTERM handler"
    assert done.outcome.status == "cancelled"
    assert done.outcome.exit_code == 0
    assert not done.outcome.escalated_to_sigkill


def launcher(tmp_path: Path) -> list[str]:
    """Three workers that ignore SIGTERM, and a launcher that exits as soon as
    they do.

    The workers inherit the launcher's stdout, which is what holds the pipe open
    after it is gone. The launcher waits for all three to have *installed* their
    handler before exiting: a worker still in interpreter startup dies to the
    sweep's SIGTERM like anything else, and the test would then pass without
    ever reaching the escalation it exists to check.
    """
    ready = tmp_path / "ready"
    ready.mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text(
        textwrap.dedent(
            """
            import os, pathlib, signal, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            (pathlib.Path(sys.argv[1]) / str(os.getpid())).write_text('ready')
            time.sleep(60)
            """
        ).lstrip()
    )
    return child(
        tmp_path,
        "launcher",
        """
        import pathlib, subprocess, sys, time
        ready = pathlib.Path(sys.argv[2])
        for _ in range(3):
            subprocess.Popen([sys.executable, sys.argv[1], str(ready)])
        while len(list(ready.iterdir())) < 3:
            time.sleep(0.01)
        print('launched', flush=True)
        sys.exit(0)
        """,
        str(worker),
        str(ready),
    )


def test_a_launcher_whose_workers_ignore_sigterm_leaves_no_survivors(
    tmp_path: Path,
) -> None:
    sup = supervisor(tmp_path, launcher(tmp_path))
    done = sup.run()

    assert done.outcome.status == "finished"
    assert sup.pgid is not None
    assert group_empty(sup.pgid), "strays left in the group; they hold GPU memory"


def test_the_duration_excludes_the_wait_for_orphaned_writers(tmp_path: Path) -> None:
    """A 1.5 s run was measured as 6.5 s because the orphans held the stdout
    pipe open, so the tee never saw EOF and its join ran the full timeout. The
    end of the run is when the child was reaped, not when the cleanup finished.
    """
    sup = supervisor(tmp_path, launcher(tmp_path), sweep=1.0)
    started = time.monotonic()
    done = sup.run()
    elapsed = time.monotonic() - started

    assert done.duration_seconds < 1.0, "cleanup was billed to the run"
    assert elapsed - done.duration_seconds > 0.8, "the cleanup never happened"


def test_a_stray_the_sweep_cannot_reach_never_holds_the_wrapper_open(
    tmp_path: Path,
) -> None:
    """A grandchild in its own session inherits the stdout pipe and is out of
    reach of the group sweep, so the tee never sees EOF. The drain is bounded
    for exactly this, and so is everything after it: closing the reader under a
    live tee waits on the same lock the blocked read holds, which held a 0.1 s
    run open for the stray's full 60 s before it was caught."""
    drain = 1.0
    pidfile = tmp_path / "stray.pid"
    stray = tmp_path / "stray.py"
    stray.write_text(
        textwrap.dedent(
            """
            import os, pathlib, sys, time
            pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
            time.sleep(20)
            """
        ).lstrip()
    )
    command = child(
        tmp_path,
        "escapes",
        """
        import pathlib, subprocess, sys, time
        subprocess.Popen(
            [sys.executable, sys.argv[1], sys.argv[2]], start_new_session=True
        )
        while not pathlib.Path(sys.argv[2]).exists():
            time.sleep(0.01)
        print('spawned', flush=True)
        sys.exit(0)
        """,
        str(stray),
        str(pidfile),
    )

    started = time.monotonic()
    done = supervisor(tmp_path, command, drain=drain).run()
    elapsed = time.monotonic() - started
    try:
        assert done.outcome.status == "finished"
        assert done.duration_seconds < 1.0
        assert elapsed > drain, "the drain was skipped; late output would be lost"
        assert elapsed < drain + 2.0, "the wrapper waited on a process it cannot kill"
    finally:
        # Tolerant on purpose: if the wrapper did wait, the stray is long gone
        # by now and the assertion above is the failure worth reading.
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_large_output_survives_a_sigkill(tmp_path: Path) -> None:
    done = run(
        tmp_path,
        "loud",
        """
        import os, signal, sys
        for i in range(100000):
            print(i)
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
        """,
    )
    assert done.outcome.status == "killed"
    lines = log_of(tmp_path).splitlines()
    assert len(lines) == 100000, f"lost {100000 - len(lines)} lines"
    assert lines[-1] == "99999"


def test_output_reaches_the_log_while_the_child_is_still_running(
    tmp_path: Path,
) -> None:
    """A pipe switches CPython from line to block buffering: five lines printed
    at 0.3 s intervals arrived in one burst at t=1.54 s until PYTHONUNBUFFERED
    was set. Over tens of minutes that is a dead terminal against a live feed.
    """
    seen_at: list[float] = []
    log = tmp_path / "stdout.log"

    def watch() -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if log.exists() and b"\n" in log.read_bytes():
                seen_at.append(time.monotonic() - started)
                return
            time.sleep(0.02)

    watcher = threading.Thread(target=watch)
    started = time.monotonic()
    watcher.start()
    done = run(
        tmp_path,
        "slow",
        """
        import time
        for i in range(3):
            print(i)
            time.sleep(1.0)
        """,
    )
    watcher.join()

    assert done.duration_seconds > 2.0
    assert seen_at, "no line ever reached the log"
    assert seen_at[0] < 1.0, f"first line only appeared at {seen_at[0]:.2f}s"


def test_a_synthesised_exit_code_is_clamped_to_eight_bits(tmp_path: Path) -> None:
    """`sys.exit(256)` is reported by the kernel as returncode 0: a failing
    child observed as a success. That mask is already applied by the time the
    wrapper sees it and cannot be undone, so the guarantee this test pins is the
    other half -- that nothing the wrapper synthesises can go the same way."""
    done = run(tmp_path, "masked", "import sys; sys.exit(256)")
    assert done.outcome.exit_code == 0

    assert clamp_exit(256) == 255
    assert clamp_exit(0) == 0
    assert clamp_exit(3) == 3
    assert clamp_exit(-9) == 1


def test_classification_is_decided_by_the_wrapper_not_by_the_child() -> None:
    assert classify(0, None).status == "finished"
    assert classify(0, signal.SIGTERM).status == "cancelled"
    assert classify(-signal.SIGKILL, None).status == "killed"
    assert classify(-signal.SIGKILL, signal.SIGINT).status == "cancelled"
    assert classify(-signal.SIGSEGV, None).status == "crashed"


def test_a_kill_with_cgroup_proof_is_oom(tmp_path: Path) -> None:
    """`killed` plus a cgroup that counted an oom_kill. The counter is bumped by
    the child itself here, because the kernel's OOM killer is not something a
    test suite may provoke on a shared box; the file, its format and the delta
    across the run are the real thing."""
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.events.local").write_text(
        "low 0\nhigh 0\nmax 4\noom 1\noom_kill 0\n"
    )
    done = supervisor(
        tmp_path,
        child(
            tmp_path,
            "oom",
            """
            import os, pathlib, signal, sys
            events = 'low 0\\nhigh 0\\nmax 9\\noom 2\\noom_kill 1\\n'
            pathlib.Path(sys.argv[1]).write_text(events)
            os.kill(os.getpid(), signal.SIGKILL)
            """,
            str(cgroup / "memory.events.local"),
        ),
        cgroup=cgroup,
    ).run()

    assert done.outcome.status == "oom"
    assert done.outcome.signal_name == "SIGKILL"


def test_a_kill_without_cgroup_proof_stays_killed(tmp_path: Path) -> None:
    """Degrade rather than guess: on a plain SSH login the cgroup is the whole
    session's, so a non-zero delta means `something in my session was killed`."""
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.events.local").write_text("oom 0\noom_kill 0\n")
    done = supervisor(
        tmp_path,
        child(
            tmp_path,
            "plainkill",
            """
            import os, signal
            os.kill(os.getpid(), signal.SIGKILL)
            """,
        ),
        cgroup=cgroup,
    ).run()

    assert done.outcome.status == "killed"


def test_a_missing_cgroup_is_not_an_error(tmp_path: Path) -> None:
    # Development happens on macOS, where there is no cgroup filesystem at all.
    done = supervisor(
        tmp_path, child(tmp_path, "nocgroup", "pass"), cgroup=tmp_path / "absent"
    ).run()
    assert done.outcome.status == "finished"
