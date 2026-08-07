"""fire control verbs — the SSH-RPC surface inside the queue container."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from sparks import box, spool
from sparks.fire import control

VERBS = frozenset(
    {"queue", "cancel", "abort", "retry", "remove", "reserve", "commit", "contract"}
)


def queue(args: argparse.Namespace) -> int:
    qd = control.queue_dir(args.shared_dir)
    entries = spool.entries(qd) if args.all else spool.publishable(qd)
    print(control.render(entries), end="")
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
    qd = control.queue_dir(args.shared_dir)
    again = control.retry(qd, control.resolve(qd, args.job))
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
    c = box.load()
    if c is None:
        raise box.NotProvisionedError(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )

    print(f"shared_dir = {c.shared_dir}")
    print(f"shared_group = {c.shared_group}")
    print(f"textfile_dir = {c.textfile_dir}")
    print(f"prometheus_url = {c.prometheus_url}")
    print(f"grafana_url = {c.grafana_url}")
    print(f"registry_url = {c.registry_url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fire", description=__doc__)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--shared-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="verb", required=True)

    q = sub.add_parser(
        "queue",
        parents=[shared],
        help="what is running and what is waiting",
    )
    q.add_argument("--all", action="store_true")
    q.set_defaults(func=queue)

    for verb, help_text, func in (
        ("cancel", "drop a job that has not started yet", cancel),
        ("abort", "stop a job, whether it has started or not", abort),
        ("retry", "submit the same job again", retry),
        ("remove", "delete a finished job", remove),
    ):
        p = sub.add_parser(verb, parents=[shared], help=help_text)
        p.add_argument("job")
        p.set_defaults(func=func)

    reserve_p = sub.add_parser("reserve", parents=[shared], help=argparse.SUPPRESS)
    reserve_p.add_argument("--name", default="job")
    reserve_p.add_argument("--user", default="fire")
    reserve_p.set_defaults(func=reserve)

    commit_p = sub.add_parser("commit", help=argparse.SUPPRESS)
    commit_p.add_argument("path", type=Path)
    commit_p.add_argument("--name", default="job")
    commit_p.add_argument("--user", default="fire")
    commit_p.add_argument("--git-sha", default="unknown")
    commit_p.add_argument("--git-dirty", action="store_true")
    commit_p.add_argument("--image", required=True)
    commit_p.add_argument("command", nargs="+")
    commit_p.set_defaults(func=commit)

    contract_p = sub.add_parser("contract", help=argparse.SUPPRESS)
    contract_p.set_defaults(func=contract)

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        func = cast(Callable[[argparse.Namespace], int], args.func)
        return func(args)
    except control.ControlError as e:
        print(f"fire: {e}", file=sys.stderr)
        return 1
    except (box.NotProvisionedError, box.MalformedError) as e:
        print(f"fire: {e}", file=sys.stderr)
        return os.EX_CONFIG
