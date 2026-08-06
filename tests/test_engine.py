"""What the engine actually asks Docker to do.

The command it builds is a security boundary, not a formality: the flags here
are the difference between a colleague running a training job and a colleague
being root on the box. They are asserted individually and on purpose.
"""

from pathlib import Path

import pytest

from sparks import engine, runner, spool


@pytest.fixture
def docker(tmp_path: Path) -> engine.Docker:
    return engine.Docker(
        shared_dir=Path("/srv/spark"), url="http://host.docker.internal:9090"
    )


IMAGE = "spark.local:5000/demo:1"


def an_entry(tmp_path: Path, command: list[str] | None = None) -> spool.Entry:
    entry = spool.submit(
        tmp_path / "queue",
        name="e0",
        user="vlad",
        command=command or ["python", "train.py"],
        image=IMAGE,
    )
    entry.data_dir.mkdir(parents=True, exist_ok=True)
    return entry


class TestTheContainerCommand:
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
        """A job that could name a volume could mount / and be root. The only
        mount is the one the queue picked."""
        entry = an_entry(tmp_path, ["--volume", "/:/host", "sh"])
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        # Only the flags docker itself reads, which is everything before the
        # image name. What comes after is the container's argv, and the test
        # below proves the job's tokens land there.
        flags = argv[: argv.index("sha256:abc")]
        volumes = [flags[i + 1] for i, a in enumerate(flags) if a == "--volume"]
        assert volumes == [
            "/srv/spark:/srv/spark",
            f"{entry.data_dir}:/data:ro",
        ]

    def test_job_data_is_mounted_read_only_at_slash_data(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        entry = an_entry(tmp_path)
        argv = docker.container_argv(
            entry, "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert f"{entry.data_dir}:/data:ro" in argv
        assert ("--env", "SPARKS_DATA=/data") in list(zip(argv, argv[1:]))

    def test_the_command_is_passed_after_the_image_so_flags_are_inert(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """Everything a job supplies lands after the image name, where docker
        treats it as the container's argv rather than as its own options."""
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
        for forbidden in ("--privileged", "--pid", "--cap-add", "--userns"):
            assert forbidden not in flags

    def test_the_gpu_is_attached(self, tmp_path: Path, docker: engine.Docker) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("--gpus") + 1] == "all"

    def test_an_init_is_used_so_an_abort_reaches_pid_one(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """PID 1 has no default signal dispositions. Without an init, a training
        script with no SIGTERM handler ignores the abort completely."""
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert "--init" in argv

    def test_the_host_gateway_alias_is_added(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """Inside a container `localhost` is the container. Without this the
        run's own metrics go nowhere, silently, from the writer's end."""
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        assert argv[argv.index("--add-host") + 1] == "host.docker.internal:host-gateway"

    def test_the_run_id_is_forwarded_rather_than_reinvented(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        argv = docker.container_argv(
            an_entry(tmp_path), "sha256:abc", tmp_path / "cid", uid=1001, gid=1002
        )
        forwarded = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
        assert "SPARKS_RUN_ID" in forwarded


class TestTheWholeCommand:
    def test_sparks_run_wraps_docker_run(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """The nesting that keeps sparks out of everybody's Dockerfile."""
        argv = docker.argv(
            an_entry(tmp_path),
            "sha256:abc",
            tmp_path / "cid",
            tmp_path / "run_id",
            uid=1001,
            gid=1002,
        )
        assert argv[0] == "sparks"
        assert "run" in argv
        assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["docker", "run"]

    def test_global_flags_come_before_the_subcommand(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """argparse rejects a global flag placed after a subcommand, and the
        rejection surfaces as the job failing with exit 2 and no explanation
        anywhere a person would look. Found by running one."""
        argv = docker.argv(
            an_entry(tmp_path),
            "sha256:abc",
            tmp_path / "cid",
            tmp_path / "run_id",
            uid=1001,
            gid=1002,
        )
        assert argv.index("--url") < argv.index("run")

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
        """The runner runs from sparks' own directory, so the default would
        record sparks' commit as though it were the training code's - a wrong
        answer that looks exactly like a right one."""
        entry = spool.submit(
            tmp_path / "queue",
            name="e0",
            user="vlad",
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
    def test_a_missing_docker_binary_is_a_pull_failure_not_a_crash(
        self, tmp_path: Path, docker: engine.Docker
    ) -> None:
        """A pull failure fails one job. An exception out of here would be
        caught by the runner's blanket handler and fail it far less legibly."""
        docker.docker_bin = "definitely-not-a-real-binary"
        with pytest.raises(runner.PullFailed, match="could not run docker pull"):
            docker.pull(IMAGE, tmp_path / "pull.log")


class TestDroppingPrivilege:
    def test_only_root_can_become_somebody_else(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking for it as a normal user is EPERM, which crashed the runner
        before this was a check rather than an attempt."""
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 501)
        assert docker.credentials(uid=501, gid=20) == engine.Credentials()

    def test_root_becomes_the_submitter(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 0)
        monkeypatch.setattr("sparks.engine.shared_group", lambda _: None)
        docker.extra_groups = [999]
        assert docker.credentials(uid=1001, gid=1002) == engine.Credentials(
            user=1001, group=1002, extra_groups=[999]
        )

    def test_the_shared_group_comes_along(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """setuid hands over the primary group and drops every supplementary
        one, so without this a job cannot write its own run directory. The box
        found this; no test before it could, because they all ran as a user who
        already owned the tree."""
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 0)
        docker = engine.Docker(shared_dir=tmp_path, url="", extra_groups=[999])
        groups = docker.credentials(uid=1001, gid=1002).extra_groups or []
        assert tmp_path.stat().st_gid in groups
        assert 999 in groups

    def test_the_shared_group_is_not_named_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 0)
        gid = tmp_path.stat().st_gid
        docker = engine.Docker(shared_dir=tmp_path, url="", extra_groups=[gid])
        assert docker.credentials(uid=1, gid=1).extra_groups == [gid]

    def test_an_unreadable_shared_tree_is_not_fatal(
        self, docker: engine.Docker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docker.shared_dir is /srv/spark, which does not exist here."""
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 0)
        assert docker.credentials(uid=1, gid=1).user == 1

    def test_a_non_root_runner_warns_when_it_is_the_wrong_account(
        self,
        docker: engine.Docker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Silently recording somebody else's run under your own name is how a
        colleague ends up unable to read their own output."""
        monkeypatch.setattr("sparks.engine.os.geteuid", lambda: 501)
        docker.credentials(uid=1001, gid=1002)
        assert "wrong account" in caplog.text


class TestReadingBackWhatTheChildWrote:
    def test_an_absent_run_id_is_not_yet_rather_than_an_error(
        self, tmp_path: Path
    ) -> None:
        assert engine._first_line(tmp_path / "nope") is None

    def test_the_run_id_is_read_as_soon_as_it_appears(self, tmp_path: Path) -> None:
        target = tmp_path / "run_id"
        target.write_text("run-20260806-1200-vlad-e0\n")
        assert engine._first_line(target) == "run-20260806-1200-vlad-e0"

    def test_an_empty_file_is_not_an_empty_run_id(self, tmp_path: Path) -> None:
        """It is written atomically, so this should not happen - but an empty
        string as a run id would join to nothing and look like a real answer."""
        target = tmp_path / "run_id"
        target.write_text("")
        assert engine._first_line(target) is None
