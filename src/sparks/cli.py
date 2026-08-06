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
from collections.abc import Callable
from pathlib import Path

from sparks import box, client, demo, engine, launcher, runner, spool, summary

LOG = logging.getLogger("sparks")

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
        "--git-sha",
        default=None,
        help="the commit to record. Defaults to this directory's HEAD, which is "
        "wrong when something else shipped the code being run",
    )
    launch.add_argument(
        "--run-id-file",
        type=Path,
        help="write the run id here as soon as it is known, for a supervisor "
        "that needs to know which run this is before the run has ended",
    )
    launch.add_argument(
        "command", nargs="+", help="the command to run, after a -- separator"
    )

    _add_queue_commands(sub)

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


def _add_queue_commands(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """The queue: submit work and then close the laptop.

    Every one of these takes `--host`, and when it is given the whole command is
    forwarded over ssh rather than each verb having a remote implementation.
    """
    submit = sub.add_parser(
        "submit", help="queue a job on the box: ship the code, build, run"
    )
    submit.add_argument("--name", default="job")
    submit.add_argument(
        "--context",
        type=Path,
        default=Path.cwd(),
        help="the project to build, which must contain a Dockerfile. "
        "Defaults to the current directory",
    )
    submit.add_argument(
        "--image",
        help="run this image instead of building anything. For a job whose "
        "image is already published",
    )
    _add_host(submit)
    submit.add_argument("command", nargs="+", help="after a -- separator")

    listing = sub.add_parser("queue", help="what is running and what is waiting")
    listing.add_argument(
        "--all",
        action="store_true",
        help="include jobs that finished long enough ago to have aged out",
    )
    _add_host(listing)

    for verb, helping in (
        ("cancel", "drop a job that has not started yet"),
        ("abort", "stop a job, whether it has started or not"),
        ("retry", "submit the same job again, reusing the code already there"),
        ("remove", "delete a finished job and the code it kept"),
    ):
        parser = sub.add_parser(verb, help=helping)
        parser.add_argument("job", help="a job id, a unique part of one, or its name")
        _add_host(parser)

    # Plumbing. `submit --host` is three steps against the box and these are two
    # of them; they are not meant to be typed. See client.submit_remote.
    reserve = sub.add_parser("reserve", help=argparse.SUPPRESS)
    reserve.add_argument("--name", default="job")
    reserve.add_argument("--shared-dir", type=Path, default=None)

    commit = sub.add_parser("commit", help=argparse.SUPPRESS)
    commit.add_argument("path", type=Path)
    commit.add_argument("--name", default="job")
    commit.add_argument("--user", default=None)
    commit.add_argument("--git-sha", default="unknown")
    commit.add_argument("--git-dirty", action="store_true")
    commit.add_argument("--image", default=None)
    commit.add_argument("command", nargs="+")

    daemon = sub.add_parser(
        "runner", help="process the queue; the queue container's entry point"
    )
    daemon.add_argument(
        "--textfile-dir",
        type=Path,
        help="where to publish the queue's metrics. Defaults to the "
        "provisioned node_exporter textfile directory",
    )
    daemon.add_argument("--shared-dir", type=Path)
    daemon.add_argument(
        "--gpus",
        default="all",
        help="passed to docker run --gpus. Empty omits the flag, which a box "
        "with no nvidia runtime needs: there it is an error, not a no-op",
    )
    daemon.add_argument("--poll-seconds", type=float, default=runner.POLL_SECONDS)
    daemon.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="stop after this many passes, for testing the wiring",
    )


def _add_host(parser: argparse.ArgumentParser) -> None:
    """Which queue, and on which machine.

    Both default to the box's own answer, so neither is normally typed. They
    exist for the same reason `run --shared-dir` does: a box sparkup does not
    manage, and the test suite.
    """
    parser.add_argument(
        "--host",
        default=None,
        help=f"run this on the box over ssh instead of here. "
        f"Defaults to ${client.HOST_ENV}",
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        default=None,
        help="the shared tree whose queue to use. Defaults to the provisioned one",
    )


def deep_link(grafana: str, run_id: str, started: float) -> str:
    """The live form. Backdated a minute so the first samples are not glued to
    the left edge of the graph."""
    frm = int((started - 60) * 1000)
    return (
        f"{grafana.rstrip('/')}{DASHBOARD}"
        f"?orgId=1&var-run_id={run_id}&from={frm}&to=now&refresh=10s"
    )


QUEUE_COMMANDS = frozenset({"submit", "queue", "cancel", "abort", "retry", "remove"})
"""Verbs that work equally well from a laptop, given `--host`."""

BOX_ONLY = frozenset({"reserve", "commit", "runner"})
"""Verbs that only make sense where the queue actually is."""


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    if args.command_name in QUEUE_COMMANDS or args.command_name in BOX_ONLY:
        try:
            return _queue(args, argv)
        except client.ClientError as e:
            print(f"sparks: {e}", file=sys.stderr)
            return 1
        except (box.NotProvisioned, box.Malformed) as e:
            print(f"sparks: {e}", file=sys.stderr)
            return EX_CONFIG
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


def _queue(args: argparse.Namespace, argv: list[str] | None) -> int:
    """Every queue verb, either here or on the box."""
    host = client.host_from(getattr(args, "host", None))
    if host and args.command_name in QUEUE_COMMANDS:
        if args.command_name == "submit":
            print(
                client.submit_remote(
                    host,
                    name=args.name,
                    command=args.command,
                    context=args.context,
                    image=args.image,
                )
            )
            return 0
        # Everything else is the same command, run over there. Forwarding the
        # argv rather than reconstructing it keeps this from drifting as flags
        # are added.
        return client.remote(host, _without_host(argv))
    return _queue_here(args)


def _queue_here(args: argparse.Namespace) -> int:
    queue_dir = _queue_dir(args)
    name = args.command_name
    if name == "runner":
        return _runner(args, queue_dir)
    if name == "reserve":
        # Prints the directory to rsync into. The manifest is written by the
        # `commit` that follows, and until then the runner cannot see this.
        _, path = spool.reserve(queue_dir, args.name, client.local_user())
        print(path)
        return 0
    if name == "commit":
        print(_commit(args).job.job_id)
        return 0
    if name == "submit":
        entry = client.submit(
            queue_dir,
            name=args.name,
            command=args.command,
            context=None if args.image else args.context,
            image=args.image,
        )
        print(entry.job.job_id)
        return 0
    if name == "queue":
        entries = spool.entries(queue_dir) if args.all else spool.publishable(queue_dir)
        print(client.render(entries), end="")
        return 0
    if name == "retry":
        again = client.retry(queue_dir, client.resolve(queue_dir, args.job))
        print(again.job.job_id)
        return 0
    if name == "remove":
        print(f"removed {client.remove(queue_dir, args.job).job.job_id}")
        return 0
    entry = client.ask(queue_dir, args.job, name)
    print(f"asked the runner to {name} {entry.job.job_id}")
    return 0


def _commit(args: argparse.Namespace) -> spool.Entry:
    return spool.commit(
        args.path,
        spool.Job(
            job_id=args.path.name,
            name=args.name,
            user=args.user or client.local_user(),
            command=args.command,
            submitted_unix=time.time(),
            git_sha=args.git_sha,
            git_dirty=args.git_dirty,
            image=args.image,
        ),
    )


def _runner(args: argparse.Namespace, queue_dir: Path) -> int:
    """The queue container's entry point."""
    contract = box.load()
    shared_dir = args.shared_dir or (contract.shared_dir if contract else None)
    if shared_dir is None:
        raise box.NotProvisioned(_unprovisioned("runner", ["--shared-dir"]))
    url = args.url if args.url is not None else _runner_url(contract)
    textfile = args.textfile_dir or box.textfile_dir(contract)
    LOG.info("sparks: serving %s, publishing to %s", queue_dir, textfile)
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


def _queue_dir(args: argparse.Namespace) -> Path:
    given = getattr(args, "shared_dir", None)
    if given:
        return Path(given) / "queue"
    contract = box.load()
    if contract is None:
        raise box.NotProvisioned(_unprovisioned(args.command_name, ["--host"]))
    queue_dir = contract.queue_dir
    if args.command_name != "runner" and not queue_dir.is_dir():
        # Its existence is the box saying it runs a queue, and it is a fact on
        # disk rather than a claim in a file. sparks does not create it: sparkup
        # gives it the group and the setgid bit that let colleagues share it,
        # and one made here would have neither.
        raise box.NotProvisioned(
            f"this box is provisioned for sparks but not for the queue: "
            f"{queue_dir} does not exist. Converge sparkup (cd sparkup && "
            f"make apply) to add the queue service, or run the job directly "
            f"with sparks run"
        )
    return queue_dir


def _without_host(argv: list[str] | None) -> list[str]:
    """The same command, minus the flag that sent it over there.

    Left in, the box would try to ssh onwards to itself.
    """
    given = list(argv if argv is not None else sys.argv[1:])
    kept: list[str] = []
    skip = False
    for token in given:
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


def _run(args: argparse.Namespace, settings: _Settings) -> int:
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


if __name__ == "__main__":
    sys.exit(main())
