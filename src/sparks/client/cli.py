"""sparks — the laptop client.

Every user-facing verb talks to the box over ssh. Set SPARKS_HOST (or pass
--host). Queue control SSHes `fire-ctl` on the box, which runs `fire <verb>`
inside the queue container. Job supervision is private
(`python -m sparks.fire.supervise`).
"""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from sparks import box
from sparks.client import remote

LOG = logging.getLogger("sparks")

# sysexits.h EX_CONFIG.
EX_CONFIG = 78

Command = Callable[[argparse.Namespace, list[str]], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks", description=__doc__)
    sub = parser.add_subparsers(dest="command_name", required=True)
    _add_client_commands(sub)
    return parser


def _add_client_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Laptop verbs: always require a host, always ssh to the box."""
    submit = sub.add_parser(
        "submit",
        help="build, push, upload --data, and queue a job on the box",
    )
    submit.add_argument("--name", default="job")
    submit.add_argument(
        "--data",
        type=Path,
        required=True,
        help="folder mounted at /data in the job",
    )
    submit.add_argument(
        "--context",
        type=Path,
        default=Path.cwd(),
        help="Docker build context (must contain a Dockerfile). "
        "Defaults to the current directory",
    )
    submit.add_argument(
        "--image",
        help="skip build/push; use this registry tag",
    )
    _add_host(submit)
    submit.add_argument("command", nargs="+", help="after a -- separator")
    submit.set_defaults(func=cmd_submit)

    listing = sub.add_parser("queue", help="what is running and what is waiting")
    listing.add_argument(
        "--all",
        action="store_true",
        help="include jobs that finished long enough ago to have aged out",
    )
    _add_host(listing)
    listing.set_defaults(func=cmd_queue)

    for verb, helping, func in (
        ("cancel", "drop a job that has not started yet", cmd_cancel),
        ("abort", "stop a job, whether it has started or not", cmd_abort),
        (
            "retry",
            "submit the same job again, reusing the code already there",
            cmd_retry,
        ),
        ("remove", "delete a finished job and the code it kept", cmd_remove),
    ):
        parser = sub.add_parser(verb, help=helping)
        parser.add_argument("job", help="a job id, a unique part of one, or its name")
        _add_host(parser)
        parser.set_defaults(func=func)


def _add_host(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--host",
        default=None,
        help=f"the box to talk to over ssh. Defaults to ${remote.HOST_ENV}",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)
    command: Command = args.func
    return command(args, given)


def _cli_errors(fn: Command) -> Command:
    """Turn the exceptions a command is allowed to raise into exit codes."""

    def wrap(args: argparse.Namespace, argv: list[str]) -> int:
        try:
            return fn(args, argv)
        except remote.ClientError as e:
            print(f"sparks: {e}", file=sys.stderr)
            return 1
        except (box.NotProvisioned, box.Malformed) as e:
            print(f"sparks: {e}", file=sys.stderr)
            return EX_CONFIG

    return wrap


def _require_host(args: argparse.Namespace) -> str:
    host = remote.host_from(args.host)
    if host is None:
        raise remote.ClientError(
            f"set {remote.HOST_ENV} or pass --host; "
            f"the client always talks to the box"
        )
    return host


@_cli_errors
def cmd_submit(args: argparse.Namespace, _argv: list[str]) -> int:
    host = _require_host(args)
    print(
        remote.submit_remote(
            host,
            name=args.name,
            command=args.command,
            context=args.context,
            data=args.data,
            image=args.image,
        )
    )
    return 0


@_cli_errors
def cmd_queue(args: argparse.Namespace, _argv: list[str]) -> int:
    host = _require_host(args)
    server = ["queue"]
    if args.all:
        server.append("--all")
    return remote.remote(host, server)


def _ask_remote(verb: str) -> Command:
    @_cli_errors
    def wrap(args: argparse.Namespace, _argv: list[str]) -> int:
        host = _require_host(args)
        return remote.remote(host, [verb, args.job])

    return wrap


cmd_cancel = _ask_remote("cancel")
cmd_abort = _ask_remote("abort")


@_cli_errors
def cmd_retry(args: argparse.Namespace, _argv: list[str]) -> int:
    host = _require_host(args)
    return remote.remote(host, ["retry", args.job])


@_cli_errors
def cmd_remove(args: argparse.Namespace, _argv: list[str]) -> int:
    host = _require_host(args)
    return remote.remote(host, ["remove", args.job])


if __name__ == "__main__":
    sys.exit(main())
