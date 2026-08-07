"""Create and wait on one training container via the Docker SDK.

Invoked as the child of `python -m sparks.fire.supervise -- …` so energy
sampling and cancel semantics stay outside the project's image.
"""

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
    """Environment passed into the training container.

    SPARKS_RUN_ID and SPARKS_PROMETHEUS_URL are forwarded from this process the
    same way `docker run --env NAME` without a value did when supervise set them.
    """
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
    """Pure mapping from job fields to `containers.run` kwargs."""
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
    parser = argparse.ArgumentParser(prog="sparks.fire.contain", description=__doc__)
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
    """Tee the container's combined stdout/stderr to this process's stdout."""
    chunks = container.logs(stream=True, follow=True, stdout=True, stderr=True)
    out = sys.stdout.buffer
    for chunk in chunks:
        out.write(chunk)
        out.flush()


def remove(container: Container | None, name: str) -> None:
    """Force-remove the job container by handle or, failing that, by name.

    The by-name path is not belt and braces: docker-py's `containers.run` is
    `create()` then `start()` with no cleanup between them, so a start that
    fails (no nvidia runtime for `--gpus all`, a uid the box does not have)
    leaves a created container we never got a handle to. The cidfile is never
    written in that case, so `engine.Process.finish` cannot reach it either and
    the name stays taken until a human runs `docker rm`.
    """
    handle = container
    if handle is None:
        LOG.debug("sparks: no handle for %s; asking the daemon by name", name)
        # The client is built inside the guard on purpose: this runs in a
        # finally, and a daemon that has gone away must not replace the real
        # error with a connection one.
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
        """Shaped for `signal.signal`. Both the flag and the handle it stops
        are main()'s locals, so a run cannot be aborted through anything but
        the handler main() itself installed.

        Nothing is logged from here: the handler runs between two bytecodes of
        whatever the main thread was doing, and that thread may be holding the
        logging lock. The narration lives at the checkpoints instead.
        """
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
        # Binding this local is what publishes the handle to the handler and to
        # the finally, and it happens before the id below is examined on
        # purpose: a container the daemon created but we then refuse still
        # exists, and one nothing supervises keeps the GPU.
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
        # docker-py types the id as optional, but sets it for anything it
        # started. A container we cannot name is one we cannot supervise.
        if container.id is None:
            raise RuntimeError("docker created a container with no id")
        args.cidfile.write_text(f"{container.id}\n", encoding="utf-8")
        LOG.debug("sparks: container %s is running as %s", args.name, container.id[:12])

        # SIGTERM during containers.run() sets the flag but cannot stop yet:
        # the handle did not exist. Catch up before we stream logs.
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
