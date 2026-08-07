from unittest.mock import MagicMock, patch

import pytest
from docker.errors import APIError, DockerException, NotFound

from sparks import dock


def test_client_uses_from_env() -> None:
    fake = MagicMock(name="DockerClient")
    with patch("docker.from_env", return_value=fake) as from_env:
        assert dock.client() is fake
        from_env.assert_called_once_with()


def test_remove_quietly_force_removes() -> None:
    client = MagicMock()
    container = MagicMock()
    client.containers.get.return_value = container
    with patch.object(dock, "client", return_value=client):
        dock.remove_quietly("abc123")
    client.containers.get.assert_called_once_with("abc123")
    container.remove.assert_called_once_with(force=True, v=True)


def test_remove_quietly_swallows_not_found() -> None:
    client = MagicMock()
    client.containers.get.side_effect = NotFound("missing")
    with patch.object(dock, "client", return_value=client):
        dock.remove_quietly("gone")


def test_remove_quietly_swallows_api_error(caplog: pytest.LogCaptureFixture) -> None:
    client = MagicMock()
    client.containers.get.side_effect = APIError("denied")
    with patch.object(dock, "client", return_value=client):
        dock.remove_quietly("abc123")
    assert "docker remove abc123" in caplog.text


def test_remove_quietly_survives_a_daemon_that_has_gone_away() -> None:
    """It is called from a finally that must still mark the job terminal, so a
    dead socket has to be swallowed here rather than escape the cleanup."""
    with patch.object(dock, "client", side_effect=DockerException("no socket")):
        dock.remove_quietly("abc123")
