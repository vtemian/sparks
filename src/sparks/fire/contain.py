from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import docker.types

from sparks import dock
from sparks.fire import process

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

    from docker.models.containers import Container

LOG = logging.getLogger("sparks")


def container_environment() -> dict[str, str]:
    env: dict[str, str] = {
        "PYTHONUNBUFFERED": "1",
        "SPARKS_DATA": "/data",
    }
    for name in ("SPARKS_RUN_ID", "SPARKS_PROMETHEUS_URL"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def run_kwargs(
    *,
    name: str,
    image: str,
    command: Sequence[str],
    user: str,
    shared_dir: str | Path,
    data_dir: str | Path,
    workdir: str | Path,
    gpus: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "image": image,
        "command": list(command),
        "name": name,
        "user": user,
        "working_dir": str(workdir),
        "environment": environment,
        "volumes": {
            str(shared_dir): {"bind": str(shared_dir), "mode": "rw"},
            str(data_dir): {"bind": "/data", "mode": "ro"},
        },
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "init": True,
        "detach": True,
        "auto_remove": False,
    }
    if gpus:
        kwargs["device_requests"] = [
            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
        ]
    return kwargs


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sparks.fire.contain",
        description="Create and wait on one training container.",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--cidfile", type=Path, required=True)
    parser.add_argument("--gpus", default="")
    parser.add_argument("--user", required=True)
    parser.add_argument("--shared-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("image")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(list(argv))


def stream(container: Container) -> None:
    chunks = container.logs(stream=True, follow=True, stdout=True, stderr=True)
    out = sys.stdout.buffer
    for chunk in chunks:
        out.write(chunk)
        out.flush()


def remove(container: Container | None, name: str) -> None:
    handle = container
    if handle is None:
        # `containers.run` is create() then start(): a failed start leaves a
        # container we hold no handle to, and its name stays taken.
        LOG.debug("sparks: no handle for %s; asking the daemon by name", name)
        # Built inside the guard: we run in a finally, and a dead daemon must
        # not replace the real error with a connection one.
        with contextlib.suppress(Exception):
            handle = dock.client().containers.get(name)
    if handle is not None:
        LOG.debug("sparks: removing container %s", name)
        with contextlib.suppress(Exception):
            handle.remove(force=True, v=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    container: Container | None = None
    aborted = False

    def stop_on_signal(_signum: int, _frame: FrameType | None) -> None:
        nonlocal aborted
        aborted = True
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop(timeout=int(process.GRACE_SECONDS))

    previous = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        LOG.debug("sparks: starting container %s from %s", args.name, args.image)
        # Bind before the id is examined: one we refuse still holds the GPU.
        container = dock.client().containers.run(
            **run_kwargs(
                name=args.name,
                image=args.image,
                command=args.command,
                user=args.user,
                shared_dir=args.shared_dir,
                data_dir=args.data_dir,
                workdir=args.workdir,
                gpus=args.gpus,
                environment=container_environment(),
            )
        )
        # docker-py types the id optional; one we cannot name we cannot stop.
        if container.id is None:
            raise RuntimeError("docker created a container with no id")
        args.cidfile.write_text(f"{container.id}\n", encoding="utf-8")
        LOG.debug("sparks: container %s is running as %s", args.name, container.id[:12])

        # A signal during containers.run() had no handle to stop; catch up.
        if aborted:
            LOG.debug(
                "sparks: abort arrived while %s was starting; stopping it now",
                args.name,
            )
            with contextlib.suppress(Exception):
                container.stop(timeout=int(process.GRACE_SECONDS))
            container.wait()
            return 1

        stream(container)
        status = int(container.wait()["StatusCode"])
        LOG.debug("sparks: container %s exited %d", args.name, status)
        # A run that was stopped failed, whatever the container made of it.
        return 1 if aborted else status
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        remove(container, args.name)


if __name__ == "__main__":
    sys.exit(main())
