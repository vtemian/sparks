import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from sparks import spool
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
            env=args.env,
            secrets=args.secret,
        )
    )
    return 0


def setup(args: argparse.Namespace, _argv: list[str]) -> int:
    for line in local.install_skills():
        print(f"sparks: {line}")
    host = args.host or remote.host_from(None) or local.ask_box()
    if host is None:
        print(
            "sparks: no box yet. Run `sparks setup you@your-box` once, "
            f"or pass --host, or set {remote.HOST_ENV}",
            file=sys.stderr,
        )
        return os.EX_CONFIG

    return local.trust_box_registry(host)


def wanted_box(args: argparse.Namespace) -> str | None:
    # setup is the one verb that runs before there is anything to remember, so
    # it takes the box as an argument and everything else reads it back.
    box: str | None = getattr(args, "box", None)
    host: str | None = args.host
    return box or host


def queue(args: argparse.Namespace, _argv: list[str]) -> int:
    server = ["queue"]
    if args.all:
        server.append("--all")
    if args.json:
        server.append("--json")

    return remote.run(args.host, server)


def logs(args: argparse.Namespace, _argv: list[str]) -> int:
    # No default of our own: the box decides how much tail is sensible, and two
    # copies of that number would drift.
    server = ["logs", args.job]
    if args.tail is not None:
        server += ["--tail", str(args.tail)]
    if args.all:
        server.append("--all")

    return remote.run(args.host, server)


def status(args: argparse.Namespace, _argv: list[str]) -> int:
    server = ["status", args.job]
    if args.json:
        server.append("--json")

    return remote.run(args.host, server)


def wait(args: argparse.Namespace, _argv: list[str]) -> int:
    try:
        state = remote.wait(args.host, args.job, args.interval, args.timeout)
    except remote.TimedOutError as exc:
        # Distinct from a job that failed: the caller is invited to wait again.
        print(f"sparks: {exc}", file=sys.stderr)
        return os.EX_TEMPFAIL
    except KeyboardInterrupt:
        print(f"sparks: stopped watching; {args.job} runs on", file=sys.stderr)
        return 130

    print(state)
    return 0 if state == spool.FINISHED else 1


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
    add_logs(subparsers, host)
    add_status(subparsers, host)
    add_wait(subparsers, host)
    setup_parser = subparsers.add_parser(
        "setup",
        parents=[host],
        help="install the skills, point this machine at a box, let Docker push there",
    )
    setup_parser.add_argument(
        "box", nargs="?", help="the box, as ssh addresses it: you@your-box"
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
    submit_parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set KEY in the job. Recorded in the job spec, which `sparks "
        "status` shows every account on the box: never a secret. Repeatable",
    )
    submit_parser.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="KEY",
        help="set KEY in the job, taking its value from this shell. The value "
        "goes over ssh stdin into a 0600 file and appears in no command line "
        "and no record; only the name is kept. Repeatable",
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
    queue_parser.add_argument(
        "--json",
        action="store_true",
        help="one object per job, for a script to read",
    )
    queue_parser.set_defaults(func=queue)


def add_logs(subparsers: Subparsers, host: argparse.ArgumentParser) -> None:
    logs_parser = subparsers.add_parser(
        "logs",
        parents=[host],
        help="what the job printed",
    )
    logs_parser.add_argument("job", help="a job id, a unique part of one, or its name")
    logs_parser.add_argument(
        "--tail",
        type=int,
        default=None,
        help="how many lines from the end",
    )
    logs_parser.add_argument(
        "--all",
        action="store_true",
        help="every line, not the last --tail of them",
    )
    logs_parser.set_defaults(func=logs)


def add_status(subparsers: Subparsers, host: argparse.ArgumentParser) -> None:
    status_parser = subparsers.add_parser(
        "status",
        parents=[host],
        help="one job in full: its state, and the record of its run",
    )
    status_parser.add_argument(
        "job", help="a job id, a unique part of one, or its name"
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="the whole record as one object, for a script to read",
    )
    status_parser.set_defaults(func=status)


def add_wait(subparsers: Subparsers, host: argparse.ArgumentParser) -> None:
    wait_parser = subparsers.add_parser(
        "wait",
        parents=[host],
        help="block until the job ends; exit 0 only if it finished",
    )
    wait_parser.add_argument("job", help="a job id, a unique part of one, or its name")
    wait_parser.add_argument(
        "--interval",
        type=float,
        default=remote.WAIT_INTERVAL_SECONDS,
        help="seconds between checks",
    )
    wait_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"give up after this many seconds, exiting {os.EX_TEMPFAIL}. "
        f"Waits for as long as the job takes by default",
    )
    wait_parser.set_defaults(func=wait)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)

    args.host = wanted_box(args)
    # setup is the one verb that can start without a box: it asks, then
    # remembers. Everything else needs one already.
    if args.func is not setup and not remote.is_configured(args.host):
        print(
            "sparks: no box yet. Run `sparks setup you@your-box` once, "
            f"or pass --host, or set {remote.HOST_ENV}",
            file=sys.stderr,
        )
        return os.EX_CONFIG

    if args.func is not setup:
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
