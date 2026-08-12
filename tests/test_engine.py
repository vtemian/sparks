import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sparks import dock, spool
from sparks.fire import contain, engine
from sparks.fire.runner import PullFailedError


@pytest.fixture
def docker(tmp_path: Path) -> engine.Docker:
    return engine.Docker(
        shared_dir=Path("/srv/spark"), url="http://host.docker.internal:9090"
    )


IMAGE = "spark.local:5000/demo:1"


def an_entry(
    tmp_path: Path,
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    secret_names: list[str] | None = None,
) -> spool.Entry:
    entry = spool.submit(
        tmp_path / "queue",
        name="e0",
        user="rex",
        command=command or ["python", "train.py"],
        image=IMAGE,
        env=env,
        secret_names=secret_names,
    )
    entry.data_dir.mkdir(parents=True, exist_ok=True)
    return entry


class TestTheContainerCommand:
    def test_it_invokes_contain_via_python_minus_m(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[:3] == [sys.executable, "-m", "sparks.fire.contain"]
        assert "--cidfile" in argv

    def test_it_runs_as_the_submitter_not_as_root(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--user" in argv
        assert argv[argv.index("--user") + 1] == "1001:1002"

    def test_the_job_cannot_choose_its_own_mounts(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path, ["--volume", "/:/host", "sh"])
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        flags = argv[: argv.index("sha256:abc")]
        assert "--volume" not in flags
        assert argv[argv.index("--shared-dir") + 1] == "/srv/spark"
        assert argv[argv.index("--data-dir") + 1] == str(entry.data_dir)

    def test_job_data_dir_is_passed_for_the_read_only_mount(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path)
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("--data-dir") + 1] == str(entry.data_dir)

    def test_the_command_is_passed_after_the_image_so_flags_are_inert(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path, ["--privileged", "sh"])
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("sha256:abc") + 1 :] == ["--privileged", "sh"]

    def test_nothing_grants_extra_privilege(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        image_at = argv.index("sha256:abc")
        flags = argv[:image_at]
        for forbidden in ("--privileged", "--pid", "--cap-add", "--userns", "--volume"):
            assert forbidden not in flags

    def test_the_gpu_is_attached(self, tmp_path: Path, docker: engine.Docker) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("--gpus") + 1] == "all"

    def test_gpus_are_omitted_when_not_configured(self, tmp_path: Path) -> None:
        eng = engine.Docker(
            shared_dir=Path("/srv/spark"),
            url="http://host.docker.internal:9090",
            gpus="",
        )
        argv = eng.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--gpus" not in argv

    def test_host_gateway_is_configured_inside_contain_not_in_argv(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "host.docker.internal" not in " ".join(argv)

    def test_flag_like_image_cannot_override_contain_options(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path, ["/", "real/img", "sh"])
        argv = docker.container_argv(
            entry, "--shared-dir", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--" in argv
        assert argv[argv.index("--") + 1] == "--shared-dir"
        args = contain.parse_args(argv[3:])
        assert args.shared_dir == Path("/srv/spark")
        assert args.image == "--shared-dir"
        assert args.command == ["/", "real/img", "sh"]


class TestTheJobsEnvironment:
    def test_a_public_pair_is_spelled_out_in_the_argv(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path, env={"WANDB_MODE": "offline"})
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        flags = argv[: argv.index("--")]
        assert flags[flags.index("--env") + 1] == "WANDB_MODE=offline"

    def test_a_secret_reaches_the_container_as_a_path_and_never_a_value(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        # summary.json keeps this argv verbatim at 0644, forever.
        entry = an_entry(tmp_path, secret_names=["HF_TOKEN"])
        spool.write_env(entry.path, {"HF_TOKEN": "hunter2"})
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("--env-file") + 1] == str(entry.env_file)
        assert "hunter2" not in " ".join(argv)

    def test_a_job_with_no_secret_is_given_no_env_file(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        # Passing one that is not there would be a hard error in contain, and
        # a job that declared nothing has nothing to fail over.
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--env-file" not in argv

    def test_a_declared_secret_is_named_even_when_the_file_is_gone(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        # The alternative is running the job without it, quietly.
        entry = an_entry(tmp_path, secret_names=["HF_TOKEN"])
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--env-file" in argv

    def test_the_job_command_cannot_choose_its_own_environment(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(
            tmp_path, ["--env-file", "/etc/shadow", "sh"], secret_names=["HF_TOKEN"]
        )
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        args = contain.parse_args(argv[3:])
        assert args.env_file == entry.env_file
        assert args.command == ["--env-file", "/etc/shadow", "sh"]

    def test_a_flag_like_image_cannot_choose_the_env_file_either(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path, ["sh"], secret_names=["HF_TOKEN"])
        argv = docker.container_argv(
            entry, "--env-file", tmp_path / "cid", uid=1001, gid=1002
        )
        args = contain.parse_args(argv[3:])
        assert args.env_file == entry.env_file
        assert args.image == "--env-file"


class TestTheWholeCommand:
    def test_job_is_supervised_via_python_minus_m_fire_supervise(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.argv(
            an_entry(tmp_path),
            "sha256:abc",
            tmp_path / "cid",
            tmp_path / "run_id",
            uid=1001,
            gid=1002,
        )
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "sparks.fire.supervise"]
        assert "sparks-run" not in argv
        after = argv[argv.index("--") + 1 :]
        assert after[:3] == [sys.executable, "-m", "sparks.fire.contain"]

    def test_url_comes_before_name(self, tmp_path: Path, docker: engine.Docker) -> None:
        argv = docker.argv(
            an_entry(tmp_path),
            "sha256:abc",
            tmp_path / "cid",
            tmp_path / "run_id",
            uid=1001,
            gid=1002,
        )
        assert argv.index("--url") < argv.index("--name")

    def test_the_container_is_named_after_the_job_once(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path)
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1, gid=1
        )
        assert argv[argv.index("--name") + 1] == f"sparks-{entry.job.job_id}"
        assert "job-job-" not in argv[argv.index("--name") + 1]

    def test_the_projects_commit_is_recorded_not_the_frameworks(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = spool.submit(
            tmp_path / "queue",
            name="e0",
            user="rex",
            command=["true"],
            image=IMAGE,
            git_sha="feedface",
        )
        argv = docker.argv(
            entry, "sha256:abc", tmp_path / "cid", tmp_path / "run_id", 1, 1
        )
        assert argv[argv.index("--git-sha") + 1] == "feedface"

    def test_the_run_id_file_is_requested_so_the_queue_can_join_early(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.argv(
            an_entry(tmp_path),
            "sha256:abc",
            tmp_path / "cid",
            tmp_path / "run_id",
            uid=1001,
            gid=1002,
        )
        assert argv[argv.index("--run-id-file") + 1] == str(tmp_path / "run_id")


class TestPull:
    def test_api_error_is_a_pull_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "pull.log"
        fake = MagicMock()
        fake.api.pull.side_effect = dock.APIError("boom")
        monkeypatch.setattr("sparks.dock.client", lambda: fake)
        eng = engine.Docker(shared_dir=tmp_path, url="http://example")
        with pytest.raises(PullFailedError, match="pull"):
            eng.pull("missing:tag", log)
        assert log.exists()
        fake.api.pull.assert_called_once_with("missing:tag", stream=True, decode=True)


class TestDroppingPrivilege:
    def test_only_root_can_become_somebody_else(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 501)
        assert docker.credentials(uid=501, gid=20) == engine.Credentials()

    def test_root_becomes_the_submitter(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 0)
        monkeypatch.setattr("sparks.fire.engine.shared_group", lambda _: None)
        docker.extra_groups = [999]
        assert docker.credentials(uid=1001, gid=1002) == engine.Credentials(
            user=1001, group=1002, extra_groups=[999]
        )

    def test_the_shared_group_comes_along(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 0)
        docker = engine.Docker(shared_dir=tmp_path, url="", extra_groups=[999])
        groups = docker.credentials(uid=1001, gid=1002).extra_groups or []
        assert tmp_path.stat().st_gid in groups
        assert 999 in groups

    def test_the_shared_group_is_not_named_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 0)
        gid = tmp_path.stat().st_gid
        docker = engine.Docker(shared_dir=tmp_path, url="", extra_groups=[gid])
        assert docker.credentials(uid=1, gid=1).extra_groups == [gid]

    def test_an_unreadable_shared_tree_is_not_fatal(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 0)
        assert docker.credentials(uid=1, gid=1).user == 1

    def test_a_non_root_runner_warns_when_it_is_the_wrong_account(
        self,
        docker: engine.Docker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr("sparks.fire.engine.os.geteuid", lambda: 501)
        docker.credentials(uid=1001, gid=1002)
        assert "wrong account" in caplog.text


class TestReadingBackWhatTheChildWrote:
    def test_an_absent_run_id_is_not_yet_rather_than_an_error(
        self, tmp_path: Path
    ) -> None:
        assert engine.first_line(tmp_path / "nope") is None

    def test_the_run_id_is_read_as_soon_as_it_appears(self, tmp_path: Path) -> None:
        target = tmp_path / "run_id"
        target.write_text("run-20260806-1200-rex-e0\n")
        assert engine.first_line(target) == "run-20260806-1200-rex-e0"

    def test_an_empty_file_is_not_an_empty_run_id(self, tmp_path: Path) -> None:
        target = tmp_path / "run_id"
        target.write_text("")
        assert engine.first_line(target) is None
