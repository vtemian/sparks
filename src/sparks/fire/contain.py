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


def _request_abort(signum: int) -> None:
    """Test hook: invoke the installed handler as if a signal arrived. Does
    nothing when no handler is installed, which is the same as a signal
    arriving before main() reached _install_signal_handlers."""
    handler = _signal_handlers.get(signum)
    if handler is None:
        return
    if not callable(handler):
        return
    handler(signum, None)


_previous_signal_handlers: dict[int, signal.Handlers] = {}
_signal_handlers: dict[int, Callable[[int, FrameType | None], None]] = {}


def _install_signal_handlers(abort: _Abort) -> None:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        abort.requested = True
        container = abort.container
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop(timeout=int(process.GRACE_SECONDS))

    assert not _signal_handlers, "handlers already installed"  # noqa: S101 -- re-entrant main()
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
    """Start the container and hand it back unexamined.

    Rejecting it is the caller's job, and must happen only once the handle is
    published for cleanup: a container the daemon created but we refused still
    exists, and one nothing supervises keeps the GPU.
    """
    environment = container_environment()
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
            environment=environment,
        )
    )
    return container


def _cid(container: Container) -> str:
    """The id docker-py types as optional but sets for anything it started."""
    identifier = container.id
    if identifier is None:
        msg = "docker created a container with no id"
        raise RuntimeError(msg)
    return identifier


def _aborted(container: Container) -> int:
    """Stop the container, wait for it, and report what an aborted run exits
    with. Always 1: the run was stopped, whatever the container made of it."""
    with contextlib.suppress(Exception):
        container.stop(timeout=int(process.GRACE_SECONDS))
    container.wait()
    return 1


def _stream(container: Container) -> None:
    """Tee the container's combined stdout/stderr to this process's stdout."""
    chunks = container.logs(stream=True, follow=True, stdout=True, stderr=True)
    out = sys.stdout.buffer
    for chunk in chunks:
        out.write(chunk)
        out.flush()


def _cleanup(container: Container | None, name: str) -> None:
    """Restore the box's own signal handlers, then force-remove the job
    container by handle or, failing that, by name.

    The by-name path is not belt and braces: docker-py's `containers.run` is
    `create()` then `start()` with no cleanup between them, so a start that
    fails (no nvidia runtime for `--gpus all`, a uid the box does not have)
    leaves a created container we never got a handle to. The cidfile is never
    written in that case, so `engine.Process.finish` cannot reach it either and
    the name stays taken until a human runs `docker rm`.
    """
    _restore_signal_handlers()
    handle = container
    if handle is None:
        # The client is built inside the guard on purpose: this runs in a
        # finally, and a daemon that has gone away must not replace the real
        # error with a connection one.
        with contextlib.suppress(Exception):
            handle = dock.client().containers.get(name)
    if handle is not None:
        with contextlib.suppress(Exception):
            handle.remove(force=True, v=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    abort = _Abort()
    container: Container | None = None

    _install_signal_handlers(abort)
    try:
        container = _create(dock.client(), args)
        abort.container = container
        args.cidfile.write_text(f"{_cid(container)}\n", encoding="utf-8")

        # SIGTERM during containers.run() sets the flag but cannot stop yet:
        # the handle did not exist. Catch up before we stream logs.
        if abort.requested:
            return _aborted(container)

        _stream(container)
        status = int(container.wait()["StatusCode"])
        return 1 if abort.requested else status
    finally:
        _cleanup(container, args.name)


if __name__ == "__main__":
    sys.exit(main())
