from pathlib import Path

import pytest

from sparks import cli
from sparks.cli import build_parser, deep_link


def test_demo_takes_a_name_and_a_seed() -> None:
    args = build_parser().parse_args(["demo", "--name", "e0", "--seed", "3"])
    assert (args.name, args.seed) == ("e0", 3)


def test_the_prometheus_url_has_a_working_default() -> None:
    assert build_parser().parse_args(["demo"]).url == "http://127.0.0.1:9090"


def test_the_deep_link_starts_a_minute_early() -> None:
    # Otherwise the first datapoints are glued to the left edge of the graph.
    link = deep_link("http://spark.local", "run-1", started=1_754_300_000.0)
    assert "var-run_id=run-1" in link
    assert "from=1754299940000" in link
    assert link.endswith("&to=now&refresh=10s")


def test_the_deep_link_tolerates_a_trailing_slash() -> None:
    link = deep_link("http://spark.local/", "run-1", started=1_754_300_000.0)
    assert "spark.local/d/training-runs" in link


def test_an_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["nope"])
    assert e.value.code == 2


def test_run_takes_a_name_and_a_command_after_the_separator() -> None:
    args = build_parser().parse_args(["run", "--name", "e0", "--", "python", "t.py"])
    assert args.name == "e0"
    assert args.command == ["python", "t.py"]


def test_run_refuses_an_empty_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--name", "e0", "--"])


def test_the_shared_dir_defaults_to_sparkups_default() -> None:
    # /srv/spark is sparkup's repo default. A box that overrode spark_shared_dir
    # in host_vars passes --shared-dir, and no one project's name is baked in.
    assert build_parser().parse_args(["run", "--", "true"]).shared_dir == "/srv/spark"


def test_a_crashed_child_makes_the_cli_exit_nonzero(tmp_path: Path) -> None:
    # A queue or a shell && reads $?. Returning 0 for a crashed run is the
    # wrapper lying to its caller, and the whole suite passed with it because
    # nothing ever called main() for a `run`.
    code = cli.main(
        [
            "--url",
            "",
            "run",
            "--shared-dir",
            str(tmp_path),
            "--baseline-seconds",
            "0",
            "--",
            "sh",
            "-c",
            "exit 3",
        ]
    )
    assert code == 3
