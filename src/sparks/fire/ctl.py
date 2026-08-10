from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sparks import box, spool
from sparks.fire import control

if TYPE_CHECKING:
    from collections.abc import Callable

VERBS = frozenset(
    {
        "queue",
        "logs",
        "status",
        "cancel",
        "abort",
        "retry",
        "remove",
        "reserve",
        "commit",
        "contract",
    }
)


def queue(args: argparse.Namespace) -> int:
    directory = control.queue_dir(args.shared_dir)
    entries = spool.entries(directory) if args.all else spool.publishable(directory)
    print(control.as_json(entries) if args.json else control.render(entries), end="")
    return 0


def logs(args: argparse.Namespace) -> int:
    entry = control.resolve(control.queue_dir(args.shared_dir), args.job)
    tail = None if args.all else args.tail
    print(control.logs(entry, args.shared_dir, tail), end="")
    return 0


def status(args: argparse.Namespace) -> int:
    entry = control.resolve(control.queue_dir(args.shared_dir), args.job)
    payload = control.status(entry, args.shared_dir)
    rendered = (
        json.dumps(payload, indent=2) + "\n"
        if args.json
        else control.render_status(payload)
    )
    print(rendered, end="")
    return 0


def cancel(args: argparse.Namespace) -> int:
    entry = control.ask(control.queue_dir(args.shared_dir), args.job, "cancel")
    print(f"asked the runner to cancel {entry.job.job_id}")
    return 0


def abort(args: argparse.Namespace) -> int:
    entry = control.ask(control.queue_dir(args.shared_dir), args.job, "abort")
    print(f"asked the runner to abort {entry.job.job_id}")
    return 0


def retry(args: argparse.Namespace) -> int:
    directory = control.queue_dir(args.shared_dir)
    again = control.retry(directory, control.resolve(directory, args.job))
    print(again.job.job_id)
    return 0


def remove(args: argparse.Namespace) -> int:
    entry = control.remove(control.queue_dir(args.shared_dir), args.job)
    print(f"removed {entry.job.job_id}")
    return 0


def reserve(args: argparse.Namespace) -> int:
    _, path = spool.reserve(control.queue_dir(args.shared_dir), args.name, args.user)
    print(path)
    return 0


def commit(args: argparse.Namespace) -> int:
    entry = spool.commit(
        args.path,
        spool.Job(
            job_id=args.path.name,
            name=args.name,
            user=args.user,
            command=args.command,
            submitted_unix=time.time(),
            git_sha=args.git_sha,
            git_dirty=args.git_dirty,
            image=args.image,
        ),
    )
    print(entry.job.job_id)
    return 0


def contract(_args: argparse.Namespace) -> int:
    provisioned = box.load()
    if provisioned is None:
        raise box.NotProvisionedError(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )

    print(f"shared_dir = {provisioned.shared_dir}")
    print(f"shared_group = {provisioned.shared_group}")
    print(f"textfile_dir = {provisioned.textfile_dir}")
    print(f"prometheus_url = {provisioned.prometheus_url}")
    print(f"grafana_url = {provisioned.grafana_url}")
    print(f"registry_url = {provisioned.registry_url}")
    return 0


type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fire", description="fire control verbs, over SSH from a laptop."
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--shared-dir", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="verb", required=True)
    add_queue(subparsers, shared)
    add_logs(subparsers, shared)
    add_status(subparsers, shared)
    for verb, help_text, func in (
        ("cancel", "drop a job that has not started yet", cancel),
        ("abort", "stop a job, whether it has started or not", abort),
        ("retry", "submit the same job again", retry),
        ("remove", "delete a finished job", remove),
    ):
        verb_parser = subparsers.add_parser(verb, parents=[shared], help=help_text)
        verb_parser.add_argument("job")
        verb_parser.set_defaults(func=func)
    add_rpc_verbs(subparsers, shared)
    return parser


def add_queue(subparsers: Subparsers, shared: argparse.ArgumentParser) -> None:
    queue_parser = subparsers.add_parser(
        "queue",
        parents=[shared],
        help="what is running and what is waiting",
    )
    queue_parser.add_argument("--all", action="store_true")
    queue_parser.add_argument("--json", action="store_true")
    queue_parser.set_defaults(func=queue)


def add_logs(subparsers: Subparsers, shared: argparse.ArgumentParser) -> None:
    logs_parser = subparsers.add_parser(
        "logs",
        parents=[shared],
        help="what the job printed",
    )
    logs_parser.add_argument("job")
    logs_parser.add_argument("--tail", type=int, default=control.TAIL_LINES)
    logs_parser.add_argument(
        "--all",
        action="store_true",
        help="every line, not the last --tail of them",
    )
    logs_parser.set_defaults(func=logs)


def add_status(subparsers: Subparsers, shared: argparse.ArgumentParser) -> None:
    status_parser = subparsers.add_parser(
        "status",
        parents=[shared],
        help="one job in full, with the record of its run",
    )
    status_parser.add_argument("job")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=status)


def add_rpc_verbs(subparsers: Subparsers, shared: argparse.ArgumentParser) -> None:
    reserve_parser = subparsers.add_parser(
        "reserve", parents=[shared], help=argparse.SUPPRESS
    )
    reserve_parser.add_argument("--name", default="job")
    reserve_parser.add_argument("--user", default="fire")
    reserve_parser.set_defaults(func=reserve)

    commit_parser = subparsers.add_parser("commit", help=argparse.SUPPRESS)
    commit_parser.add_argument("path", type=Path)
    commit_parser.add_argument("--name", default="job")
    commit_parser.add_argument("--user", default="fire")
    commit_parser.add_argument("--git-sha", default="unknown")
    commit_parser.add_argument("--git-dirty", action="store_true")
    commit_parser.add_argument("--image", required=True)
    commit_parser.add_argument("command", nargs="+")
    commit_parser.set_defaults(func=commit)

    contract_parser = subparsers.add_parser("contract", help=argparse.SUPPRESS)
    contract_parser.set_defaults(func=contract)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        func = cast("Callable[[argparse.Namespace], int]", args.func)
        return func(args)
    except control.ControlError as exc:
        print(f"fire: {exc}", file=sys.stderr)
        return 1
    except (box.NotProvisionedError, box.MalformedError) as exc:
        print(f"fire: {exc}", file=sys.stderr)
        return os.EX_CONFIG
