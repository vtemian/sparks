from pathlib import Path

import pytest

from sparks import cli
from sparks.cli import build_parser, deep_link


def test_no_url_is_left_unset_rather_than_guessed() -> None:
    # It used to default to http://127.0.0.1:9090, which is a guess about the
    # box. The provisioned value comes from /etc/sparks/box.toml now, and unset
    # has to stay distinguishable from `--url ""`, which means "no telemetry".
    assert build_parser().parse_args(["run", "--", "true"]).url is None
    assert build_parser().parse_args(["--url", "", "run", "--", "true"]).url == ""


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


def test_the_shared_dir_is_not_guessed_either() -> None:
    # It used to default to /srv/spark, sparkup's repo default, which is not
    # this box's: host_vars overrides it. A wrong default here records runs into
    # a directory nobody reads.
    assert build_parser().parse_args(["run", "--", "true"]).shared_dir is None


def test_an_unprovisioned_box_refuses_to_run_a_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # conftest.py points SPARKS_BOX_CONFIG at a file that does not exist, so
    # every test sees an unprovisioned box unless it says otherwise.
    code = cli.main(["run", "--", "true"])
    assert code == cli.EX_CONFIG
    err = capsys.readouterr().err
    assert "not configured for sparks" in err
    # Names the missing file and the way out of it, both: "misconfigured" with
    # no path to fix is a dead end for whoever hits this.
    assert "no-such-box.toml" in err
    assert "sparkup" in err
    assert "--shared-dir" in err


def test_explicit_paths_run_on_a_box_sparkup_does_not_manage(tmp_path: Path) -> None:
    # A laptop, or someone else's cluster. Saying where everything is must stay
    # possible, or sparks is unusable anywhere but one machine.
    code = cli.main(
        # --baseline-seconds 0: the default samples idle power for a minute.
        ["--url", "", "run", "--shared-dir", str(tmp_path), "-b", "0", "--", "true"],
    )
    assert code == 0


def test_the_contract_supplies_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "srv"
    (shared / "runs").mkdir(parents=True)
    (tmp_path / "index").mkdir()
    contract = tmp_path / "box.toml"
    contract.write_text(
        f'shared_dir = "{shared}"\nshared_group = "spark"\n'
        f'textfile_dir = "{tmp_path / "index"}"\n'
        'prometheus_url = ""\ngrafana_url = "http://elsewhere.local"\n'
        'registry_url = "http://spark.local:5000"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(contract))
    code = cli.main(["run", "--name", "contract", "-b", "0", "--", "true"])
    assert code == 0
    # The run landed in the tree the box named, with no flags passed at all.
    assert list((shared / "runs").iterdir())


def test_a_contract_that_no_longer_matches_the_box_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Provisioning half-finished, or someone moved the shared tree. Different
    # advice from an absent contract, so it must not read as "not provisioned".
    contract = tmp_path / "box.toml"
    contract.write_text(
        f'shared_dir = "{tmp_path / "gone"}"\nshared_group = "spark"\n'
        f'textfile_dir = "{tmp_path / "index"}"\n'
        'prometheus_url = ""\ngrafana_url = ""\n'
        'registry_url = "http://spark.local:5000"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(contract))
    code = cli.main(["run", "--", "true"])
    assert code == cli.EX_CONFIG
    err = capsys.readouterr().err
    assert "does not match" in err
    assert str(tmp_path / "gone" / "runs") in err


def test_a_hand_edited_contract_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = tmp_path / "box.toml"
    contract.write_text("this is not toml\n")
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(contract))
    assert cli.main(["run", "--", "true"]) == cli.EX_CONFIG
    assert "not valid TOML" in capsys.readouterr().err


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
