from __future__ import annotations

import logging

import docker
from docker.errors import APIError, DockerException, NotFound
from urllib3.exceptions import ReadTimeoutError

LOG = logging.getLogger("sparks")

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "STREAM_TIMEOUT_SECONDS",
    "APIError",
    "DockerException",
    "NotFound",
    "ReadTimeoutError",
    "client",
    "remove_quietly",
]

DEFAULT_TIMEOUT_SECONDS = 60

STREAM_TIMEOUT_SECONDS = 3600


def client(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> docker.DockerClient:
    # Per read from the daemon socket, not for the whole transfer: a
    # multi-gigabyte layer moves for minutes without a progress line, which is
    # why anything streaming an image passes STREAM_TIMEOUT_SECONDS instead.
    return docker.from_env(timeout=timeout)


def remove_quietly(container_id: str) -> None:
    try:
        container = client().containers.get(container_id)
        container.remove(force=True, v=True)
    except NotFound:
        return
    except (APIError, DockerException, OSError) as exc:
        LOG.warning("sparks: docker remove %s failed: %s", container_id[:12], exc)
