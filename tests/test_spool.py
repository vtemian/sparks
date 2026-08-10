import json
import os
import time
from pathlib import Path

import pytest

from sparks import spool

IMAGE = "spark.local:5000/demo:1"


def a_job(queue: Path, name: str = "e0", user: str = "rex") -> spool.Entry:
    return spool.submit(
        queue, name=name, user=user, command=["python", "train.py"], image=IMAGE
    )


def test_job_requires_an_image() -> None:
    with pytest.raises(TypeError):
        spool.Job(  # type: ignore[call-arg]
            job_id="job-1",
            name="n",
            user="u",
            command=["true"],
            submitted_unix=1.0,
            # image omitted
        )


def test_from_dict_requires_an_image() -> None:
    with pytest.raises(KeyError, match="image"):
        spool.Job.from_dict(
            {
                "job_id": "job-1",
                "name": "n",
                "user": "u",
                "command": ["true"],
                "submitted_unix": 1.0,
            }
        )


def test_data_dir_is_beside_the_manifest(tmp_path: Path) -> None:
    _, path = spool.reserve(tmp_path, "n", "u")
    entry = spool.commit(
        path,
        spool.Job(
            job_id=path.name,
            name="n",
            user="u",
            command=["true"],
            submitted_unix=1.0,
            image="spark.local:5000/demo:1",
        ),
    )
    assert entry.data_dir == path / "data"


def test_a_submitted_job_is_queued_and_readable_back(tmp_path: Path) -> None:
    entry = a_job(tmp_path)
    assert entry.state.state == spool.QUEUED
    assert spool.load(entry.path).job == entry.job


def test_the_id_carries_who_and_what_so_a_listing_needs_no_lookup(
    tmp_path: Path,
) -> None:
    entry = a_job(tmp_path, name="ablation 3", user="marius")
    assert entry.job.job_id.startswith("job-")
    # Slugged, because a job id reaches Grafana as a label value and a PromQL
    # regex, exactly as a run id does.
    assert "marius" in entry.job.job_id
    assert "ablation-3" in entry.job.job_id


def test_jobs_run_oldest_first(tmp_path: Path) -> None:
    first = spool.submit(
        tmp_path, name="a", user="rex", command=["true"], image=IMAGE, when=1000.0
    )
    second = spool.submit(
        tmp_path, name="b", user="rex", command=["true"], image=IMAGE, when=2000.0
    )
    assert [e.job.job_id for e in spool.entries(tmp_path)] == [
        first.job.job_id,
        second.job.job_id,
    ]
    assert spool.next_queued(tmp_path) is not None
    assert spool.next_queued(tmp_path).job.job_id == first.job.job_id  # type: ignore[union-attr]


def test_a_half_written_submission_is_invisible(tmp_path: Path) -> None:
    _, path = spool.reserve(tmp_path, name="e0", user="rex")
    (path / "context").mkdir()
    (path / "context" / "half.py").write_text("incomplete\n")
    assert spool.entries(tmp_path) == []
    assert spool.next_queued(tmp_path) is None


def test_state_is_absent_until_the_runner_writes_it(tmp_path: Path) -> None:
    entry = a_job(tmp_path)
    assert not (entry.path / spool.STATE_FILE).exists()
    assert entry.state.state == spool.QUEUED


def test_the_runner_records_progress_and_it_survives_a_reload(
    tmp_path: Path,
) -> None:
    entry = a_job(tmp_path)
    spool.set_state(
        entry.path,
        spool.State(
            state=spool.RUNNING,
            image="sha256:abc",
            run_id="run-20260806-120000-rex-e0",
            container_id="deadbeef",
            started_unix=1234.0,
        ),
    )
    reloaded = spool.load(entry.path)
    assert reloaded.state.state == spool.RUNNING
    assert reloaded.state.run_id == "run-20260806-120000-rex-e0"
    assert reloaded.state.container_id == "deadbeef"
    # The submission is untouched by anything the runner does.
    assert reloaded.job == entry.job


def test_a_finished_job_is_terminal_and_a_running_one_is_not(tmp_path: Path) -> None:
    entry = a_job(tmp_path)
    assert not entry.is_terminal
    spool.set_state(entry.path, spool.State(state=spool.FINISHED, exit_code=0))
    assert spool.load(entry.path).is_terminal


def test_one_unreadable_job_does_not_hide_the_others(tmp_path: Path) -> None:
    good = a_job(tmp_path, name="good")
    broken = a_job(tmp_path, name="broken")
    (broken.path / spool.JOB_FILE).write_text("{not json")
    assert [e.job.name for e in spool.entries(tmp_path)] == ["good"]
    assert spool.load(good.path).job.name == "good"


def test_a_state_file_from_a_future_version_leaves_the_job_alone(
    tmp_path: Path,
) -> None:
    entry = a_job(tmp_path)
    (entry.path / spool.STATE_FILE).write_text("{not json")
    assert spool.load(entry.path).state.state == spool.UNKNOWN
    assert spool.next_queued(tmp_path) is None


def test_provenance_is_recorded_when_there_is_any(tmp_path: Path) -> None:
    entry = spool.submit(
        tmp_path,
        name="e0",
        user="rex",
        command=["true"],
        image=IMAGE,
        git_sha="abc1234",
        git_dirty=True,
    )
    stored = json.loads((entry.path / spool.JOB_FILE).read_text())
    assert stored["git_sha"] == "abc1234"
    assert stored["git_dirty"] is True


def test_a_retry_says_what_it_is_a_retry_of(tmp_path: Path) -> None:
    original = a_job(tmp_path)
    again = spool.submit(
        tmp_path,
        name=original.job.name,
        user=original.job.user,
        command=original.job.command,
        image=original.job.image,
        retry_of=original.job.job_id,
    )
    assert again.job.retry_of == original.job.job_id
    assert again.job.job_id != original.job.job_id


class TestRequests:
    def test_a_request_carries_who_made_it(self, tmp_path: Path) -> None:
        entry = a_job(tmp_path)
        spool.request(entry.path, spool.ABORT)
        pending = spool.requests(entry.path)
        assert [r.action for r in pending] == [spool.ABORT]
        # Taken from the filesystem, never from anything inside the file: you
        # cannot create a file owned by somebody else, so this cannot be forged.
        assert pending[0].uid == os.getuid()

    def test_two_people_can_ask_at_once(self, tmp_path: Path) -> None:
        entry = a_job(tmp_path)
        spool.request(entry.path, spool.ABORT)
        spool.request(entry.path, spool.CANCEL)
        assert {r.action for r in spool.requests(entry.path)} == {
            spool.ABORT,
            spool.CANCEL,
        }

    def test_asking_twice_is_not_an_error(self, tmp_path: Path) -> None:
        entry = a_job(tmp_path)
        spool.request(entry.path, spool.ABORT)
        spool.request(entry.path, spool.ABORT)
        assert len(spool.requests(entry.path)) == 1

    def test_a_handled_request_is_cleared(self, tmp_path: Path) -> None:
        entry = a_job(tmp_path)
        spool.request(entry.path, spool.ABORT)
        spool.clear_requests(entry.path)
        assert spool.requests(entry.path) == []

    def test_the_owner_of_a_job_is_the_account_that_submitted_it(
        self, tmp_path: Path
    ) -> None:
        entry = a_job(tmp_path, user="someone-else")
        # The *filesystem* owner, not the name in the file. The name is a label;
        # this is the authorisation.
        assert entry.owner_uid == os.getuid()
        assert entry.may_be_controlled_by(os.getuid())
        assert not entry.may_be_controlled_by(os.getuid() + 1)

    def test_root_may_control_anything(self, tmp_path: Path) -> None:
        assert a_job(tmp_path).may_be_controlled_by(0)


class TestRetention:
    def test_a_removed_job_takes_its_context_with_it(self, tmp_path: Path) -> None:
        entry = a_job(tmp_path)
        (entry.path / "context").mkdir()
        (entry.path / "context" / "big.bin").write_bytes(b"x" * 1024)
        spool.remove(entry.path)
        assert not entry.path.exists()
        assert spool.entries(tmp_path) == []

    def test_recently_finished_jobs_stay_visible(self, tmp_path: Path) -> None:
        now = time.time()
        fresh = a_job(tmp_path, name="fresh")
        stale = a_job(tmp_path, name="stale")
        spool.set_state(
            fresh.path, spool.State(state=spool.FINISHED, finished_unix=now - 60)
        )
        spool.set_state(
            stale.path, spool.State(state=spool.FINISHED, finished_unix=now - 100_000)
        )
        names = {e.job.name for e in spool.publishable(tmp_path, now=now)}
        assert names == {"fresh"}

    def test_a_job_that_is_still_going_is_always_published(
        self, tmp_path: Path
    ) -> None:
        now = time.time()
        entry = a_job(tmp_path)
        spool.set_state(
            entry.path, spool.State(state=spool.RUNNING, started_unix=now - 700_000)
        )
        assert [e.job.name for e in spool.publishable(tmp_path, now=now)] == ["e0"]


def test_the_queue_directory_is_group_writable_but_sticky(tmp_path: Path) -> None:
    queue = spool.make_queue_dir(tmp_path / "queue")
    assert queue.stat().st_mode & 0o7777 == spool.QUEUE_MODE
    assert spool.QUEUE_MODE & 0o1000, "sticky"


def test_submitting_creates_the_queue_directory_if_it_is_missing(
    tmp_path: Path,
) -> None:
    entry = a_job(tmp_path / "not-there-yet")
    assert entry.path.is_dir()


@pytest.mark.parametrize("state", [spool.QUEUED, spool.BUILDING, spool.RUNNING])
def test_live_states_are_not_terminal(tmp_path: Path, state: str) -> None:
    entry = a_job(tmp_path)
    spool.set_state(entry.path, spool.State(state=state))
    assert not spool.load(entry.path).is_terminal


@pytest.mark.parametrize(
    "state", [spool.FINISHED, spool.FAILED, spool.CANCELLED, spool.ABORTED]
)
def test_terminal_states_are_terminal(tmp_path: Path, state: str) -> None:
    entry = a_job(tmp_path)
    spool.set_state(entry.path, spool.State(state=state))
    assert spool.load(entry.path).is_terminal


def test_the_job_is_owned_by_the_account_ssh_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # owner_uid is read from job.json, which write_atomically recreates as
    # whoever ran commit -- root, inside the queue container. Without this the
    # privilege drop has nothing to drop to and the job runs as host root.
    chowned: list[tuple[Path, int]] = []
    monkeypatch.setenv("SPARKS_SSH_UID", "501")
    monkeypatch.setattr("sparks.spool.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "sparks.spool.os.chown",
        lambda path, uid, _gid: chowned.append((Path(path), uid)),
    )

    _, path = spool.reserve(tmp_path, "e0", "rex")
    spool.commit(
        path,
        spool.Job(
            job_id=path.name,
            name="e0",
            user="rex",
            command=["true"],
            submitted_unix=1.0,
            image=IMAGE,
        ),
    )

    assert (path, 501) in chowned
    assert (path / spool.JOB_FILE, 501) in chowned


def test_a_claimed_uid_is_ignored_when_ssh_did_not_vouch_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No SPARKS_SSH_UID means nobody authenticated: leave ownership alone
    # rather than trusting whatever the client asked for.
    chowned: list[tuple[Path, int]] = []
    monkeypatch.delenv("SPARKS_SSH_UID", raising=False)
    monkeypatch.setattr("sparks.spool.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "sparks.spool.os.chown",
        lambda path, uid, _gid: chowned.append((Path(path), uid)),
    )

    # Other uid-shaped variables in the environment are not a substitute: only
    # the one fire-ctl set from the authenticated connection counts.
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SPARKS_CLAIMED_UID", "501")

    _, path = spool.reserve(tmp_path, "e0", "rex")
    spool.commit(
        path,
        spool.Job(
            job_id=path.name,
            name="e0",
            user="rex",
            command=["true"],
            submitted_unix=1.0,
            image=IMAGE,
        ),
    )

    assert chowned == []
