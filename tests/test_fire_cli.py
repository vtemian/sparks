from pathlib import Path

import pytest

from sparks.fire.cli import build_parser


def test_runner_takes_flags_without_a_subcommand() -> None:
    args = build_parser().parse_args(
        [
            "--url",
            "http://host.docker.internal:9090",
            "--shared-dir",
            "/srv/spark",
            "--textfile-dir",
            "/var/lib/node_exporter",
            "--poll-seconds",
            "2",
        ]
    )
    assert args.url == "http://host.docker.internal:9090"
    assert args.shared_dir == Path("/srv/spark")
    assert args.textfile_dir == Path("/var/lib/node_exporter")
    assert args.poll_seconds == 2.0


def test_daemon_flags_still_parse_without_subcommand() -> None:
    args = build_parser().parse_args(
        ["--shared-dir", "/srv/spark", "--poll-seconds", "2"]
    )
    assert args.shared_dir == Path("/srv/spark")
    assert args.poll_seconds == 2.0


def test_runner_rejects_a_leading_runner_token() -> None:
    # Compose used to pass `runner` as a sparks subcommand; fire must not
    # require that token, and a stray one must not be silently accepted as
    # a positional that argparse then misreads.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["runner", "--shared-dir", "/srv/spark"])
