"""Talk to a Docker daemon without shelling out to the CLI."""

from __future__ import annotations

import logging

import docker
from docker.errors import APIError, DockerException, NotFound

LOG = logging.getLogger("sparks")

__all__ = ["APIError", "DockerException", "NotFound", "client", "remove_quietly"]


def client() -> docker.DockerClient:
    return docker.from_env()


def remove_quietly(
    docker_client: docker.DockerClient,
    container_id: str,
    *,
    timeout: float = 60.0,
) -> None:
    """Best-effort force-remove; never raises to the runner."""
    try:
        container = docker_client.containers.get(container_id)
        container.remove(force=True, v=True)
    except NotFound:
        return
    except (APIError, DockerException, OSError) as e:
        LOG.warning("sparks: docker remove %s failed: %s", container_id[:12], e)
