import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sparks.client import local
from sparks.client import remote as client
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
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        docker_reporting(monkeypatch, REGISTRY)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert "ready" in capsys.readouterr().out
        assert not daemon.exists()

    def test_it_writes_the_registry_and_asks_for_a_docker_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        daemon: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr("sparks.client.local.restart_docker", lambda: False)
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
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr("sparks.client.local.restart_docker", lambda: False)
        daemon.parent.mkdir(parents=True)
        daemon.write_text(json.dumps({"insecure-registries": [REGISTRY]}))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        docker_reporting(monkeypatch)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert "restart Docker" in capsys.readouterr().out


class TestRememberingTheBox:
    def test_it_writes_the_host_where_a_later_shell_will_find_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        client.remember_host("vlad@spark.local")

        assert client.stored_host() == "vlad@spark.local"

    def test_it_reports_no_host_when_nothing_was_ever_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        assert client.stored_host() is None

    def test_a_second_setup_replaces_the_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        client.remember_host("vlad@old-box")

        client.remember_host("vlad@new-box")

        assert client.stored_host() == "vlad@new-box"

    def test_a_config_that_is_not_readable_is_refused_rather_than_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silently falling back to "no host" would print "set SPARKS_HOST" at
        # somebody who set it, in a file, which is the wrong thing to debug.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        client.config_path().parent.mkdir(parents=True, exist_ok=True)
        client.config_path().write_text("host = ")

        with pytest.raises(ClientError, match="not readable"):
            client.stored_host()


class TestFinishingTheJob:
    def test_setup_remembers_the_box_it_was_given(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        docker_reporting(monkeypatch, REGISTRY)

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert client.stored_host() == "vlad@spark.local"
        assert "ready" in capsys.readouterr().out

    def test_a_box_it_cannot_reach_is_not_remembered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Storing it first would leave every later command pointed at a box
        # that never answered.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        def refuse(host: str) -> str:
            raise ClientError("nope")

        monkeypatch.setattr("sparks.client.local.fetch_registry_url", refuse)

        with pytest.raises(ClientError):
            local.trust_box_registry("vlad@spark.local")

        assert client.stored_host() is None

    def test_it_says_ready_once_docker_has_taken_the_registry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        daemon: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        monkeypatch.setattr("sparks.client.local.restart_docker", lambda: True)
        seen = iter([set(), {REGISTRY}])
        monkeypatch.setattr(
            "sparks.client.local.trusted_registries", lambda: next(seen)
        )

        assert local.trust_box_registry("vlad@spark.local") == 0

        assert "ready" in capsys.readouterr().out

    def test_a_docker_it_cannot_restart_asks_rather_than_claiming_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        daemon: Path,
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(
            "sparks.client.local.fetch_registry_url", lambda host: f"http://{REGISTRY}"
        )
        monkeypatch.setattr("sparks.client.local.daemon_json_path", lambda: daemon)
        monkeypatch.setattr("sparks.client.local.restart_docker", lambda: False)
        monkeypatch.setattr("sparks.client.local.trusted_registries", set)

        assert local.trust_box_registry("vlad@spark.local") == 0

        printed = capsys.readouterr().out
        assert "ready" not in printed
        assert "restart" in printed
