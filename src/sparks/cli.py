"""sparks demo --name e0

Plays a synthetic run against the box's Prometheus and prints the Grafana link
to watch it on.
"""

import argparse
import logging
import sys
import time

from sparks import demo

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
    sub = parser.add_subparsers(dest="command", required=True)

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
    started = time.time()
    run_id = demo.run(args.url, name=args.name, seed=args.seed, epochs=args.epochs)
    print(run_id)
    print(deep_link(args.grafana, run_id, started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
