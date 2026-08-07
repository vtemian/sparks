"""Unit tests for the SDK-backed training container driver."""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docker.types import DeviceRequest

from sparks.fire import contain


def _sample_environment() -> dict[str, str]:
    return {
        "SPARKS_RUN_ID": "r1",
        "SPARKS_PROMETHEUS_URL": "http://x",
        "PYTHONUNBUFFERED": "1",
        "SPARKS_DATA": "/data",
    }


class TestRunKwargs:
    def test_gpus_all(self) -> None:
        kwargs = contain.run_kwargs(
            name="sparks-job-1",
            image="img:t",
            command=["python", "train.py"],
            user="1000:1000",
            shared_dir="/shared",
            data_dir="/data/job",
            workdir="/shared",
            gpus="all",
            environment=_sample_environment(),
        )
        assert kwargs["name"] == "sparks-job-1"
        assert kwargs["image"] == "img:t"
        assert kwargs["command"] == ["python", "train.py"]
        assert kwargs["user"] == "1000:1000"
        assert kwargs["working_dir"] == "/shared"
        assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
        assert kwargs["init"] is True
        assert kwargs["detach"] is True
        assert kwargs["auto_remove"] is False
        assert kwargs["environment"] == _sample_environment()
        assert kwargs["volumes"] == {
            "/shared": {"bind": "/shared", "mode": "rw"},
            "/data/job": {"bind": "/data", "mode": "ro"},
        }
        device_requests = kwargs.get("device_requests") or []
        assert any(
            getattr(d, "count", None) == -1 and d.capabilities == [["gpu"]]
            for d in device_requests
        )

    def test_omits_gpus_when_empty(self) -> None:
        kwargs = contain.run_kwargs(
            name="sparks-job-1",
            image="img:t",
            command=["python", "train.py"],
            user="1000:1000",
            shared_dir="/shared",
            data_dir="/data/job",
            workdir="/shared",
            gpus="",
            environment=_sample_environment(),
        )
        assert not kwargs.get("device_requests")

    def test_device_request_shape(self) -> None:
        kwargs = contain.run_kwargs(
            name="n",
            image="i",
            command=["true"],
            user="1:1",
            shared_dir="/s",
            data_dir="/d",
            workdir="/s",
            gpus="all",
            environment={},
        )
        assert kwargs["device_requests"] == [
            DeviceRequest(count=-1, capabilities=[["gpu"]])
        ]


class TestContainerEnvironment:
    def test_forwards_sparks_vars_from_process_environ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPARKS_RUN_ID", "run-abc")
        monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://host.docker.internal:9090")
        env = contain.container_environment()
        assert env["SPARKS_RUN_ID"] == "run-abc"
        assert env["SPARKS_PROMETHEUS_URL"] == "http://host.docker.internal:9090"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["SPARKS_DATA"] == "/data"

    def test_omits_missing_sparks_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
        monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)
        env = contain.container_environment()
        assert "SPARKS_RUN_ID" not in env
        assert "SPARKS_PROMETHEUS_URL" not in env
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["SPARKS_DATA"] == "/data"


class TestMain:
    def test_writes_cidfile_and_removes_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cidfile = tmp_path / "container.id"
        fake_container = MagicMock()
        fake_container.id = "abc123deadbeef"
        fake_container.logs.return_value = [b"hello\n"]
        fake_container.wait.return_value = {"StatusCode": 0}

        fake_client = MagicMock()
        fake_client.containers.run.return_value = fake_container
        monkeypatch.setattr("sparks.fire.contain.dock.client", lambda: fake_client)

        code = contain.main(
            [
                "--name",
                "sparks-job-1",
                "--cidfile",
                str(cidfile),
                "--user",
                "1000:1000",
                "--shared-dir",
                "/shared",
                "--data-dir",
                str(tmp_path / "data"),
                "--workdir",
                "/shared",
                "img:t",
                "python",
                "train.py",
            ]
        )

        assert code == 0
        assert cidfile.read_text(encoding="utf-8") == "abc123deadbeef\n"
        fake_client.containers.run.assert_called_once()
        fake_container.remove.assert_called_once_with(force=True, v=True)

    def test_a_container_with_no_id_is_still_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It exists on the daemon whether or not docker told us its id, and a
        container nothing supervises keeps the GPU."""
        fake_container = MagicMock()
        fake_container.id = None

        fake_client = MagicMock()
        fake_client.containers.run.return_value = fake_container
        monkeypatch.setattr("sparks.fire.contain.dock.client", lambda: fake_client)

        with pytest.raises(RuntimeError, match="no id"):
            contain.main(
                [
                    "--name",
                    "sparks-job-1",
                    "--cidfile",
                    str(tmp_path / "container.id"),
                    "--user",
                    "1000:1000",
                    "--shared-dir",
                    "/shared",
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--workdir",
                    "/shared",
                    "img:t",
                    "python",
                    "train.py",
                ]
            )

        fake_container.remove.assert_called_once_with(force=True, v=True)

    def test_stops_if_aborted_during_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cidfile = tmp_path / "container.id"
        fake_container = MagicMock()
        fake_container.id = "abc123deadbeef"
        fake_container.wait.return_value = {"StatusCode": 137}

        fake_client = MagicMock()

        def run_then_mark_abort(*_a: object, **_k: object) -> MagicMock:
            contain._request_abort(signal.SIGTERM)
            return fake_container

        fake_client.containers.run.side_effect = run_then_mark_abort
        monkeypatch.setattr("sparks.fire.contain.dock.client", lambda: fake_client)

        code = contain.main(
            [
                "--name",
                "sparks-job-1",
                "--cidfile",
                str(cidfile),
                "--user",
                "1:1",
                "--shared-dir",
                "/shared",
                "--data-dir",
                str(tmp_path / "data"),
                "--workdir",
                "/shared",
                "img:t",
                "true",
            ]
        )

        assert code == 1
        fake_container.stop.assert_called_once_with(timeout=30)
        fake_container.logs.assert_not_called()
        fake_container.remove.assert_called_once_with(force=True, v=True)

    def test_command_tokens_do_not_change_volumes(self) -> None:
        kwargs = contain.run_kwargs(
            name="n",
            image="i",
            command=["--volume", "/:/host", "sh"],
            user="1:1",
            shared_dir="/shared",
            data_dir="/data/job",
            workdir="/shared",
            gpus="",
            environment={},
        )
        assert kwargs["volumes"] == {
            "/shared": {"bind": "/shared", "mode": "rw"},
            "/data/job": {"bind": "/data", "mode": "ro"},
        }
        assert kwargs["command"] == ["--volume", "/:/host", "sh"]

    def test_stops_container_on_sigterm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cidfile = tmp_path / "container.id"
        fake_container = MagicMock()
        fake_container.id = "abc123deadbeef"

        def logs_then_abort(*_args: object, **_kwargs: object) -> list[bytes]:
            contain._request_abort(signal.SIGTERM)
            return [b"partial\n"]

        fake_container.logs.side_effect = logs_then_abort
        fake_container.wait.return_value = {"StatusCode": 137}

        fake_client = MagicMock()
        fake_client.containers.run.return_value = fake_container
        monkeypatch.setattr("sparks.fire.contain.dock.client", lambda: fake_client)

        code = contain.main(
            [
                "--name",
                "sparks-job-1",
                "--cidfile",
                str(cidfile),
                "--user",
                "1:1",
                "--shared-dir",
                "/shared",
                "--data-dir",
                str(tmp_path / "data"),
                "--workdir",
                "/shared",
                "img:t",
                "true",
            ]
        )

        assert code == 1
        fake_container.stop.assert_called_once_with(timeout=30)
        fake_container.remove.assert_called_once_with(force=True, v=True)

    def test_remove_runs_even_when_run_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_container = MagicMock()
        fake_container.id = "abc123deadbeef"
        fake_container.logs.side_effect = RuntimeError("boom")

        fake_client = MagicMock()
        fake_client.containers.run.return_value = fake_container
        monkeypatch.setattr("sparks.fire.contain.dock.client", lambda: fake_client)

        with pytest.raises(RuntimeError, match="boom"):
            contain.main(
                [
                    "--name",
                    "n",
                    "--cidfile",
                    str(tmp_path / "cid"),
                    "--user",
                    "1:1",
                    "--shared-dir",
                    "/shared",
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--workdir",
                    "/shared",
                    "img:t",
                    "true",
                ]
            )

        fake_container.remove.assert_called_once_with(force=True, v=True)


class TestParseArgs:
    def test_parses_image_and_command(self) -> None:
        args = contain.parse_args(
            [
                "--name",
                "n",
                "--cidfile",
                "/tmp/cid",
                "--gpus",
                "all",
                "--user",
                "1:2",
                "--shared-dir",
                "/shared",
                "--data-dir",
                "/data/job",
                "--workdir",
                "/shared",
                "img:tag",
                "python",
                "train.py",
                "--epochs",
                "3",
            ]
        )
        assert args.name == "n"
        assert args.cidfile == Path("/tmp/cid")
        assert args.gpus == "all"
        assert args.user == "1:2"
        assert args.shared_dir == Path("/shared")
        assert args.data_dir == Path("/data/job")
        assert args.workdir == Path("/shared")
        assert args.image == "img:tag"
        assert args.command == ["python", "train.py", "--epochs", "3"]

    def test_gpus_defaults_to_empty(self) -> None:
        args = contain.parse_args(
            [
                "--name",
                "n",
                "--cidfile",
                "/tmp/cid",
                "--user",
                "1:2",
                "--shared-dir",
                "/shared",
                "--data-dir",
                "/data/job",
                "--workdir",
                "/shared",
                "img:tag",
                "true",
            ]
        )
        assert args.gpus == ""
