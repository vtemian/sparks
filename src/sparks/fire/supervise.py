import argparse
import functools
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from sparks import box, summary
from sparks.fire import launch as launcher

LOG = logging.getLogger("sparks")

DASHBOARD = "/d/training-runs/training-runs"

GRAFANA_FALLBACK = "http://spark.local"


def deep_link(grafana: str, run_id: str, started: float) -> str:
    frm = int((started - 60) * 1000)
    return (
        f"{grafana.rstrip('/')}{DASHBOARD}"
        f"?orgId=1&var-run_id={run_id}&from={frm}&to=now&refresh=10s"
    )


class _Settings:
    def __init__(self, shared_dir: Path, url: str, grafana: str) -> None:
        self.shared_dir = shared_dir
        self.url = url
        self.grafana = grafana


def read_settings(args: argparse.Namespace) -> _Settings:
    contract = box.load()
    shared = Path(args.shared_dir) if args.shared_dir else None
    url = args.url

    if contract is None:
        missing = [
            name
            for name, value in (("--shared-dir", shared), ("--url", url))
            if value is None
        ]
        require_provisioned(missing)
    else:
        require_matching(box.preflight(contract))
        shared = shared or contract.shared_dir
        url = contract.prometheus_url if url is None else url

    assert shared is not None  # noqa: S101 -- narrowing; unprovisioned raised above
    return _Settings(
        shared_dir=shared,
        url=url or "",
        grafana=args.grafana
        or (contract.grafana_url if contract else "")
        or GRAFANA_FALLBACK,
    )


def require_provisioned(missing: list[str]) -> None:
    if not missing:
        return

    flags = " ".join(f"{flag} ..." for flag in missing)
    raise box.NotProvisionedError(
        f"this box is not configured for sparks.\n\n"
        f"{box.config_path()} does not exist, so nothing here knows where "
        f"runs are recorded on this machine or which Prometheus to publish "
        f"them to. sparks reads that file; sparkup writes it.\n\n"
        f"Provision the box:\n"
        f"    cd sparkup && make apply\n\n"
        f"Or, on a box sparkup does not manage, say so explicitly:\n"
        f"    python -m sparks.fire.supervise {flags}"
    )


def require_matching(complaints: list[str]) -> None:
    if not complaints:
        return

    problems = "\n".join(f"    {complaint}" for complaint in complaints)
    raise box.NotProvisionedError(
        f"this box's sparks configuration does not match the box.\n\n"
        f"{box.config_path()} describes provisioning that is not there:\n"
        f"{problems}\n\n"
        f"Either provisioning did not finish or the box changed under it. "
        f"Converge it again:\n"
        f"    cd sparkup && make apply"
    )


def announce(
    target: Path | None,
) -> Callable[[str, Path], None] | None:
    if target is None:
        return None

    def write(run_id: str, _run_dir: Path) -> None:
        line = run_id + "\n"
        render = functools.partial(str, line)
        summary.write_atomically(target, render)

    return write


def run(args: argparse.Namespace) -> int:
    settings = read_settings(args)
    started = time.time()

    result = launcher.launch(
        args.command,
        name=args.name,
        shared_dir=settings.shared_dir,
        url=settings.url,
        baseline_seconds=args.baseline_seconds,
        on_reserved=announce(args.run_id_file),
        sha=args.git_sha,
    )

    print(result.run_id)
    print(deep_link(settings.grafana, result.run_id, started))
    print(f"{result.status}  ->  {result.run_dir}")
    # Faithful status, so `$?` means something to whatever called us.
    return result.wrapper_exit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparks.fire.supervise",
        description="Supervise one job: measure it and record how it ended.",
    )
    # No defaults on these three: unset must stay distinct from `--url ""`,
    # which is how you ask for no telemetry at all.

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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(given)

    try:
        return run(args)
    except (box.NotProvisionedError, box.MalformedError) as exc:
        print(f"sparks: {exc}", file=sys.stderr)
        return os.EX_CONFIG


if __name__ == "__main__":
    sys.exit(main())
