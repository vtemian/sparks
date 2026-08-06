"""fire control verbs — the SSH-RPC surface inside the queue container."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sparks import box, spool
from sparks.fire import control

EX_CONFIG = 78

VERBS = frozenset(
    {"queue", "cancel", "abort", "retry", "remove", "reserve", "commit", "contract"}
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fire", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    q = sub.add_parser("queue", help="what is running and what is waiting")
    q.add_argument("--all", action="store_true")
    _shared(q)
    q.set_defaults(func=queue)

    for verb, help_text in (
        ("cancel", "drop a job that has not started yet"),
        ("abort", "stop a job, whether it has started or not"),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("job")
        _shared(p)
        p.set_defaults(func=_ask(verb))

    for verb, help_text, func in (
        ("retry", "submit the same job again", retry),
        ("remove", "delete a finished job", remove),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("job")
        _shared(p)
        p.set_defaults(func=func)

    reserve_p = sub.add_parser("reserve", help=argparse.SUPPRESS)
    reserve_p.add_argument("--name", default="job")
    reserve_p.add_argument("--user", default="fire")
    _shared(reserve_p)
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


def _shared(p: argparse.ArgumentParser) -> None:
    p.add_argument("--shared-dir", type=Path, default=None)


def ctl_main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except control.ControlError as e:
        print(f"fire: {e}", file=sys.stderr)
        return 1
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"fire: {e}", file=sys.stderr)
        return EX_CONFIG


def queue(args: argparse.Namespace) -> int:
    qd = control.queue_dir(args.shared_dir)
    entries = spool.entries(qd) if args.all else spool.publishable(qd)
    print(control.render(entries), end="")
    return 0


def _ask(action: str):
    def run(args: argparse.Namespace) -> int:
        entry = control.ask(control.queue_dir(args.shared_dir), args.job, action)
        print(f"asked the runner to {action} {entry.job.job_id}")
        return 0

    return run


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
    _, path = spool.reserve(
        control.queue_dir(args.shared_dir), args.name, args.user
    )
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
        raise box.NotProvisioned(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )
    print(f"shared_dir = {c.shared_dir}")
    print(f"shared_group = {c.shared_group}")
    print(f"textfile_dir = {c.textfile_dir}")
    print(f"prometheus_url = {c.prometheus_url}")
    print(f"grafana_url = {c.grafana_url}")
    print(f"registry_url = {c.registry_url}")
    return 0
