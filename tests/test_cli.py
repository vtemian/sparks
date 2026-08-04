import pytest

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
