"""Submitting, listing and the four lifecycle verbs, from the side a person
touches."""

import os
from pathlib import Path

import pytest

from sparks import client, spool


@pytest.fixture
def project(tmp_path: Path) -> Path:
    context = tmp_path / "project"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n")
    (context / "train.py").write_text("print('hi')\n")
    return context


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    return tmp_path / "queue"


class TestSubmitting:
    def test_the_code_travels_with_the_job(self, queue: Path, project: Path) -> None:
        entry = client.submit(
            queue, name="e0", command=["python", "train.py"], context=project
        )
        assert (entry.context_dir / "train.py").read_text() == "print('hi')\n"
        assert (entry.context_dir / "Dockerfile").is_file()

    def test_a_job_with_an_image_needs_no_code(self, queue: Path) -> None:
        entry = client.submit(queue, name="e0", command=["true"], image="ghcr.io/x/y:1")
        assert entry.job.image == "ghcr.io/x/y:1"
        assert not entry.context_dir.exists()

    def test_a_job_with_neither_is_refused(self, queue: Path) -> None:
        with pytest.raises(client.ClientError, match="--context"):
            client.submit(queue, name="e0", command=["true"], context=None)

    def test_the_manifest_is_written_after_the_code(
        self, queue: Path, project: Path
    ) -> None:
        """Anything else lets the runner build a half-copied tree."""
        entry = client.submit(queue, name="e0", command=["true"], context=project)
        manifest = entry.path / spool.JOB_FILE
        assert (
            manifest.stat().st_mtime >= (entry.context_dir / "train.py").stat().st_mtime
        )

    def test_git_metadata_is_recorded_when_there_is_a_checkout(
        self, queue: Path, project: Path
    ) -> None:
        """Nothing here is a git repo, so this is the honest 'unknown' path."""
        entry = client.submit(queue, name="e0", command=["true"], context=project)
        assert entry.job.git_sha == "unknown"
        assert entry.job.git_dirty is False


class TestWhatIsShipped:
    def test_the_git_directory_is_never_copied(
        self, queue: Path, project: Path
    ) -> None:
        """It is the biggest thing in most checkouts and no build needs it."""
        (project / ".git").mkdir()
        (project / ".git" / "enormous.pack").write_bytes(b"x" * 4096)
        entry = client.submit(queue, name="e0", command=["true"], context=project)
        assert not (entry.context_dir / ".git").exists()

    def test_dockerignore_is_honoured(self, queue: Path, project: Path) -> None:
        """So a project that already excludes its datasets does not have them
        copied into the queue on every submit."""
        (project / ".dockerignore").write_text("data/\n")
        (project / "data").mkdir()
        (project / "data" / "huge.bin").write_bytes(b"x" * 4096)
        entry = client.submit(queue, name="e0", command=["true"], context=project)
        assert not (entry.context_dir / "data").exists()
        assert (entry.context_dir / "train.py").is_file()

    def test_the_source_slash_is_not_lost(self, project: Path) -> None:
        """Without the trailing slash rsync nests the directory inside the
        destination, and the Dockerfile is then one level too deep."""
        argv = client.rsync_argv(project, "/dest")
        assert argv[-2] == f"{project}/"

    def test_a_context_that_is_not_there_says_so(self, queue: Path) -> None:
        with pytest.raises(client.ClientError, match="not a directory"):
            client.submit(
                queue, name="e0", command=["true"], context=Path("/no/such/place")
            )


class TestFindingAJob:
    def test_a_full_id_matches(self, queue: Path, project: Path) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        assert client.resolve(queue, entry.job.job_id).job.job_id == entry.job.job_id

    def test_a_unique_fragment_matches(self, queue: Path, project: Path) -> None:
        """Job ids are long and nobody retypes them."""
        entry = client.submit(queue, "distinctive", ["true"], context=project)
        assert client.resolve(queue, "distinctive").job.job_id == entry.job.job_id

    def test_an_ambiguous_name_is_refused_rather_than_guessed(
        self, queue: Path, project: Path
    ) -> None:
        """Guessing wrong here aborts somebody's training."""
        for _ in range(3):
            entry = client.submit(queue, "same", ["true"], context=project)
            spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(client.ClientError, match="matches several"):
            client.resolve(queue, "same")

    def test_one_live_job_among_finished_ones_is_not_ambiguous(
        self, queue: Path, project: Path
    ) -> None:
        """`sparks abort e0` after six finished attempts means the one running."""
        old = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(old.path, spool.State(state=spool.FINISHED))
        live = client.submit(queue, "e0", ["true"], context=project)
        assert client.resolve(queue, "e0").job.job_id == live.job.job_id

    def test_nothing_matching_says_so(self, queue: Path, project: Path) -> None:
        client.submit(queue, "e0", ["true"], context=project)
        with pytest.raises(client.ClientError, match="no job matches"):
            client.resolve(queue, "nonsense")

    def test_an_empty_queue_says_that_instead(self, queue: Path) -> None:
        spool.make_queue_dir(queue)
        with pytest.raises(client.ClientError, match="no jobs"):
            client.resolve(queue, "anything")


class TestStopping:
    def test_asking_to_abort_leaves_a_request(self, queue: Path, project: Path) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        client.ask(queue, "e0", spool.ABORT)
        assert [r.action for r in spool.requests(entry.path)] == [spool.ABORT]

    def test_a_job_that_already_ended_cannot_be_stopped(
        self, queue: Path, project: Path
    ) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(client.ClientError, match="nothing to stop"):
            client.ask(queue, "e0", spool.ABORT)

    def test_removing_a_running_job_is_refused_with_the_verb_that_works(
        self, queue: Path, project: Path
    ) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(client.ClientError, match="sparks abort"):
            client.remove(queue, "e0")

    def test_removing_a_finished_job_takes_the_context_with_it(
        self, queue: Path, project: Path
    ) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        client.remove(queue, "e0")
        assert not entry.path.exists()


class TestRetry:
    def test_a_retry_reuses_the_code_without_copying_it_again(
        self, queue: Path, project: Path
    ) -> None:
        """A build context can be gigabytes, and a retry does not change it."""
        entry = client.submit(queue, "e0", ["python", "train.py"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.FAILED, exit_code=1))
        again = client.retry(queue, spool.load(entry.path))
        assert (again.context_dir / "train.py").is_file()
        assert again.job.retry_of == entry.job.job_id
        assert again.job.command == ["python", "train.py"]
        assert again.state.state == spool.QUEUED

    def test_retrying_something_still_going_is_refused(
        self, queue: Path, project: Path
    ) -> None:
        """It would run the same thing twice at once, on one GPU."""
        entry = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(client.ClientError, match=r"twice at once"):
            client.retry(queue, spool.load(entry.path))

    def test_the_retry_is_owned_by_whoever_retried_it(
        self, queue: Path, project: Path
    ) -> None:
        entry = client.submit(
            queue, "e0", ["true"], context=project, user="someone-else"
        )
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        again = client.retry(queue, spool.load(entry.path))
        assert again.owner_uid == os.getuid()


class TestListing:
    def test_an_empty_queue_says_so_rather_than_printing_a_bare_header(
        self,
    ) -> None:
        assert client.render([]) == "the queue is empty\n"

    def test_the_columns_line_up(self, queue: Path, project: Path) -> None:
        client.submit(queue, "short", ["true"], context=project)
        client.submit(queue, "a-much-longer-name", ["true"], context=project)
        lines = client.render(spool.entries(queue)).splitlines()
        assert len(lines) == 3
        # The state column starts at the same offset on both rows, which it
        # only does if the widest job id set the width.
        assert len({line.index("queued") for line in lines[1:]}) == 1

    def test_a_running_job_shows_its_run(self, queue: Path, project: Path) -> None:
        entry = client.submit(queue, "e0", ["true"], context=project)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING, run_id="run-abc"))
        assert "run-abc" in client.render(spool.entries(queue))

    @pytest.mark.parametrize(
        ("seconds", "shown"),
        [(5, "5s"), (90, "1m"), (7200, "2.0h"), (200_000, "2d")],
    )
    def test_ages_are_readable(self, seconds: float, shown: str) -> None:
        assert client._duration(seconds) == shown


class TestReachingTheBox:
    def test_the_box_decides_who_submitted_not_this_laptop(self) -> None:
        """Ownership of the files decides who may abort a job, and over ssh that
        is the account on the box. Naming this laptop's account made one person
        show up as two: the queue said `whitemonk`, the run said `vlad`."""
        argv = client.commit_argv("/q/job-1", "e0", ["true"], "abc", False, None)
        assert "--user" not in argv

    def test_the_command_survives_the_separator(self) -> None:
        """Everything after `--` is the job's, and flags in it must reach the
        job rather than being read by the commit itself."""
        argv = client.commit_argv(
            "/q/job-1",
            "e0",
            ["python", "-m", "train", "--lr", "3e-4"],
            "abc",
            True,
            None,
        )
        assert argv[argv.index("--") + 1 :] == ["python", "-m", "train", "--lr", "3e-4"]
        assert "--git-dirty" in argv[: argv.index("--")]

    def test_the_host_flag_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(client.HOST_ENV, "from-env")
        assert client.host_from("explicit") == "explicit"
        assert client.host_from(None) == "from-env"

    def test_no_host_anywhere_means_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(client.HOST_ENV, raising=False)
        assert client.host_from(None) is None
