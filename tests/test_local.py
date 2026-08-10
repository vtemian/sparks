import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sparks.client import local
from sparks.client.remote import ClientError

REGISTRY = "spark.local:5000"


@pytest.fixture
def daemon(tmp_path: Path) -> Path:
    return tmp_path / "docker" / "daemon.json"


def info_with(*insecure: str) -> dict[str, Any]:
    configs = {
        "docker.io": {"Name": "docker.io", "Secure": True},
        **{name: {"Name": name, "Secure": False} for name in insecure},
    }
    return {"RegistryConfig": {"IndexConfigs": configs}}


def docker_reporting(monkeypatch: pytest.MonkeyPatch, *insecure: str) -> None:
    fake_client = MagicMock()
    fake_client.info.return_value = info_with(*insecure)
    monkeypatch.setattr("sparks.dock.client", lambda: fake_client)


class TestTrustingTheBoxRegistry:
    def test_it_writes_a_daemon_file_that_was_never_there(self, daemon: Path) -> None:
        assert local.trust_registry(daemon, REGISTRY) is True

        assert json.loads(daemon.read_text()) == {"insecure-registries": [REGISTRY]}

    def test_it_keeps_every_other_setting_docker_already_had(
        self, daemon: Path
    ) -> None:
        daemon.parent.mkdir(parents=True)
        daemon.write_text(
            json.dumps({"experimental": True, "insecure-registries": ["other:5000"]})
        )

        assert local.trust_registry(daemon, REGISTRY) is True

        assert json.loads(daemon.read_text()) == {
            "experimental": True,
            "insecure-registries": ["other:5000", REGISTRY],
        }

    def test_it_leaves_the_file_alone_when_the_registry_is_already_listed(
        self, daemon: Path
    ) -> None:
        daemon.parent.mkdir(parents=True)
        original = json.dumps({"insecure-registries": [REGISTRY]}, indent=4)
        daemon.write_text(original)

        assert local.trust_registry(daemon, REGISTRY) is False

        assert daemon.read_text() == original

    def test_it_refuses_a_daemon_file_that_is_not_json(self, daemon: Path) -> None:
        daemon.parent.mkdir(parents=True)
        daemon.write_text("{ not json")

        with pytest.raises(ClientError, match="is not readable JSON"):
            local.trust_registry(daemon, REGISTRY)

    def test_it_refuses_a_daemon_file_holding_something_other_than_an_object(
        self, daemon: Path
    ) -> None:
        daemon.parent.mkdir(parents=True)
        daemon.write_text("[]")

        with pytest.raises(ClientError, match="not an object"):
            local.trust_registry(daemon, REGISTRY)

    def test_it_refuses_when_insecure_registries_is_not_a_list(
        self, daemon: Path
    ) -> None:
        daemon.parent.mkdir(parents=True)
        daemon.write_text(json.dumps({"insecure-registries": REGISTRY}))

        with pytest.raises(ClientError, match="is not a list"):
            local.trust_registry(daemon, REGISTRY)


class TestWhatDockerAlreadyTrusts:
    def test_it_reports_the_registries_docker_is_running_without_tls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docker_reporting(monkeypatch, REGISTRY, "other:5000")

        assert local.trusted_registries() == {REGISTRY, "other:5000"}

    def test_it_does_not_report_a_registry_docker_reaches_over_tls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        docker_reporting(monkeypatch)

        assert local.trusted_registries() == set()

    def test_it_says_so_when_docker_is_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse() -> None:
            raise OSError("no such file: /var/run/docker.sock")

        monkeypatch.setattr("sparks.dock.client", refuse)

        with pytest.raises(ClientError, match="cannot ask Docker"):
            local.trusted_registries()


class TestTheSetupCommand:
    def test_it_does_nothing_when_docker_already_trusts_the_box_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        daemon: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        docker_reporting(monkeypatch, REGISTRY)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert "already trusts" in capsys.readouterr().out
        assert not daemon.exists()

    def test_it_writes_the_registry_and_asks_for_a_docker_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        daemon: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        docker_reporting(monkeypatch)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert json.loads(daemon.read_text()) == {"insecure-registries": [REGISTRY]}
        printed = capsys.readouterr().out
        assert REGISTRY in printed
        assert "restart Docker" in printed

    def test_it_still_asks_for_a_restart_when_the_file_was_already_edited(
        self,
        monkeypatch: pytest.MonkeyPatch,
        daemon: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        daemon.parent.mkdir(parents=True)
        daemon.write_text(json.dumps({"insecure-registries": [REGISTRY]}))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        docker_reporting(monkeypatch)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert "restart Docker" in capsys.readouterr().out
