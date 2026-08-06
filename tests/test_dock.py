from unittest.mock import MagicMock, patch

import sparks.dock as dock


def test_client_uses_from_env() -> None:
    fake = MagicMock(name="DockerClient")
    with patch("docker.from_env", return_value=fake) as from_env:
        assert dock.client() is fake
        from_env.assert_called_once_with()
