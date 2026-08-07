"""Create and wait on one training container via the Docker SDK.

Invoked as the child of `python -m sparks.fire.supervise -- …` so energy
sampling and cancel semantics stay outside the project's image.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import docker.types

from sparks import dock
from sparks.fire import process

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

    from docker.models.containers import Container


@dataclass
class _Abort:
    """Set by the signal handler; read at the two checkpoints in main()."""

    requested: bool = False
    container: Container | None = None


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


def _request_abort(signum: int, abort: _Abort | None = None) -> None:
    """Test hook: invoke the installed handler as if a signal arrived.

    With no handler installed, fall back to marking the state passed in.
    """
    handler = _signal_handlers.get(signum)
    if handler is not None and callable(handler):
        handler(signum, None)
    elif abort is not None:
        abort.requested = True


_previous_signal_handlers: dict[int, signal.Handlers] = {}
_signal_handlers: dict[int, Callable[[int, FrameType | None], None]] = {}


def _install_signal_handlers(abort: _Abort) -> None:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        abort.requested = True
        container = abort.container
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop(timeout=int(process.GRACE_SECONDS))

    for signum in (signal.SIGTERM, signal.SIGINT):
        _signal_handlers[signum] = handler
        _previous_signal_handlers[signum] = cast(
            "signal.Handlers", signal.signal(signum, handler)
        )


def _restore_signal_handlers() -> None:
    for signum, previous in _previous_signal_handlers.items():
        signal.signal(signum, previous)
    _previous_signal_handlers.clear()
    _signal_handlers.clear()


def _create(client: docker.DockerClient, args: argparse.Namespace) -> Container:
    container: Container = client.containers.run(
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
    if container.id is None:
        msg = "docker created a container with no id"
        raise RuntimeError(msg)
    return container


def _stop_and_wait(container: Container) -> int:
    with contextlib.suppress(Exception):
        container.stop(timeout=int(process.GRACE_SECONDS))
    container.wait()
    return 1


def _stream(container: Container) -> None:
    for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def _cleanup(container: Container | None) -> None:
    _restore_signal_handlers()
    if container is not None:
        with contextlib.suppress(Exception):
            container.remove(force=True, v=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    client = dock.client()
    abort = _Abort()
    container: Container | None = None

    _install_signal_handlers(abort)
    try:
        container = _create(client, args)
        abort.container = container
        args.cidfile.write_text(f"{container.id}\n", encoding="utf-8")

        # SIGTERM during containers.run() sets the flag but cannot stop yet:
        # the handle did not exist. Catch up before we stream logs.
        if abort.requested:
            return _stop_and_wait(container)

        _stream(container)
        status = int(container.wait()["StatusCode"])
        return 1 if abort.requested else status
    finally:
        _cleanup(container)


if __name__ == "__main__":
    sys.exit(main())
