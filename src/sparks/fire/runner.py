import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sparks import index, spool

LOG = logging.getLogger("sparks")

POLL_SECONDS = 2.0


class Handle(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def run_id(self) -> str | None: ...

    def container_id(self) -> str | None: ...

    def finish(self) -> None: ...


class Engine(Protocol):
    def pull(self, image: str, log_path: Path) -> None: ...

    def start(self, entry: spool.Entry, image: str, log_path: Path) -> Handle: ...

    def release(self, container_id: str) -> None: ...


class PullFailedError(Exception): ...


@dataclass
class Runner:
    queue_dir: Path
    engine: Engine
    textfile_dir: Path
    poll_seconds: float = POLL_SECONDS
    # Injected so a test spends no real seconds and can pin the stamped clock.
    sleep: Callable[[float], None] = field(default=time.sleep)
    now: Callable[[], float] = field(default=time.time)

    def serve(self, ticks: int | None = None) -> None:
        self.reconcile()
        pass_count = 0
        while ticks is None or pass_count < ticks:
            pass_count += 1
            try:
                started_one = self.tick()
            except Exception:  # a bad job must never stop the queue forever
                LOG.exception("sparks: the runner's pass failed")
                started_one = False
            if not started_one:
                self.sleep(self.poll_seconds)

    def reconcile(self) -> None:
        for entry in spool.entries(self.queue_dir):
            if entry.state.state not in (spool.BUILDING, spool.RUNNING):
                continue
            LOG.warning(
                "sparks: %s was %s when the runner stopped; marking it failed",
                entry.job.job_id,
                entry.state.state,
            )
            if entry.state.container_id:
                # A container outlives the client that started it, so it may
                # genuinely still be running and holding the GPU.
                with contextlib.suppress(Exception):
                    self.engine.release(entry.state.container_id)
            spool.advance(
                entry.path,
                state=spool.FAILED,
                finished_unix=self.now(),
                detail=(
                    f"the runner stopped while this job was {entry.state.state}. "
                    f"Nothing is running it now; sparks retry to try again"
                ),
            )
            spool.clear_requests(entry.path)
        self.publish()

    def tick(self) -> bool:
        self.pump()
        entry = spool.next_queued(self.queue_dir)
        if entry is None:
            return False
        self.process(entry)
        self.pump()
        return True

    def pump(self) -> None:
        entries = spool.entries(self.queue_dir)
        for entry in entries:
            if entry.state.state == spool.QUEUED:
                self._apply_queued_requests(entry)
            elif entry.is_terminal:
                # A request left on a stopped job would be replayed forever.
                spool.clear_requests(entry.path)
        self.publish()

    def publish(self) -> None:
        try:
            index.publish_queue(
                spool.publishable(self.queue_dir, now=self.now()),
                self.textfile_dir / index.QUEUE_FILENAME,
                heartbeat=self.now(),
            )
        except OSError as exc:
            # Losing the view of the queue must not stop the queue.
            LOG.warning("sparks: could not publish the queue: %s", exc)

    def process(self, entry: spool.Entry) -> None:
        if not entry.job.image:
            self._fail(
                entry,
                "job has no image; rebuild and submit from a laptop",
            )
            return
        if not entry.data_dir.is_dir():
            self._fail(entry, "job data/ directory is missing")
            return
        try:
            self.engine.pull(entry.job.image, entry.path / spool.PULL_LOG)
        except PullFailedError as exc:
            LOG.warning("sparks: %s failed to pull: %s", entry.job.job_id, exc)
            self._fail(entry, f"pull failed: {exc}")
            return
        self._run(entry, entry.job.image)

    def _fail(self, entry: spool.Entry, detail: str) -> None:
        spool.advance(
            entry.path,
            state=spool.FAILED,
            finished_unix=self.now(),
            detail=detail,
        )
        spool.clear_requests(entry.path)
        self.publish()

    def _run(self, entry: spool.Entry, image: str) -> None:
        started = self.now()
        spool.advance(
            entry.path, state=spool.RUNNING, image=image, started_unix=started
        )
        self.publish()
        handle = self.engine.start(entry, image, entry.path / spool.LAUNCH_LOG)
        try:
            aborted_by = self._wait(entry, handle)
            code = handle.poll()
        finally:
            # Before the state is written, so a container that outlived its
            # supervisor is gone by the time the job reads as terminal.
            handle.finish()
        spool.advance(
            entry.path,
            state=self._outcome(code, aborted_by is not None),
            run_id=handle.run_id(),
            container_id=handle.container_id(),
            exit_code=code,
            finished_unix=self.now(),
            detail=None if aborted_by is None else f"aborted by uid {aborted_by}",
        )
        spool.clear_requests(entry.path)
        self.publish()

    def _wait(self, entry: spool.Entry, handle: Handle) -> int | None:
        aborted_by = None
        while handle.poll() is None:
            if aborted_by is None:
                aborted_by = self._abort_requested(entry)
                if aborted_by is not None:
                    LOG.info("sparks: uid %d aborted %s", aborted_by, entry.job.job_id)
                    handle.terminate()
            # So the run id and container id appear as soon as they are known,
            # rather than only in the terminal write.
            spool.advance(
                entry.path,
                run_id=handle.run_id(),
                container_id=handle.container_id(),
            )
            self.publish()
            self.sleep(self.poll_seconds)
        return aborted_by

    def _abort_requested(self, entry: spool.Entry) -> int | None:
        for pending in spool.requests(entry.path):
            if pending.action != spool.ABORT:
                continue
            if entry.may_be_controlled_by(pending.uid):
                return pending.uid
            self._refuse(entry, pending)
        return None

    def _apply_queued_requests(self, entry: spool.Entry) -> None:
        for pending in spool.requests(entry.path):
            if not entry.may_be_controlled_by(pending.uid):
                self._refuse(entry, pending)
                continue
            spool.advance(
                entry.path,
                state=spool.CANCELLED,
                finished_unix=self.now(),
                detail=f"cancelled by uid {pending.uid}",
            )
            spool.clear_requests(entry.path)
            return

    def _refuse(self, entry: spool.Entry, pending: spool.Request) -> None:
        LOG.warning(
            "sparks: uid %d may not %s %s, which belongs to uid %d",
            pending.uid,
            pending.action,
            entry.job.job_id,
            entry.owner_uid,
        )
        try:
            pending.path.unlink()
        except OSError as exc:
            LOG.warning("sparks: could not clear the refused request: %s", exc)

    @staticmethod
    def _outcome(code: int | None, aborted: bool) -> str:
        if aborted:
            return spool.ABORTED
        return spool.FINISHED if code == 0 else spool.FAILED
