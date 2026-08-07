"""The runner, with Docker faked out.

Everything asserted here is the runner's own judgement: what runs next, what a
pull failure does to the job behind it, who is allowed to stop what, and
whether a job that ends still leaves a truthful record. None of it needs a
daemon, which is the point of the Engine seam.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from sparks import index, spool
from sparks.fire import runner


class FakeHandle:
    """A job in flight. `alive_for` is how many polls it survives, so a test can
    make a job long enough to abort without any real time passing."""

    def __init__(
        self,
        code: int = 0,
        alive_for: int = 0,
        run_id: str | None = "run-20260806-1200-vlad-e0",
    ) -> None:
        self.code = code
        self.alive_for = alive_for
        self._run_id = run_id
        self.terminated = False
        self.finished = False
        self.polls = 0

    def poll(self) -> int | None:
        self.polls += 1
        if self.polls <= self.alive_for and not self.terminated:
            return None
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def run_id(self) -> str | None:
        return self._run_id

    def container_id(self) -> str | None:
        return "c0ffee"

    def finish(self) -> None:
        self.finished = True


class FakeEngine:
    def __init__(
        self,
        handle: FakeHandle | None = None,
        pull_error: str | None = None,
        on_start: Callable[[spool.Entry], None] | None = None,
    ) -> None:
        self.handle = handle or FakeHandle()
        self.pull_error = pull_error
        self.on_start = on_start
        self.pulled: list[str] = []
        self.started: list[tuple[str, str]] = []
        self.released: list[str] = []

    def release(self, container_id: str) -> None:
        self.released.append(container_id)

    def pull(self, image: str, log_path: Path) -> None:
        self.pulled.append(image)
        if self.pull_error:
            raise runner.PullFailedError(self.pull_error)

    def start(self, entry: spool.Entry, image: str, log_path: Path) -> FakeHandle:
        self.started.append((entry.job.job_id, image))
        # The hook is how a test asks for an abort at the only moment a person
        # could: once the job is actually running.
        if self.on_start is not None:
            self.on_start(entry)
        return self.handle


@pytest.fixture
def textfile(tmp_path: Path) -> Path:
    target = tmp_path / "textfile"
    target.mkdir()
    return target


def a_runner(
    tmp_path: Path, textfile: Path, engine: FakeEngine | None = None
) -> runner.Runner:
    return runner.Runner(
        queue_dir=tmp_path / "queue",
        engine=engine or FakeEngine(),
        textfile_dir=textfile,
        poll_seconds=0.0,
        sleep=lambda _: None,
        now=lambda: 1785849000.0,
    )


IMAGE = "spark.local:5000/demo:1"


def a_job(
    queue: Path,
    name: str = "e0",
    when: float | None = None,
    image: str = IMAGE,
    *,
    with_data: bool = True,
) -> spool.Entry:
    entry = spool.submit(
        queue,
        name=name,
        user="vlad",
        command=["python", "train.py"],
        when=when,
        image=image,
    )
    if with_data:
        entry.data_dir.mkdir(parents=True, exist_ok=True)
    return entry


def ask_to_abort(entry: spool.Entry) -> None:
    """What a person does from the CLI once they see the job running."""
    spool.request(entry.path, spool.ABORT)


class TestHappyPath:
    def test_a_queued_job_is_pulled_and_run_and_recorded(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        r.tick()
        done = spool.load(entry.path)
        assert done.state.state == spool.FINISHED
        assert done.state.exit_code == 0
        assert done.state.image == IMAGE
        assert engine.pulled == [IMAGE]
        assert engine.started[0][1] == IMAGE

    def test_the_run_id_is_recorded_so_the_job_joins_its_run(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """Without this the queue and the run index are two lists of the same
        events with nothing connecting them."""
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        r.tick()
        assert spool.load(entry.path).state.run_id == "run-20260806-1200-vlad-e0"

    def test_a_nonzero_exit_is_a_failure_not_a_finish(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile, FakeEngine(FakeHandle(code=1)))
        entry = a_job(r.queue_dir)
        r.tick()
        done = spool.load(entry.path)
        assert done.state.state == spool.FAILED
        assert done.state.exit_code == 1

    def test_the_job_is_always_cleaned_up_after(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """A container that outlives its supervisor keeps the GPU, and the next
        job in the queue would then contend with a run nobody is watching."""
        handle = FakeHandle()
        r = a_runner(tmp_path, textfile, FakeEngine(handle))
        a_job(r.queue_dir)
        r.tick()
        assert handle.finished


class TestOrdering:
    def test_the_oldest_job_goes_first(self, tmp_path: Path, textfile: Path) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        second = a_job(r.queue_dir, name="second", when=2000.0)
        first = a_job(r.queue_dir, name="first", when=1000.0)
        r.tick()
        r.tick()
        assert [job_id for job_id, _ in engine.started] == [
            first.job.job_id,
            second.job.job_id,
        ]

    def test_only_one_job_runs_per_pass(self, tmp_path: Path, textfile: Path) -> None:
        """Exclusivity is structural here: there is one runner and it is busy.
        No lock to get wrong, which is why the per-user design was dropped."""
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        a_job(r.queue_dir, name="a")
        a_job(r.queue_dir, name="b")
        r.tick()
        assert len(engine.started) == 1

    def test_an_empty_queue_reports_nothing_to_do(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        assert a_runner(tmp_path, textfile).tick() is False


class TestMissingPrerequisites:
    def test_a_job_with_no_image_fails_without_starting(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir, image="")
        r.tick()
        done = spool.load(entry.path)
        assert done.state.state == spool.FAILED
        assert "no image" in (done.state.detail or "")
        assert engine.pulled == []
        assert engine.started == []

    def test_a_job_with_no_data_dir_fails_without_starting(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir, with_data=False)
        r.tick()
        done = spool.load(entry.path)
        assert done.state.state == spool.FAILED
        assert "data/" in (done.state.detail or "")
        assert engine.pulled == []
        assert engine.started == []


class TestPullFailure:
    def test_a_broken_pull_fails_only_its_own_job(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine(pull_error="manifest unknown")
        r = a_runner(tmp_path, textfile, engine)
        broken = a_job(r.queue_dir, name="broken", when=1000.0)
        healthy = a_job(r.queue_dir, name="healthy", when=2000.0)
        r.tick()
        assert spool.load(broken.path).state.state == spool.FAILED
        engine.pull_error = None
        r.tick()
        assert spool.load(healthy.path).state.state == spool.FINISHED

    def test_the_reason_is_kept_where_the_submitter_will_look(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile, FakeEngine(pull_error="manifest unknown"))
        entry = a_job(r.queue_dir)
        r.tick()
        assert "manifest unknown" in (spool.load(entry.path).state.detail or "")

    def test_a_failed_pull_never_starts_a_container(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine(pull_error="boom")
        r = a_runner(tmp_path, textfile, engine)
        a_job(r.queue_dir)
        r.tick()
        assert engine.started == []


class TestCancel:
    def test_a_queued_job_cancels_without_ever_pulling(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        spool.request(entry.path, spool.CANCEL)
        r.tick()
        assert spool.load(entry.path).state.state == spool.CANCELLED
        assert engine.pulled == []

    def test_aborting_a_job_that_has_not_started_cancels_it(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """The verb the user reaches for depends on what they last saw on the
        dashboard, which may already be out of date. Both mean stop it."""
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        spool.request(entry.path, spool.ABORT)
        r.tick()
        assert spool.load(entry.path).state.state == spool.CANCELLED

    def test_the_request_is_cleared_so_it_is_not_replayed(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        spool.request(entry.path, spool.CANCEL)
        r.tick()
        assert spool.requests(entry.path) == []


class TestAbort:
    def test_a_running_job_is_terminated(self, tmp_path: Path, textfile: Path) -> None:
        handle = FakeHandle(alive_for=5, code=143)
        engine = FakeEngine(handle, on_start=ask_to_abort)
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        r.tick()
        assert handle.terminated
        assert spool.load(entry.path).state.state == spool.ABORTED

    def test_an_abort_beats_the_exit_code_it_causes(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """A terminated job exits non-zero, and calling that `failed` would
        blame the training for something a person did."""
        handle = FakeHandle(alive_for=3, code=137)
        engine = FakeEngine(handle, on_start=ask_to_abort)
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        r.tick()
        done = spool.load(entry.path)
        assert done.state.state == spool.ABORTED
        assert done.state.exit_code == 137
        assert f"uid {os.getuid()}" in (done.state.detail or "")

    def test_a_job_nobody_aborts_runs_to_completion(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        handle = FakeHandle(alive_for=3)
        r = a_runner(tmp_path, textfile, FakeEngine(handle))
        entry = a_job(r.queue_dir)
        r.tick()
        assert not handle.terminated
        assert spool.load(entry.path).state.state == spool.FINISHED


class TestAuthorisation:
    """Ownership comes from the filesystem, so these are the tests that would
    catch somebody deciding to read a `user` field instead."""

    def test_a_stranger_may_not_cancel_your_job(
        self, tmp_path: Path, textfile: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        pending = spool.request(entry.path, spool.CANCEL)
        # The request exists but was made by somebody who does not own the job.
        monkeypatch.setattr(
            spool,
            "requests",
            lambda path: [spool.Request(action=spool.CANCEL, uid=99999, path=pending)],
        )
        r.pump()
        monkeypatch.undo()
        assert spool.load(entry.path).state.state == spool.QUEUED

    def test_a_refused_request_is_dropped_rather_than_retried_forever(
        self, tmp_path: Path, textfile: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        pending = spool.request(entry.path, spool.CANCEL)
        monkeypatch.setattr(
            spool,
            "requests",
            lambda path: [spool.Request(action=spool.CANCEL, uid=99999, path=pending)],
        )
        r.pump()
        monkeypatch.undo()
        assert not pending.exists()

    def test_the_owner_may_cancel_their_own(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        spool.request(entry.path, spool.CANCEL)
        r.pump()
        assert spool.load(entry.path).state.state == spool.CANCELLED


class TestPublishing:
    def test_every_pass_leaves_a_heartbeat(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile)
        r.tick()
        published = (textfile / index.QUEUE_FILENAME).read_text()
        assert "sparks_queue_runner_heartbeat_timestamp_seconds 1785849000" in published

    def test_the_queue_is_published_before_a_long_job_finishes(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """Six hours of silence from a healthy runner would trip every alarm
        the heartbeat exists to avoid."""
        published: list[str] = []
        r = a_runner(tmp_path, textfile, FakeEngine(FakeHandle(alive_for=3)))
        a_job(r.queue_dir)
        original = r.publish

        def record() -> None:
            original()
            published.append((textfile / index.QUEUE_FILENAME).read_text())

        r.publish = record  # type: ignore[method-assign]
        r.tick()
        assert any('state="running"' in p for p in published)

    def test_an_unwritable_textfile_directory_does_not_stop_the_queue(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """Telemetry never kills a run. That rule is why the box contract
        preflights the run directory and not Prometheus."""
        r = a_runner(tmp_path, textfile)
        r.textfile_dir = tmp_path / "does-not-exist"
        entry = a_job(r.queue_dir)
        r.tick()
        assert spool.load(entry.path).state.state == spool.FINISHED


class TestReconciliation:
    """What a runner does about the mess the last one left.

    Every case here was found by running the thing for real: a crash between
    "mark it running" and "start the container" left a job that `next_queued`
    skipped forever, so the queue accepted work and silently never ran any of it.
    """

    @pytest.mark.parametrize("stuck", [spool.BUILDING, spool.RUNNING])
    def test_a_job_left_mid_flight_is_failed_not_left_hanging(
        self, tmp_path: Path, textfile: Path, stuck: str
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        spool.set_state(entry.path, spool.State(state=stuck))
        r.reconcile()
        assert spool.load(entry.path).state.state == spool.FAILED

    def test_the_reason_points_at_the_way_out(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile)
        entry = a_job(r.queue_dir)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        r.reconcile()
        assert "sparks retry" in (spool.load(entry.path).state.detail or "")

    def test_a_container_that_outlived_its_supervisor_is_removed(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """It is still holding the GPU, and its job already reads as ended."""
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        spool.set_state(
            entry.path, spool.State(state=spool.RUNNING, container_id="c0ffee")
        )
        r.reconcile()
        assert engine.released == ["c0ffee"]

    def test_queued_and_finished_jobs_are_left_exactly_as_they_were(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        r = a_runner(tmp_path, textfile)
        waiting = a_job(r.queue_dir, name="waiting")
        done = a_job(r.queue_dir, name="done")
        spool.set_state(done.path, spool.State(state=spool.FINISHED, exit_code=0))
        r.reconcile()
        assert spool.load(waiting.path).state.state == spool.QUEUED
        assert spool.load(done.path).state.state == spool.FINISHED

    def test_serving_reconciles_before_it_takes_new_work(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """Otherwise the queue's first act after a reboot is to start a second
        container for something it thinks is already running."""
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        orphan = a_job(r.queue_dir, name="orphan", when=1000.0)
        spool.set_state(orphan.path, spool.State(state=spool.RUNNING))
        fresh = a_job(r.queue_dir, name="fresh", when=2000.0)
        r.serve(ticks=1)
        assert spool.load(orphan.path).state.state == spool.FAILED
        assert [job_id for job_id, _ in engine.started] == [fresh.job.job_id]

    def test_a_release_that_fails_does_not_stop_the_reconcile(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """The daemon may not have come back yet. Leaving the job stuck because
        of that is the bug this whole method exists to fix."""

        class Stubborn(FakeEngine):
            def release(self, container_id: str) -> None:
                raise RuntimeError("cannot reach the docker daemon")

        r = a_runner(tmp_path, textfile, Stubborn())
        entry = a_job(r.queue_dir)
        spool.set_state(
            entry.path, spool.State(state=spool.RUNNING, container_id="c0ffee")
        )
        r.reconcile()
        assert spool.load(entry.path).state.state == spool.FAILED


class TestResilience:
    def test_a_job_that_explodes_does_not_stop_the_runner(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        """`serve` is a loop that must survive anything one job can do to it,
        including the failures nothing here anticipated."""

        class Exploding(FakeEngine):
            def pull(self, image: str, log_path: Path) -> None:
                raise RuntimeError("the daemon went away")

        r = a_runner(tmp_path, textfile, Exploding())
        a_job(r.queue_dir)
        r.serve(ticks=2)
        assert (textfile / index.QUEUE_FILENAME).exists()

    def test_an_unreadable_state_file_is_never_restarted(
        self, tmp_path: Path, textfile: Path
    ) -> None:
        engine = FakeEngine()
        r = a_runner(tmp_path, textfile, engine)
        entry = a_job(r.queue_dir)
        (entry.path / spool.STATE_FILE).write_text("{corrupt")
        r.tick()
        assert engine.started == []
