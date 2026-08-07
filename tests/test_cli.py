"""The laptop client surface: queue verbs only, no run/runner/demo."""

import os
from pathlib import Path
from unittest.mock import patch

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


def test_queue_verbs_require_host(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    code = cli.main(["queue"])
    assert code == os.EX_CONFIG
    assert client.HOST_ENV in capsys.readouterr().err


def test_queue_sshes_fire_ctl_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["queue", "--host", "box", "--all"]) == 0
    run_fn.assert_called_once_with("box", ["queue", "--all"])


def test_cancel_sshes_fire_ctl_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["cancel", "job-1"]) == 0
    run_fn.assert_called_once_with("box", ["cancel", "job-1"])


def test_submit_requires_host_and_never_runs_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    code = cli.main(["submit", "--data", str(data), "--", "true"])
    assert code == os.EX_CONFIG
    assert client.HOST_ENV in capsys.readouterr().err
