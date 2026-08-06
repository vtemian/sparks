"""fire — queue daemon and control verbs.

Without a leading verb, flags are the main argparse surface so compose can
pass `--url … --shared-dir …` directly. Leading tokens in ctl.VERBS are the
SSH-RPC control surface (queue, cancel, abort, …).
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from sparks import box, spool
from sparks.fire import ctl, engine, runner

LOG = logging.getLogger("sparks")


def serve(args: argparse.Namespace) -> int:
    """The queue container's entry point."""
    contract = box.load()
    shared_dir = args.shared_dir or (contract.shared_dir if contract else None)
    if shared_dir is None:
        raise box.NotProvisioned(_unprovisioned(["--shared-dir"]))

    url = args.url if args.url is not None else _runner_url(contract)
    textfile = args.textfile_dir or box.textfile_dir(contract)
    queue_dir = shared_dir / "queue"

    LOG.info("fire: serving %s, publishing to %s", queue_dir, textfile)
    spool.make_queue_dir(queue_dir)
    runner.Runner(
        queue_dir=queue_dir,
        engine=engine.Docker(
            shared_dir=shared_dir,
            url=url,
            gpus=args.gpus,
            extra_groups=[g for g in [engine.docker_group()] if g is not None],
        ),
        textfile_dir=textfile,
        poll_seconds=args.poll_seconds,
    ).serve(ticks=args.ticks)
    return 0


def _runner_url(contract: box.Box | None) -> str:
    """Prometheus, as reachable from inside the queue container.

    The contract's URL is loopback, which in here is this container. Rewritten
    rather than refused because every other consumer of the contract is on the
    host, where loopback is right, and one of them being containerised is this
    module's problem rather than the box's.
    """
    if contract is None:
        return ""

    url = contract.prometheus_url
    for loopback in ("127.0.0.1", "localhost", "::1"):
        if loopback in url:
            return url.replace(loopback, "host.docker.internal")
    return url


def _unprovisioned(missing: list[str]) -> str:
    flags = " ".join(f"{f} ..." for f in missing)
    return (
        f"this box is not configured for sparks.\n\n"
        f"{box.config_path()} does not exist, so nothing here knows where the "
        f"queue lives. sparks reads that file; sparkup writes it.\n\n"
        f"Provision the box:\n"
        f"    cd sparkup && make apply\n\n"
        f"Or say so explicitly:\n"
        f"    fire {flags}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fire", description=__doc__)

    parser.add_argument(
        "--url",
        help="Prometheus as reachable from inside the queue container. "
        "Defaults to the contract's URL rewritten off loopback",
    )
    parser.add_argument(
        "--textfile-dir",
        type=Path,
        help="where to publish the queue's metrics. Defaults to the "
        "provisioned node_exporter textfile directory",
    )
    parser.add_argument("--shared-dir", type=Path)

    parser.add_argument(
        "--gpus",
        default="all",
        help="passed to docker run --gpus. Empty omits the flag, which a box "
        "with no nvidia runtime needs: there it is an error, not a no-op",
    )
    parser.add_argument("--poll-seconds", type=float, default=runner.POLL_SECONDS)
    parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="stop after this many passes, for testing the wiring",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])

    if given and not given[0].startswith("-") and given[0] in ctl.VERBS:
        return ctl.main(given)

    args = build_parser().parse_args(given)
    try:
        return serve(args)
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"fire: {e}", file=sys.stderr)
        return os.EX_CONFIG


if __name__ == "__main__":
    sys.exit(main())
