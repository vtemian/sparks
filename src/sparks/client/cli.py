"""sparks — the laptop client.

Submit work to the box queue and then close the laptop. The queue daemon and
the training wrapper are separate entry points (`sparks-runner`, `sparks-run`).

Laptops set SPARKS_HOST (or pass --host). When host is unset, verbs that can
run against a local queue directory do so — that path is for the box (after
ssh) and for unit tests with --shared-dir.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from sparks import box, spool
from sparks.client import remote

LOG = logging.getLogger("sparks")

# sysexits.h EX_CONFIG.
EX_CONFIG = 78

Command = Callable[[argparse.Namespace, list[str]], int]

# User-facing verbs that talk to the queue. Without a host they need a local
# queue (contract or --shared-dir); otherwise the error points at SPARKS_HOST.
_QUEUE_VERBS = frozenset({"queue", "cancel", "abort", "retry", "remove"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks", description=__doc__)
    sub = parser.add_subparsers(dest="command_name", required=True)
    _add_queue_commands(sub)
    return parser


def _add_queue_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """The queue: submit work and then close the laptop.

    Every user-facing verb takes `--host`. When it is set the whole command is
    forwarded over ssh rather than each verb having a remote implementation.
    """
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

    # Plumbing. `submit --host` is several steps against the box and these are
    # two of them; they are not meant to be typed. See remote.submit_remote.
    reserve = sub.add_parser("reserve", help=argparse.SUPPRESS)
    reserve.add_argument("--name", default="job")
    reserve.add_argument("--shared-dir", type=Path, default=None)
    reserve.set_defaults(func=cmd_reserve)

    commit = sub.add_parser("commit", help=argparse.SUPPRESS)
    commit.add_argument("path", type=Path)
    commit.add_argument("--name", default="job")
    commit.add_argument("--user", default=None)
    commit.add_argument("--git-sha", default="unknown")
    commit.add_argument("--git-dirty", action="store_true")
    commit.add_argument("--image", required=True)
    commit.add_argument("command", nargs="+")
    commit.set_defaults(func=cmd_commit)

    # Hidden: inspect the box contract (registry_url and friends).
    contract = sub.add_parser("contract", help=argparse.SUPPRESS)
    contract.set_defaults(func=cmd_contract)


def _add_host(parser: argparse.ArgumentParser) -> None:
    """Which queue, and on which machine.

    Laptops set SPARKS_HOST. On the box (and in unit tests with --shared-dir)
    host may be unset and the verb runs against the local queue directory.
    """
    parser.add_argument(
        "--host",
        default=None,
        help=f"the box to talk to over ssh. Defaults to ${remote.HOST_ENV}",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="the shared tree whose queue to use. Defaults to the provisioned one",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)
    command: Command = args.func
    return command(args, given)


def _cli_errors(fn: Command) -> Command:
    """Turn the exceptions a command is allowed to raise into exit codes.

    Lives on the command, not in `main`: each verb owns its failure mode, and
    `main` only parses and dispatches.
    """

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


@_cli_errors
def cmd_submit(args: argparse.Namespace, argv: list[str]) -> int:
    # Host set → remote. Unset → local queue (box / unit tests). Laptops set
    # SPARKS_HOST so they never take the local path by accident.
    host = remote.host_from(args.host)
    if host:
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
    registry_url = None
    if args.image is None:
        contract = box.load()
        registry_url = contract.registry_url if contract else None
    entry = remote.submit(
        _queue_dir(args),
        name=args.name,
        command=args.command,
        data=args.data,
        context=None if args.image else args.context,
        image=args.image,
        registry_url=registry_url,
    )
    print(entry.job.job_id)
    return 0


def _remote_or_local(local: Command) -> Command:
    """Queue verbs: ssh when host is set, else the local queue directory."""

    @_cli_errors
    def wrap(args: argparse.Namespace, argv: list[str]) -> int:
        host = remote.host_from(args.host)
        if host:
            # Forwarding the argv rather than reconstructing it keeps this from
            # drifting as flags are added.
            return remote.remote(host, _without_host(argv))
        if (
            args.command_name in _QUEUE_VERBS
            and getattr(args, "shared_dir", None) is None
            and box.load() is None
        ):
            raise remote.ClientError(
                f"set {remote.HOST_ENV} or pass --host; "
                f"the client always talks to the box"
            )
        return local(args, argv)

    return wrap


@_remote_or_local
def cmd_queue(args: argparse.Namespace, _argv: list[str]) -> int:
    queue_dir = _queue_dir(args)
    entries = spool.entries(queue_dir) if args.all else spool.publishable(queue_dir)
    print(remote.render(entries), end="")
    return 0


def _ask(verb: str) -> Command:
    @_remote_or_local
    def wrap(args: argparse.Namespace, _argv: list[str]) -> int:
        entry = remote.ask(_queue_dir(args), args.job, verb)
        print(f"asked the runner to {verb} {entry.job.job_id}")
        return 0

    return wrap


cmd_cancel = _ask("cancel")
cmd_abort = _ask("abort")


@_remote_or_local
def cmd_retry(args: argparse.Namespace, _argv: list[str]) -> int:
    queue_dir = _queue_dir(args)
    again = remote.retry(queue_dir, remote.resolve(queue_dir, args.job))
    print(again.job.job_id)
    return 0


@_remote_or_local
def cmd_remove(args: argparse.Namespace, _argv: list[str]) -> int:
    print(f"removed {remote.remove(_queue_dir(args), args.job).job.job_id}")
    return 0


@_cli_errors
def cmd_reserve(args: argparse.Namespace, _argv: list[str]) -> int:
    # Prints the directory to rsync into. The manifest is written by the
    # `commit` that follows, and until then the runner cannot see this.
    # Runs locally on the box (submit --host ssh's this in).
    _, path = spool.reserve(_queue_dir(args), args.name, remote.local_user())
    print(path)
    return 0


@_cli_errors
def cmd_commit(args: argparse.Namespace, _argv: list[str]) -> int:
    entry = spool.commit(
        args.path,
        spool.Job(
            job_id=args.path.name,
            name=args.name,
            user=args.user or remote.local_user(),
            command=args.command,
            submitted_unix=time.time(),
            git_sha=args.git_sha,
            git_dirty=args.git_dirty,
            image=args.image,
        ),
    )
    print(entry.job.job_id)
    return 0


@_cli_errors
def cmd_contract(_args: argparse.Namespace, _argv: list[str]) -> int:
    """Print the box contract fields (registry_url and the rest)."""
    contract = box.load()
    if contract is None:
        raise box.NotProvisioned(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )
    print(f"shared_dir = {contract.shared_dir}")
    print(f"shared_group = {contract.shared_group}")
    print(f"textfile_dir = {contract.textfile_dir}")
    print(f"prometheus_url = {contract.prometheus_url}")
    print(f"grafana_url = {contract.grafana_url}")
    print(f"registry_url = {contract.registry_url}")
    return 0


def _queue_dir(args: argparse.Namespace) -> Path:
    given = getattr(args, "shared_dir", None)
    if given:
        return Path(given) / "queue"
    contract = box.load()
    if contract is None:
        raise box.NotProvisioned(
            f"set {remote.HOST_ENV} or pass --host; "
            f"the client always talks to the box"
        )
    queue_dir = contract.queue_dir
    if not queue_dir.is_dir():
        # Its existence is the box saying it runs a queue, and it is a fact on
        # disk rather than a claim in a file. sparks does not create it: sparkup
        # gives it the group and the setgid bit that let colleagues share it,
        # and one made here would have neither.
        raise box.NotProvisioned(
            f"this box is provisioned for sparks but not for the queue: "
            f"{queue_dir} does not exist. Converge sparkup (cd sparkup && "
            f"make apply) to add the queue service"
        )
    return queue_dir


def _without_host(argv: list[str]) -> list[str]:
    """The same command, minus the flag that sent it over there.

    Left in, the box would try to ssh onwards to itself.
    """
    kept: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token == "--host":
            skip = True
            continue
        if token.startswith("--host="):
            continue
        kept.append(token)
    return kept


if __name__ == "__main__":
    sys.exit(main())
