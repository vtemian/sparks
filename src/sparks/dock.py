from __future__ import annotations

import logging

import docker
from docker.errors import APIError, DockerException, NotFound

LOG = logging.getLogger("sparks")

__all__ = ["APIError", "DockerException", "NotFound", "client", "remove_quietly"]


def client() -> docker.DockerClient:
    return docker.from_env()


def remove_quietly(container_id: str) -> None:
    try:
        container = client().containers.get(container_id)
        container.remove(force=True, v=True)
    except NotFound:
        return
    except (APIError, DockerException, OSError) as exc:
        LOG.warning("sparks: docker remove %s failed: %s", container_id[:12], exc)
