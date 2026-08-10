import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from sparks.client import local, remote

LOG = logging.getLogger("sparks")

Command = Callable[[argparse.Namespace, list[str]], int]


def submit(args: argparse.Namespace, _argv: list[str]) -> int:
    print(
        remote.submit_remote(
            args.host,
            name=args.name,
            command=args.command,
            context=args.context,
            data=args.data,
            image=args.image,
        )
    )
    return 0


def setup(args: argparse.Namespace, _argv: list[str]) -> int:
    return local.trust_box_registry(args.host)


def queue(args: argparse.Namespace, _argv: list[str]) -> int:
    server = ["queue"]
    if args.all:
        server.append("--all")

    return remote.run(args.host, server)


def cancel(args: argparse.Namespace, _argv: list[str]) -> int:
    return remote.run(args.host, ["cancel", args.job])


def abort(args: argparse.Namespace, _argv: list[str]) -> int:
    return remote.run(args.host, ["abort", args.job])


def retry(args: argparse.Namespace, _argv: list[str]) -> int:
    return remote.run(args.host, ["retry", args.job])


def remove(args: argparse.Namespace, _argv: list[str]) -> int:
    return remote.run(args.host, ["remove", args.job])


type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparks", description="sparks: submit training runs to the box queue."
    )
    host = argparse.ArgumentParser(add_help=False)
    host.add_argument(
        "--host",
        default=None,
        help=f"the box to talk to over ssh. Defaults to ${remote.HOST_ENV}",
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    add_submit(subparsers, host)
    add_queue(subparsers, host)
    setup_parser = subparsers.add_parser(
        "setup",
        parents=[host],
        help="let this machine's Docker push to the box registry",
    )
    setup_parser.set_defaults(func=setup)
    for verb, helping, func in (
        ("cancel", "drop a job that has not started yet", cancel),
        ("abort", "stop a job, whether it has started or not", abort),
        (
            "retry",
            "submit the same job again, reusing the code already there",
            retry,
        ),
        ("remove", "delete a finished job and the code it kept", remove),
    ):
        verb_parser = subparsers.add_parser(verb, parents=[host], help=helping)
        verb_parser.add_argument(
            "job", help="a job id, a unique part of one, or its name"
        )
        verb_parser.set_defaults(func=func)
    return parser


def add_submit(subparsers: Subparsers, host: argparse.ArgumentParser) -> None:
    submit_parser = subparsers.add_parser(
        "submit",
        parents=[host],
        help="build, push, upload --data, and queue a job on the box",
    )
    submit_parser.add_argument("--name", default="job")
    submit_parser.add_argument(
        "--data",
        type=Path,
        required=True,
        help="folder mounted at /data in the job",
    )
    submit_parser.add_argument(
        "--context",
        type=Path,
        default=Path.cwd(),
        help="Docker build context (must contain a Dockerfile). "
        "Defaults to the current directory",
    )
    submit_parser.add_argument(
        "--image",
        help="skip build/push; use this registry tag",
    )
    submit_parser.add_argument("command", nargs="+", help="after a -- separator")
    submit_parser.set_defaults(func=submit)


def add_queue(subparsers: Subparsers, host: argparse.ArgumentParser) -> None:
    queue_parser = subparsers.add_parser(
        "queue",
        parents=[host],
        help="what is running and what is waiting",
    )
    queue_parser.add_argument(
        "--all",
        action="store_true",
        help="include jobs that finished long enough ago to have aged out",
    )
    queue_parser.set_defaults(func=queue)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)

    if not remote.is_configured(args.host):
        print(
            f"sparks: set {remote.HOST_ENV} or pass --host; "
            f"the client always talks to the box",
            file=sys.stderr,
        )
        return os.EX_CONFIG

    host = remote.host_from(args.host)
    assert host is not None  # noqa: S101 -- narrowing; the None case returned above
    args.host = host

    command: Command = args.func
    try:
        return command(args, given)
    except remote.ClientError as exc:
        print(f"sparks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
