"""Talk to a Docker daemon without shelling out to the CLI."""

from __future__ import annotations

import logging

import docker
from docker.errors import APIError, DockerException, NotFound

LOG = logging.getLogger("sparks")

__all__ = ["APIError", "DockerException", "NotFound", "client", "remove_quietly"]


def client() -> docker.DockerClient:
    return docker.from_env()


def remove_quietly(container_id: str) -> None:
    """Best-effort force-remove; never raises to the runner. It builds its own
    client inside the `try` because `client()` raises when the daemon has gone
    away, and every caller here is a cleanup path."""
    try:
        container = client().containers.get(container_id)
        container.remove(force=True, v=True)
    except NotFound:
        return
    except (APIError, DockerException, OSError) as exc:
        LOG.warning("sparks: docker remove %s failed: %s", container_id[:12], exc)
