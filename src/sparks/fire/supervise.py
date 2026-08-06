"""sparks.fire.supervise -- python train.py

Wraps a training command, records what it cost, and prints the Grafana link.
Private to the fire package: the queue nests it via `python -m`, not as a
console script. Used inside the queue container (and on the box) — not the
laptop client.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from sparks import box, summary
from sparks.fire import launch as launcher

LOG = logging.getLogger("sparks")

DASHBOARD = "/d/training-runs/training-runs"

# sysexits.h EX_CONFIG. A distinct code so a queue, or a shell running this in a
# loop, can tell "this box is set up wrong" from "the training job crashed".
EX_CONFIG = 78

# Cosmetic: it only decorates the printed link, so an unknown Grafana is not
# worth refusing a run over.
GRAFANA_FALLBACK = "http://spark.local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparks.fire.supervise", description=__doc__)
    # No defaults on any of these three. They are properties of the box, and the
    # box states them in /etc/sparks/box.toml. An unset value stays distinct from
    # `--url ""`, which is how you ask for no telemetry at all.
    parser.add_argument(
        "--url",
        help="Prometheus, which must have the remote-write receiver enabled. "
        "Defaults to the provisioned one; pass empty to publish nothing",
    )
    parser.add_argument("--grafana", help="where the dashboards live")
    parser.add_argument("--name", default="run")
    parser.add_argument(
        "--shared-dir",
        help="where runs are recorded. Defaults to the provisioned "
        "spark_shared_dir, which is a per-box value and not this repo's guess",
    )
    parser.add_argument(
        "-b",
        "--baseline-seconds",
        type=float,
        default=launcher.BASELINE_SECONDS,
        help="idle power sampled first, so marginal energy means something",
    )
    parser.add_argument(
        "--git-sha",
        default=None,
        help="the commit to record. Defaults to this directory's HEAD, which is "
        "wrong when something else shipped the code being run",
    )
    parser.add_argument(
        "--run-id-file",
        type=Path,
        help="write the run id here as soon as it is known, for a supervisor "
        "that needs to know which run this is before the run has ended",
    )
    parser.add_argument(
        "command", nargs="+", help="the command to run, after a -- separator"
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
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)
    try:
        return cmd_run(args)
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"sparks: {e}", file=sys.stderr)
        return EX_CONFIG


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    started = time.time()
    result = launcher.launch(
        args.command,
        name=args.name,
        shared_dir=settings.shared_dir,
        url=settings.url,
        baseline_seconds=args.baseline_seconds,
        on_reserved=_announce(args.run_id_file),
        sha=args.git_sha,
    )
    print(result.run_id)
    print(deep_link(settings.grafana, result.run_id, started))
    print(f"{result.status}  ->  {result.run_dir}")
    # Faithful status, so `$?` means something to whatever called us.
    return result.wrapper_exit


class _Settings:
    """The three box-shaped values a run needs, after the flags and the contract
    have been reconciled."""

    def __init__(self, shared_dir: Path, url: str, grafana: str) -> None:
        self.shared_dir = shared_dir
        self.url = url
        self.grafana = grafana


def _settings(args: argparse.Namespace) -> _Settings:
    """Flags win, then the contract. Nothing is guessed except the Grafana link.

    Raises rather than returning a partial answer: a run recorded into a
    directory nobody reads is the failure this whole path exists to prevent, and
    a default here is exactly how that happens.
    """
    contract = box.load()
    shared = Path(args.shared_dir) if args.shared_dir else None
    url = args.url
    if contract is None:
        missing = [
            n for n, v in (("--shared-dir", shared), ("--url", url)) if v is None
        ]
        if missing:
            raise box.NotProvisioned(_unprovisioned(missing))
    else:
        complaints = box.preflight(contract)
        if complaints:
            raise box.NotProvisioned(_mismatched(complaints))
        shared = shared or contract.shared_dir
        url = contract.prometheus_url if url is None else url
    assert shared is not None  # unprovisioned path raised above when missing
    return _Settings(
        shared_dir=shared,
        url=url or "",
        grafana=args.grafana
        or (contract.grafana_url if contract else "")
        or GRAFANA_FALLBACK,
    )


def _unprovisioned(missing: list[str]) -> str:
    flags = " ".join(f"{f} ..." for f in missing)
    return (
        f"this box is not configured for sparks.\n\n"
        f"{box.config_path()} does not exist, so nothing here knows where runs "
        f"are recorded on this machine or which Prometheus to publish them to. "
        f"sparks reads that file; sparkup writes it.\n\n"
        f"Provision the box:\n"
        f"    cd sparkup && make apply\n\n"
        f"Or, on a box sparkup does not manage, say so explicitly:\n"
        f"    python -m sparks.fire.supervise {flags}"
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


def _announce(
    target: Path | None,
) -> Callable[[str, Path], None] | None:
    """Publish the run id the moment it exists, for whoever asked.

    A file rather than a line on stdout: stdout carries the training's own
    output, so anything parsing it would be reading the run's log for a control
    signal - which works right up until a job prints something that looks like a
    run id.
    """
    if target is None:
        return None

    def write(run_id: str, _run_dir: Path) -> None:
        summary.write_atomically(target, lambda: run_id + "\n")

    return write


if __name__ == "__main__":
    sys.exit(main())
