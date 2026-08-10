import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from urllib3.connectionpool import HTTPConnectionPool

from sparks import dock, spool
from sparks.client import remote as client
from sparks.fire import control

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
            client.tag_for("http://spark.local:5000", "rex", "exp", "abc1234")
            == "spark.local:5000/rex/exp:abc1234"
        )

    def test_tag_for_strips_trailing_slash(self) -> None:
        assert (
            client.tag_for("http://spark.local:5000/", "u", "n", "r")
            == "spark.local:5000/u/n:r"
        )

    def test_tag_for_keeps_the_host_of_a_registry_written_without_a_scheme(
        self,
    ) -> None:
        assert (
            client.tag_for("spark.local:5000", "u", "n", "r")
            == "spark.local:5000/u/n:r"
        )


class TestRegistryNetloc:
    def test_it_takes_the_host_and_port_out_of_a_registry_url(self) -> None:
        assert client.registry_netloc("http://spark.local:5000") == "spark.local:5000"

    def test_it_accepts_a_registry_written_without_a_scheme(self) -> None:
        assert client.registry_netloc("spark.local:5000") == "spark.local:5000"

    def test_it_refuses_a_registry_url_that_names_no_host(self) -> None:
        with pytest.raises(client.ClientError, match="names no host"):
            client.registry_netloc("http://")


class TestSplitTag:
    def test_split_tag_defaults_latest_without_splitting_the_port_colon(self) -> None:
        assert client.split_tag("spark.local:5000/u/n") == (
            "spark.local:5000/u/n",
            "latest",
        )
        assert client.split_tag("spark.local:5000/u/n:abc") == (
            "spark.local:5000/u/n",
            "abc",
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
        entry = _submit(queue, data)
        manifest = entry.path / spool.JOB_FILE
        assert (
            manifest.stat().st_mtime >= (entry.data_dir / "corpus.txt").stat().st_mtime
        )

    def test_git_metadata_is_recorded_when_there_is_a_checkout(
        self, queue: Path, data: Path, project: Path
    ) -> None:
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
        (data / ".git").mkdir()
        (data / ".git" / "enormous.pack").write_bytes(b"x" * 4096)
        dest = tmp_path / "dest"
        client.ship(data, dest)
        assert not (dest / ".git").exists()
        assert (dest / "corpus.txt").is_file()

    def test_dockerignore_is_honoured(self, project: Path, tmp_path: Path) -> None:
        (project / ".dockerignore").write_text("data/\n")
        (project / "data").mkdir()
        (project / "data" / "huge.bin").write_bytes(b"x" * 4096)
        dest = tmp_path / "dest"
        client.ship(project, dest)
        assert not (dest / "data").exists()
        assert (dest / "train.py").is_file()

    def test_the_source_slash_is_not_lost(self, project: Path) -> None:
        argv = client.rsync_argv(project, "/dest")
        assert argv[-2] == f"{project}/"


class TestFindingAJob:
    def test_a_full_id_matches(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        assert control.resolve(queue, entry.job.job_id).job.job_id == entry.job.job_id

    def test_a_unique_fragment_matches(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data, name="distinctive")
        assert control.resolve(queue, "distinctive").job.job_id == entry.job.job_id

    def test_an_ambiguous_name_is_refused_rather_than_guessed(
        self, queue: Path, data: Path
    ) -> None:
        for _ in range(3):
            entry = _submit(queue, data, name="same")
            spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(control.ControlError, match="matches several"):
            control.resolve(queue, "same")

    def test_one_live_job_among_finished_ones_is_not_ambiguous(
        self, queue: Path, data: Path
    ) -> None:
        old = _submit(queue, data)
        spool.set_state(old.path, spool.State(state=spool.FINISHED))
        live = _submit(queue, data)
        assert control.resolve(queue, "e0").job.job_id == live.job.job_id

    def test_nothing_matching_says_so(self, queue: Path, data: Path) -> None:
        _submit(queue, data)
        with pytest.raises(control.ControlError, match="no job matches"):
            control.resolve(queue, "nonsense")

    def test_an_empty_queue_says_that_instead(self, queue: Path) -> None:
        spool.make_queue_dir(queue)
        with pytest.raises(control.ControlError, match="no jobs"):
            control.resolve(queue, "anything")


class TestStopping:
    def test_asking_to_abort_leaves_a_request(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        control.ask(queue, "e0", spool.ABORT)
        assert [r.action for r in spool.requests(entry.path)] == [spool.ABORT]

    def test_a_job_that_already_ended_cannot_be_stopped(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        with pytest.raises(control.ControlError, match="nothing to stop"):
            control.ask(queue, "e0", spool.ABORT)

    def test_removing_a_running_job_is_refused_with_the_verb_that_works(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(control.ControlError, match="sparks abort"):
            control.remove(queue, "e0")

    def test_removing_a_finished_job_takes_the_context_with_it(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        control.remove(queue, "e0")
        assert not entry.path.exists()


class TestRetry:
    def test_a_retry_reuses_the_image_and_keeps_the_command(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data, command=["python", "train.py"])
        spool.set_state(entry.path, spool.State(state=spool.FAILED, exit_code=1))
        again = control.retry(queue, spool.load(entry.path))
        assert again.job.retry_of == entry.job.job_id
        assert again.job.command == ["python", "train.py"]
        assert again.job.image == entry.job.image
        assert again.state.state == spool.QUEUED

    def test_retrying_something_still_going_is_refused(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING))
        with pytest.raises(control.ControlError, match=r"twice at once"):
            control.retry(queue, spool.load(entry.path))

    def test_the_retry_is_owned_by_whoever_retried_it(
        self, queue: Path, data: Path
    ) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        again = control.retry(queue, spool.load(entry.path))
        assert again.owner_uid == os.getuid()

    def test_retrying_someone_elses_job_is_refused(
        self, queue: Path, data: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Corpora make this sharper than cancel: retry would clone their data/.
        entry = _submit(queue, data, user="alice")
        spool.set_state(entry.path, spool.State(state=spool.FINISHED))
        monkeypatch.setattr(
            spool.Entry, "may_be_controlled_by", lambda self, uid: False
        )
        with pytest.raises(control.ControlError, match="belongs to"):
            control.retry(queue, spool.load(entry.path))


class TestListing:
    def test_an_empty_queue_says_so_rather_than_printing_a_bare_header(
        self,
    ) -> None:
        assert control.render([]) == "the queue is empty\n"

    def test_the_columns_line_up(self, queue: Path, data: Path) -> None:
        _submit(queue, data, name="short")
        _submit(queue, data, name="a-much-longer-name")
        lines = control.render(spool.entries(queue)).splitlines()
        assert len(lines) == 3
        # The state column starts at the same offset on both rows, which it
        # only does if the widest job id set the width.
        assert len({line.index("queued") for line in lines[1:]}) == 1

    def test_a_running_job_shows_its_run(self, queue: Path, data: Path) -> None:
        entry = _submit(queue, data)
        spool.set_state(entry.path, spool.State(state=spool.RUNNING, run_id="run-abc"))
        assert "run-abc" in control.render(spool.entries(queue))

    @pytest.mark.parametrize(
        ("seconds", "shown"),
        [(5, "5s"), (90, "1m"), (7200, "2.0h"), (200_000, "2d")],
    )
    def test_ages_are_readable(self, seconds: float, shown: str) -> None:
        assert control.duration(seconds) == shown


class TestReachingTheBox:
    def test_commit_argv_requires_image(self) -> None:
        argv = client.commit_argv(
            "/srv/spark/queue/job-1",
            name="n",
            command=["python", "train.py"],
            sha="deadbeef",
            dirty=False,
            image="spark.local:5000/rex/n:deadbeef",
        )
        assert "--image" in argv
        assert "spark.local:5000/rex/n:deadbeef" in argv

    def test_commit_argv_passes_local_user_for_display(self) -> None:
        argv = client.commit_argv("/q/job-1", "e0", ["true"], "abc", False, IMAGE)
        assert "--user" in argv
        assert argv[argv.index("--user") + 1] == client.local_user()

    def test_the_command_survives_the_separator(self) -> None:
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
        argv = client.ssh_argv("box", ["submit", "--", "sh", "-c", "echo a; echo b"])
        assert argv[:2] == ["ssh", "box"]
        # One argument to ssh, and the dangerous parts are inert inside it.
        assert len(argv) == 3
        assert "'echo a; echo b'" in argv[2]

    def test_a_command_with_no_metacharacters_stays_readable(self) -> None:
        argv = client.ssh_argv("box", ["queue", "--all"])
        assert argv[2] == "fire-ctl queue --all"

    def test_ssh_argv_uses_fire_ctl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPARKS_REMOTE", raising=False)
        argv = client.ssh_argv("box", ["queue", "--all"])
        assert argv[2] == "fire-ctl queue --all"

    def test_ssh_argv_honours_sparks_remote_bin_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPARKS_REMOTE", "fire")
        assert client.ssh_argv("box", ["queue"])[2] == "fire queue"

    def test_the_host_flag_beats_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(client.HOST_ENV, "from-env")
        assert client.host_from("explicit") == "explicit"
        assert client.host_from(None) == "from-env"

    def test_no_host_anywhere_means_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(client.HOST_ENV, raising=False)
        assert client.host_from(None) is None
        assert not client.is_configured(None)

    def test_is_configured_when_host_is_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(client.HOST_ENV, raising=False)
        assert client.is_configured("box")
        monkeypatch.setenv(client.HOST_ENV, "from-env")
        assert client.is_configured(None)


class TestBuildAndPush:
    def test_build_refuses_a_missing_dockerfile(self, tmp_path: Path) -> None:
        with pytest.raises(client.ClientError, match="Dockerfile is missing"):
            client.build(tmp_path, "tag:1")

    def test_build_streams_and_tags(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_client = MagicMock()
        fake_client.api.build.return_value = iter([{"stream": "Step 1\n"}])
        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        client.build(project, "spark.local:5000/u/n:r")
        fake_client.api.build.assert_called_once()
        kwargs = fake_client.api.build.call_args.kwargs
        assert kwargs["path"] == str(project)
        assert kwargs["tag"] == "spark.local:5000/u/n:r"

    def test_build_progress_keeps_off_stdout(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_client = MagicMock()
        fake_client.api.build.return_value = iter([{"stream": "Step 1\n"}])
        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        client.build(project, "spark.local:5000/u/n:r")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Step 1" in captured.err

    def test_push_progress_keeps_off_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_client = MagicMock()
        fake_client.images.push.return_value = iter([{"status": "Pushed"}])
        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        client.push("spark.local:5000/u/n:r")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Pushed" in captured.err

    def test_a_push_that_outlives_the_socket_timeout_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_client = MagicMock()

        def stall(*args: Any, **kwargs: Any) -> None:
            raise dock.ReadTimeoutError(
                HTTPConnectionPool("localhost"), "unix://", "read timed out"
            )

        fake_client.images.push.side_effect = stall
        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)

        with pytest.raises(client.ClientError, match="stopped sending progress"):
            client.push("spark.local:5000/u/n:r")

    def test_push_asks_for_a_client_that_outlasts_a_multi_gigabyte_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[int] = []
        fake_client = MagicMock()
        fake_client.images.push.return_value = iter([{"status": "Pushed"}])

        def record(timeout: int) -> MagicMock:
            asked.append(timeout)
            return fake_client

        monkeypatch.setattr("sparks.dock.client", record)
        client.push("spark.local:5000/u/n:r")

        assert asked == [dock.STREAM_TIMEOUT_SECONDS]
        assert dock.STREAM_TIMEOUT_SECONDS > dock.DEFAULT_TIMEOUT_SECONDS

    def test_push_failure_is_helpful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_client = MagicMock()
        fake_client.images.push.return_value = iter([{"error": "denied"}])
        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        with pytest.raises(client.ClientError, match="insecure-registries"):
            client.push("spark.local:5000/u/n:r")


class TestWhoSubmitted:
    def test_reserve_and_commit_agree_on_who_submitted(
        self, data: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # reserve names the job directory and commit writes the displayed user.
        # If they disagree, one queue row names two different people.
        sent: dict[str, str] = {}

        def fake_capture(host: str, argv: list[str]) -> str:
            sent[argv[0]] = argv[argv.index("--user") + 1]
            return "/q/job-1" if argv[0] == "reserve" else "job-1"

        monkeypatch.setattr(client, "capture", fake_capture)
        monkeypatch.setattr(client, "local_user", lambda: "ana")
        monkeypatch.setattr(client, "provenance", lambda _ctx: ("abc1234", False))
        monkeypatch.setattr(client, "ship_to", lambda *a, **k: None)

        client.submit_remote(
            "rex@your-box",
            name="exp",
            command=["true"],
            data=data,
            context=data,
            image="alpine:3",
        )
        assert sent["reserve"] == sent["commit"] == "rex"

    def test_the_ssh_account_is_who_submitted_not_the_laptop_account(self) -> None:
        assert client.submitting_user("rex@your-box") == "rex"

    def test_a_host_with_no_account_falls_back_to_the_local_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client, "local_user", lambda: "ana")
        assert client.submitting_user("spark.local") == "ana"

    def test_an_ipv6_style_host_is_not_mistaken_for_an_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client, "local_user", lambda: "ana")
        assert client.submitting_user("[fe80::1]") == "ana"


class TestSubmitRemote:
    def test_submit_remote_builds_pushes_rsyncs_data_then_commits(
        self,
        project: Path,
        data: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_client = MagicMock()
        fake_client.api.build.return_value = iter([{"stream": "ok\n"}])
        fake_client.images.push.return_value = iter([{"status": "Pushed"}])
        calls: list[tuple[str, list[str]]] = []

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            calls.append(("run", list(argv)))
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        def fake_capture(host: str, argv: list[str]) -> str:
            calls.append(("ssh", list(argv)))
            if argv[0] == "reserve":
                return "/q/job-1"
            return "job-1"

        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        monkeypatch.setattr("sparks.client.remote.subprocess.run", fake_run)
        monkeypatch.setattr(client, "capture", fake_capture)
        monkeypatch.setattr(client, "local_user", lambda: "rex")
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

        tag = "spark.local:5000/rex/exp:abc1234"
        fake_client.api.build.assert_called_once()
        build_kwargs = fake_client.api.build.call_args.kwargs
        assert build_kwargs["path"] == str(project)
        assert build_kwargs["tag"] == tag
        fake_client.images.push.assert_called_once_with(
            "spark.local:5000/rex/exp", tag="abc1234", stream=True, decode=True
        )

        kinds = [kind for kind, _ in calls]
        assert kinds == ["ssh", "run", "ssh"]
        assert calls[0] == (
            "ssh",
            ["reserve", "--name", "exp", "--user", "rex"],
        )
        assert calls[1][1][0] == "rsync"
        assert calls[1][1][-1] == f"box:/q/job-1/{spool.DATA_DIR}/"
        assert f"{data}/" in calls[1][1]
        assert "context" not in "".join(calls[1][1])
        commit = calls[2][1]
        assert commit[0] == "commit"
        assert "--image" in commit
        assert tag in commit

    def test_submit_remote_fetches_registry_url_when_omitted(
        self,
        project: Path,
        data: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fetched: list[str] = []
        fake_client = MagicMock()
        fake_client.api.build.return_value = iter([{"stream": "ok\n"}])
        fake_client.images.push.return_value = iter([{"status": "Pushed"}])

        def fake_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        def fake_capture(host: str, argv: list[str]) -> str:
            if argv[0] == "reserve":
                return "/q/job-1"
            return "job-1"

        def fake_fetch(host: str) -> str:
            fetched.append(host)
            return "http://spark.local:5000"

        monkeypatch.setattr("sparks.dock.client", lambda **kwargs: fake_client)
        monkeypatch.setattr("sparks.client.remote.subprocess.run", fake_run)
        monkeypatch.setattr(client, "capture", fake_capture)
        monkeypatch.setattr(client, "fetch_registry_url", fake_fetch)
        monkeypatch.setattr(client, "local_user", lambda: "rex")
        monkeypatch.setattr(client, "provenance", lambda _ctx: ("abc1234", False))

        client.submit_remote(
            "box",
            name="exp",
            command=["true"],
            context=project,
            data=data,
        )
        assert fetched == ["box"]

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

        def fake_capture(host: str, argv: list[str]) -> str:
            calls.append(("ssh", list(argv)))
            if argv[0] == "reserve":
                return "/q/job-1"
            return "job-1"

        monkeypatch.setattr("sparks.client.remote.subprocess.run", fake_run)
        monkeypatch.setattr(client, "capture", fake_capture)
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
            argv for kind, argv in calls if kind == "ssh" and argv[0] == "commit"
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

        monkeypatch.setattr("sparks.client.remote.subprocess.run", fake_run)
        assert client.fetch_registry_url("box") == "http://spark.local:5000"
