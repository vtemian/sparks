"""sparks demo --name e0

Plays a synthetic run against the box's Prometheus and prints the Grafana link
to watch it on.

This module is the boundary where "you are on a box provisioned for sparks"
stops being an assumption and becomes a check. `launcher.launch()` stays a plain
library call taking explicit paths, so embedding sparks in a harness and running
the unit suite both keep working; the command-line entry points refuse to guess.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from sparks import box, demo, launcher

DASHBOARD = "/d/training-runs/training-runs"

# sysexits.h EX_CONFIG. A distinct code so a queue, or a shell running this in a
# loop, can tell "this box is set up wrong" from "the training job crashed".
# Those need different people looking at them.
EX_CONFIG = 78

# Cosmetic: it only decorates the printed link, so an unknown Grafana is not
# worth refusing a run over. Unlike the shared directory, being wrong here
# costs a bad URL, not a lost record.
GRAFANA_FALLBACK = "http://spark.local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks", description=__doc__)
    # No defaults on any of these three. They are properties of the box, and the
    # box states them in /etc/sparks/box.toml. An unset value stays distinct from
    # `--url ""`, which is how you ask for no telemetry at all.
    parser.add_argument(
        "--url",
        help="Prometheus, which must have the remote-write receiver enabled. "
        "Defaults to the provisioned one; pass empty to publish nothing",
    )
    parser.add_argument("--grafana", help="where the dashboards live")
    sub = parser.add_subparsers(dest="command_name", required=True)

    launch = sub.add_parser(
        "run", help="wrap a training command and record what it cost"
    )
    launch.add_argument("--name", default="run")
    launch.add_argument(
        "--shared-dir",
        help="where runs are recorded. Defaults to the provisioned "
        "spark_shared_dir, which is a per-box value and not this repo's guess",
    )
    launch.add_argument(
        "-b",
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
    wants_shared = args.command_name == "run"
    try:
        settings = _settings(args, wants_shared=wants_shared)
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"sparks: {e}", file=sys.stderr)
        return EX_CONFIG
    if wants_shared:
        return _run(args, settings)
    started = time.time()
    run_id = demo.run(settings.url, name=args.name, seed=args.seed, epochs=args.epochs)
    print(run_id)
    print(deep_link(settings.grafana, run_id, started))
    return 0


class _Settings:
    """The three box-shaped values this invocation needs, after the flags and the
    contract have been reconciled."""

    def __init__(self, shared_dir: Path | None, url: str, grafana: str) -> None:
        self.url = url
        self.grafana = grafana
        self._shared_dir = shared_dir

    @property
    def shared_dir(self) -> Path:
        assert self._shared_dir is not None, "only `run` asks, and it required it"
        return self._shared_dir


def _settings(args: argparse.Namespace, *, wants_shared: bool) -> _Settings:
    """Flags win, then the contract. Nothing is guessed except the Grafana link.

    Raises rather than returning a partial answer: a run recorded into a
    directory nobody reads is the failure this whole path exists to prevent, and
    a default here is exactly how that happens.
    """
    contract = box.load()
    # getattr: --shared-dir belongs to `run`, so the demo namespace lacks it.
    given = getattr(args, "shared_dir", None)
    shared = Path(given) if given else None
    url = args.url
    if contract is None:
        missing = [
            n for n, v in (("--shared-dir", shared), ("--url", url)) if v is None
        ]
        if wants_shared and missing:
            raise box.NotProvisioned(_unprovisioned("run", missing))
        if not wants_shared and url is None:
            raise box.NotProvisioned(_unprovisioned("demo", ["--url"]))
    else:
        complaints = box.preflight(contract)
        if complaints:
            raise box.NotProvisioned(_mismatched(complaints))
        shared = shared or contract.shared_dir
        url = contract.prometheus_url if url is None else url
    return _Settings(
        shared_dir=shared,
        url=url or "",
        grafana=args.grafana
        or (contract.grafana_url if contract else "")
        or GRAFANA_FALLBACK,
    )


def _unprovisioned(command: str, missing: list[str]) -> str:
    flags = " ".join(f"{f} ..." for f in missing)
    return (
        f"this box is not configured for sparks.\n\n"
        f"{box.config_path()} does not exist, so nothing here knows where runs "
        f"are recorded on this machine or which Prometheus to publish them to. "
        f"sparks reads that file; sparkup writes it.\n\n"
        f"Provision the box:\n"
        f"    cd sparkup && make apply\n\n"
        f"Or, on a box sparkup does not manage, say so explicitly:\n"
        f"    sparks {command} {flags}"
    )


def _mismatched(complaints: list[str]) -> str:
    problems = "\n".join(f"    {c}" for c in complaints)
    return (
        f"this box's sparks configuration does not match the box.\n\n"
        f"{box.config_path()} describes provisioning that is not there:\n"
        f"{problems}\n\n"
        f"Either provisioning did not finish or the box changed under it. "
        f"Converge it again:\n"
        f"    cd sparkup && make apply"
    )


def _run(args: argparse.Namespace, settings: _Settings) -> int:
    started = time.time()
    result = launcher.launch(
        args.command,
        name=args.name,
        shared_dir=settings.shared_dir,
        url=settings.url,
        baseline_seconds=args.baseline_seconds,
    )
    print(result.run_id)
    print(deep_link(settings.grafana, result.run_id, started))
    print(f"{result.status}  ->  {result.run_dir}")
    # Faithful status, so `$?` means something to whatever called us.
    return result.wrapper_exit


if __name__ == "__main__":
    sys.exit(main())
