"""sparks demo --name e0

Plays a synthetic run against the box's Prometheus and prints the Grafana link
to watch it on.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from sparks import demo, launcher

DASHBOARD = "/d/training-runs/training-runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks", description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:9090",
        help="Prometheus, which must have the remote-write receiver enabled",
    )
    parser.add_argument(
        "--grafana", default="http://spark.local", help="where the dashboard lives"
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    launch = sub.add_parser(
        "run", help="wrap a training command and record what it cost"
    )
    launch.add_argument("--name", default="run")
    launch.add_argument(
        "--shared-dir",
        default="/srv/spark",
        help="sparkup's spark_shared_dir; the repo default, not any one box's",
    )
    launch.add_argument(
        "--baseline-seconds",
        type=float,
        default=launcher.BASELINE_SECONDS,
        help="idle power sampled first, so marginal energy means something",
    )
    launch.add_argument(
        "command", nargs="+", help="the command to run, after a -- separator"
    )

    run = sub.add_parser("demo", help="play a synthetic run")
    run.add_argument("--name", default="demo")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument(
        "--epochs",
        type=int,
        default=demo.EPOCHS,
        help="shorten an acceptance run without editing the module",
    )
    return parser


def deep_link(grafana: str, run_id: str, started: float) -> str:
    """The live form. Backdated a minute so the first samples are not glued to
    the left edge of the graph."""
    frm = int((started - 60) * 1000)
    return (
        f"{grafana.rstrip('/')}{DASHBOARD}"
        f"?orgId=1&var-run_id={run_id}&from={frm}&to=now&refresh=10s"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    if args.command_name == "run":
        return _run(args)
    started = time.time()
    run_id = demo.run(args.url, name=args.name, seed=args.seed, epochs=args.epochs)
    print(run_id)
    print(deep_link(args.grafana, run_id, started))
    return 0


def _run(args: argparse.Namespace) -> int:
    started = time.time()
    result = launcher.launch(
        args.command,
        name=args.name,
        shared_dir=Path(args.shared_dir),
        url=args.url,
        baseline_seconds=args.baseline_seconds,
    )
    print(result.run_id)
    print(deep_link(args.grafana, result.run_id, started))
    print(f"{result.status}  ->  {result.run_dir}")
    # Faithful status, so `$?` means something to whatever called us.
    return result.wrapper_exit


if __name__ == "__main__":
    sys.exit(main())
