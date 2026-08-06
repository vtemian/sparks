"""The laptop client surface: queue verbs only, no run/runner/demo."""

from pathlib import Path

import pytest

from sparks.client import cli
from sparks.client import remote as client
from sparks.client.cli import build_parser


def test_client_cli_has_no_run() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--", "true"])


def test_client_cli_has_no_runner() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["runner"])


def test_client_cli_has_no_demo() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["demo"])


def test_an_unknown_command_exits_two() -> None:
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["nope"])
    assert e.value.code == 2


def test_submit_parses_data_and_command() -> None:
    args = build_parser().parse_args(
        ["submit", "--data", "/tmp/d", "--name", "e0", "--", "python", "t.py"]
    )
    assert args.name == "e0"
    assert args.data == Path("/tmp/d")
    assert args.command == ["python", "t.py"]


def test_queue_verbs_require_host_when_unprovisioned(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    code = cli.main(["queue"])
    assert code == 1
    assert client.HOST_ENV in capsys.readouterr().err


def test_queue_works_locally_with_shared_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    queue = tmp_path / "queue"
    queue.mkdir()
    code = cli.main(["queue", "--shared-dir", str(tmp_path)])
    assert code == 0
    assert "empty" in capsys.readouterr().out


def test_hidden_contract_prints_registry_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract = tmp_path / "box.toml"
    contract.write_text(
        f'shared_dir = "{tmp_path / "srv"}"\nshared_group = "spark"\n'
        f'textfile_dir = "{tmp_path / "index"}"\n'
        'prometheus_url = ""\ngrafana_url = ""\n'
        'registry_url = "http://spark.local:5000"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(contract))
    assert cli.main(["contract"]) == 0
    out = capsys.readouterr().out
    assert "registry_url = http://spark.local:5000" in out
    assert "shared_dir =" in out


def test_hidden_verbs_still_parse() -> None:
    # help=SUPPRESS hides the prose; they remain in the usage choices list.
    args = build_parser().parse_args(["reserve", "--name", "x"])
    assert args.name == "x"
    args = build_parser().parse_args(
        ["commit", "/tmp/j", "--image", "x:1", "--", "true"]
    )
    assert args.image == "x:1"
    assert build_parser().parse_args(["contract"]).command_name == "contract"
