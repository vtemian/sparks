"""Submitting, listing and the four lifecycle verbs, from the side a person
touches."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sparks import client, spool

IMAGE = "spark.local:5000/test/job:1"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    context = tmp_path / "project"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n")
    (context / "train.py").write_text("print('hi')\n")
    return context


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "upload"
    folder.mkdir()
    (folder / "corpus.txt").write_text("hi\n")
    return folder


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    return tmp_path / "queue"


def _submit(
    queue: Path,
    data: Path,
    name: str = "e0",
    command: list[str] | None = None,
    image: str = IMAGE,
    **kwargs: Any,
) -> spool.Entry:
    return client.submit(
        queue,
        name=name,
        command=command or ["true"],
        data=data,
        image=image,
        **kwargs,
    )


class TestTagFor:
    def test_tag_for_uses_registry_user_and_name(self) -> None:
        assert (
            client.tag_for("http://spark.local:5000", "vlad", "exp", "abc1234")
            == "spark.local:5000/vlad/exp:abc1234"
        )

    def test_tag_for_strips_trailing_slash(self) -> None:
        assert (
            client.tag_for("http://spark.local:5000/", "u", "n", "r")
            == "spark.local:5000/u/n:r"
        )


class TestSubmitting:
    def test_the_data_travels_with_the_job(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data, command=["python", "train.py"])
        assert (entry.data_dir / "corpus.txt").read_text() == "hi\n"
        assert entry.job.image == IMAGE
        assert not entry.context_dir.exists()

    def test_a_job_with_an_image_needs_no_build(
        self, queue: Path, data: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            client,
            "build",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        entry = _submit(queue, data, image="ghcr.io/x/y:1")
        assert entry.job.image == "ghcr.io/x/y:1"

    def test_a_job_with_neither_image_nor_context_is_refused(
        self, queue: Path, data: Path
    ) -> None:
        with pytest.raises(client.ClientError, match=r"--image|--context"):
            client.submit(queue, name="e0", command=["true"], data=data)

    def test_missing_data_is_refused(self, queue: Path) -> None:
        with pytest.raises(client.ClientError, match="not a directory"):
            client.submit(
                queue,
                name="e0",
                command=["true"],
                data=Path("/no/such/data"),
                image=IMAGE,
            )

    def test_the_manifest_is_written_after_the_data(
        self, queue: Path, data: Path
    ) -> None:
        """Anything else lets the runner mount a half-copied tree."""
        entry = _submit(queue, data)
        manifest = entry.path / spool.JOB_FILE
        assert (
            manifest.stat().st_mtime >= (entry.data_dir / "corpus.txt").stat().st_mtime
        )

    def test_git_metadata_is_recorded_when_there_is_a_checkout(
        self, queue: Path, data: Path, project: Path
    ) -> None:
        """Nothing here is a git repo, so this is the honest 'unknown' path."""
        entry = client.submit(
            queue,
            name="e0",
            command=["true"],
            data=data,
            context=project,
            image=IMAGE,
        )
        assert entry.job.git_sha == "unknown"
        assert entry.job.git_dirty is False


class TestWhatIsShipped:
    def test_the_git_directory_is_never_copied(
        self, data: Path, tmp_path: Path
    ) -> None:
        """It is the biggest thing in most checkouts and no job needs it."""
        (data / ".git").mkdir()
        (data / ".git" / "enormous.pack").write_bytes(b"x" * 4096)
        dest = tmp_path / "dest"
        client.ship(data, dest)
        assert not (dest / ".git").exists()
        assert (dest / "corpus.txt").is_file()

    def test_dockerignore_is_honoured(self, project: Path, tmp_path: Path) -> None:
        """So a project that already excludes its datasets does not have them
        copied when shipping a tree that uses .dockerignore."""
        (project / ".dockerignore").write_text("data/\n")
        (project / "data").mkdir()
        (project / "data" / "huge.bin").write_bytes(b"x" * 4096)
        dest = tmp_path / "dest"
        client.ship(project, dest)
        assert not (dest / "data").exists()
        assert (dest / "train.py").is_file()

    def test_the_source_slash_is_not_lost(self, project: Path) -> None:
        """Without the trailing slash rsync nests the directory inside the
        destination, and `/data` would then be one level too deep."""
        argv = client.rsync_argv(project, "/dest")
        assert argv[-2] == f"{project}/"


class TestFindingAJob:
    def test_a_full_id_matches(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        assert client.resolve(queue, entry.job.job_id).job.job_id == entry.job.job_id

    def test_a_unique_fragment_matches(self, queue: Path, data: Path) -> None:
        """Job ids are long and nobody retypes them."""
        entry = _submit(queue, data, name="distinctive")
        assert client.resolve(queue, "distinctive").job.job_id == entry.job.job_id

    def test_an_ambiguous_name_is_refused_rather_than_guessed(
        self, queue: Path, data: Path
    ) -> None:
        """Guessing wrong here aborts somebody's training."""
        for _ in range(3):
            entry = _submit(queue, data, name="same")
            spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(client.ClientError, match="matches several"):
            client.resolve(queue, "same")

    def test_one_live_job_among_finished_ones_is_not_ambiguous(
        self, queue: Path, data: Path
    ) -> None:
        """`sparks abort e0` after six finished attempts means the one running."""
        old = _submit(queue, data)
        spool.set_state(old.path, spool.State(state=spool.FINISHED))
        live = _submit(queue, data)
        assert client.resolve(queue, "e0").job.job_id == live.job.job_id

    def test_nothing_matching_says_so(self, queue: Path, data: Path) -> None:
        _submit(queue, data)
        with pytest.raises(client.ClientError, match="no job matches"):
            client.resolve(queue, "nonsense")

    def test_an_empty_queue_says_that_instead(self, queue: Path) -> None:
        spool.make_queue_dir(queue)
        with pytest.raises(client.ClientError, match="no jobs"):
            client.resolve(queue, "anything")


class TestStopping:
    def test_asking_to_abort_leaves_a_request(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        client.ask(queue, "e0", spool.ABORT)
        assert [r.action for r in spool.requests(entry.path)] == [spool.ABORT]

    def test_a_job_that_already_ended_cannot_be_stopped(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(client.ClientError, match="nothing to stop"):
            client.ask(queue, "e0", spool.ABORT)

    def test_removing_a_running_job_is_refused_with_the_verb_that_works(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(client.ClientError, match="sparks abort"):
            client.remove(queue, "e0")

    def test_removing_a_finished_job_takes_the_context_with_it(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        client.remove(queue, "e0")
        assert not entry.path.exists()


class TestRetry:
    def test_a_retry_reuses_the_image_and_keeps_the_command(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data, command=["python", "train.py"])
        spool.set_state(entry.path, spool.State(state=spool.FAILED, exit_code=1))
        again = client.retry(queue, spool.load(entry.path))
        assert again.job.retry_of == entry.job.job_id
        assert again.job.command == ["python", "train.py"]
        assert again.job.image == entry.job.image
        assert again.state.state == spool.QUEUED

    def test_retrying_something_still_going_is_refused(
        self, queue: Path, data: Path
    ) -> None:
        """It would run the same thing twice at once, on one GPU."""
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(client.ClientError, match=r"twice at once"):
            client.retry(queue, spool.load(entry.path))

    def test_the_retry_is_owned_by_whoever_retried_it(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data, user="someone-else")
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        again = client.retry(queue, spool.load(entry.path))
        assert again.owner_uid == os.getuid()


class TestListing:
    def test_an_empty_queue_says_so_rather_than_printing_a_bare_header(
        self,
    ) -> None:
        assert client.render([]) == "the queue is empty\n"

    def test_the_columns_line_up(self, queue: Path, data: Path) -> None:
        _submit(queue, data, name="short")
        _submit(queue, data, name="a-much-longer-name")
        lines = client.render(spool.entries(queue)).splitlines()
        assert len(lines) == 3
        # The state column starts at the same offset on both rows, which it
        # only does if the widest job id set the width.
        assert len({line.index("queued") for line in lines[1:]}) == 1

    def test_a_running_job_shows_its_run(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING, run_id="run-abc"))
        assert "run-abc" in client.render(spool.entries(queue))

    @pytest.mark.parametrize(
        ("seconds", "shown"),
        [(5, "5s"), (90, "1m"), (7200, "2.0h"), (200_000, "2d")],
    )
    def test_ages_are_readable(self, seconds: float, shown: str) -> None:
        assert client._duration(seconds) == shown


class TestReachingTheBox:
    def test_commit_argv_requires_image(self) -> None:
        argv = client.commit_argv(
            "/srv/spark/queue/job-1",
            name="n",
            command=["python", "train.py"],
            sha="deadbeef",
            dirty=False,
            image="spark.local:5000/vlad/n:deadbeef",
        )
        assert "--image" in argv
        assert "spark.local:5000/vlad/n:deadbeef" in argv

    def test_the_box_decides_who_submitted_not_this_laptop(self) -> None:
        """Ownership of the files decides who may abort a job, and over ssh that
        is the account on the box. Naming this laptop's account made one person
        show up as two: the queue said `whitemonk`, the run said `vlad`."""
        argv = client.commit_argv("/q/job-1", "e0", ["true"], "abc", False, IMAGE)
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
            IMAGE,
        )
        assert argv[argv.index("--") + 1 :] == ["python", "-m", "train", "--lr", "3e-4"]
        assert "--git-dirty" in argv[: argv.index("--")]
        assert "--image" in argv[: argv.index("--")]

    def test_a_quoted_command_survives_the_trip(self) -> None:
        """ssh joins its arguments and hands them to a shell on the far side, so
        passing them separately only looks safe. A real submit came back as a
        bash syntax error because the job's own semicolon was executed there."""
        argv = client.ssh_argv("box", ["submit", "--", "sh", "-c", "echo a; echo b"])
        assert argv[:2] == ["ssh", "box"]
        # One argument to ssh, and the dangerous parts are inert inside it.
        assert len(argv) == 3
        assert "'echo a; echo b'" in argv[2]

    def test_a_command_with_no_metacharacters_stays_readable(self) -> None:
        """Quoting everything unconditionally would make every command in the
        logs unreadable for the sake of the rare one that needs it."""
        argv = client.ssh_argv("box", ["queue", "--all"])
        assert argv[2] == "sparks queue --all"

    def test_the_host_flag_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(client.HOST_ENV, "from-env")
        assert client.host_from("explicit") == "explicit"
        assert client.host_from(None) == "from-env"

    def test_no_host_anywhere_means_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(client.HOST_ENV, raising=False)
        assert client.host_from(None) is None


class TestBuildAndPush:
    def test_build_refuses_a_missing_dockerfile(self, tmp_path: Path) -> None:
        with pytest.raises(client.ClientError, match="Dockerfile is missing"):
            client.build(tmp_path, "tag:1")

    def test_build_invokes_docker(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr("sparks.client.subprocess.run", fake_run)
        client.build(project, "spark.local:5000/u/n:r")
        assert seen == [
            ["docker", "build", "-t", "spark.local:5000/u/n:r", str(project)]
        ]

    def test_push_failure_is_helpful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sparks.client.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1),
        )
        with pytest.raises(client.ClientError, match="insecure-registries"):
            client.push("spark.local:5000/u/n:r")


class TestSubmitRemote:
    def test_submit_remote_builds_pushes_rsyncs_data_then_commits(
        self,
        project: Path,
        data: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(("run", list(argv)))
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        def fake_remote_capture(host: str, argv: list[str]) -> str:
            calls.append(("remote", list(argv)))
            if argv[0] == "reserve":
                return "/q/job-1"
            return "job-1"

        monkeypatch.setattr("sparks.client.subprocess.run", fake_run)
        monkeypatch.setattr(client, "remote_capture", fake_remote_capture)
        monkeypatch.setattr(client, "local_user", lambda: "vlad")
        monkeypatch.setattr(client, "provenance", lambda _ctx: ("abc1234", False))

        job_id = client.submit_remote(
            "box",
            name="exp",
            command=["python", "train.py"],
            context=project,
            data=data,
            registry_url="http://spark.local:5000",
        )
        assert job_id == "job-1"

        kinds = [kind for kind, _ in calls]
        assert kinds == ["run", "run", "remote", "run", "remote"]

        tag = "spark.local:5000/vlad/exp:abc1234"
        assert calls[0] == (
            "run",
            ["docker", "build", "-t", tag, str(project)],
        )
        assert calls[1] == ("run", ["docker", "push", tag])
        assert calls[2] == ("remote", ["reserve", "--name", "exp"])
        assert calls[3][1][0] == "rsync"
        assert calls[3][1][-1] == f"box:/q/job-1/{spool.DATA_DIR}/"
        assert f"{data}/" in calls[3][1]
        assert "context" not in "".join(calls[3][1])
        commit = calls[4][1]
        assert commit[0] == "commit"
        assert "--image" in commit
        assert tag in commit

    def test_submit_remote_skips_build_when_image_given(
        self,
        project: Path,
        data: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(("run", list(argv)))
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        def fake_remote_capture(host: str, argv: list[str]) -> str:
            calls.append(("remote", list(argv)))
            if argv[0] == "reserve":
                return "/q/job-1"
            return "job-1"

        monkeypatch.setattr("sparks.client.subprocess.run", fake_run)
        monkeypatch.setattr(client, "remote_capture", fake_remote_capture)
        monkeypatch.setattr(client, "provenance", lambda _ctx: ("abc1234", False))

        client.submit_remote(
            "box",
            name="exp",
            command=["true"],
            context=project,
            data=data,
            image="already:pushed",
        )
        assert all(c[1][:1] != ["docker"] for c in calls if c[0] == "run")
        commit = next(
            argv
            for kind, argv in calls
            if kind == "remote" and argv[0] == "commit"
        )
        assert "already:pushed" in commit

    def test_fetch_registry_url_reads_remote_box_toml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = b'registry_url = "http://spark.local:5000"\n'

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            assert argv[:3] == ["ssh", "box", "cat"]
            assert argv[3] == "/etc/sparks/box.toml"
            return subprocess.CompletedProcess(argv, 0, stdout=toml, stderr=b"")

        monkeypatch.setattr("sparks.client.subprocess.run", fake_run)
        assert client.fetch_registry_url("box") == "http://spark.local:5000"
