import json
import os
import subprocess
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


def test_queue_json_travels_to_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["queue", "--json"]) == 0
    run_fn.assert_called_once_with("box", ["queue", "--json"])


def test_logs_send_no_tail_of_their_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["logs", "job-1"]) == 0
    run_fn.assert_called_once_with("box", ["logs", "job-1"])


def test_logs_pass_a_tail_the_user_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["logs", "job-1", "--tail", "20"]) == 0
    run_fn.assert_called_once_with("box", ["logs", "job-1", "--tail", "20"])


def test_logs_all_asks_for_every_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["logs", "job-1", "--all"]) == 0
    run_fn.assert_called_once_with("box", ["logs", "job-1", "--all"])


def test_status_json_travels_to_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch.object(client, "run", return_value=0) as run_fn:
        assert cli.main(["status", "job-1", "--json"]) == 0
    run_fn.assert_called_once_with("box", ["status", "job-1", "--json"])


def a_status(state: str, run_id: str | None = None, detail: str | None = None) -> str:
    return json.dumps(
        {
            "job": {"job_id": "job-1"},
            "state": {"state": state, "run_id": run_id, "detail": detail},
            "summary": None,
        }
    )


def ssh_answers(*payloads: str) -> list[subprocess.CompletedProcess[str]]:
    return [
        subprocess.CompletedProcess([], 0, stdout=text, stderr="") for text in payloads
    ]


def test_wait_polls_until_the_job_reaches_a_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    answers = ssh_answers(
        a_status("queued"), a_status("running", "run-1"), a_status("finished", "run-1")
    )
    with patch("subprocess.run", side_effect=answers) as ssh:
        assert client.wait("box", "job-1", interval=0.0) == "finished"
    assert ssh.call_count == 3


def test_wait_says_each_state_once_as_it_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    answers = ssh_answers(
        a_status("running", "run-1"),
        a_status("running", "run-1"),
        a_status("failed", "run-1", detail="pull failed: no such tag"),
    )
    with patch("subprocess.run", side_effect=answers):
        client.wait("box", "job-1", interval=0.0)
    said = capsys.readouterr().err.splitlines()
    assert said == ["running run-1", "failed run-1: pull failed: no such tag"]


def test_wait_gives_up_on_a_timeout_rather_than_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with (
        patch("subprocess.run", side_effect=ssh_answers(a_status("running", "run-1"))),
        pytest.raises(client.TimedOutError, match="still running after 0s"),
    ):
        client.wait("box", "job-1", interval=0.0, timeout=0.0)


def test_wait_exits_zero_only_when_the_job_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch("subprocess.run", side_effect=ssh_answers(a_status("finished", "r"))):
        assert cli.main(["wait", "job-1", "--interval", "0"]) == 0


def test_wait_exits_one_when_the_job_did_not_finish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch("subprocess.run", side_effect=ssh_answers(a_status("aborted", "r"))):
        assert cli.main(["wait", "job-1", "--interval", "0"]) == 1
    assert capsys.readouterr().out.strip() == "aborted"


def test_a_timeout_is_not_reported_as_a_failed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch("subprocess.run", side_effect=ssh_answers(a_status("running", "r"))):
        code = cli.main(["wait", "job-1", "--interval", "0", "--timeout", "0"])
    assert code == os.EX_TEMPFAIL


def test_ctrl_c_leaves_the_job_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with patch("subprocess.run", side_effect=KeyboardInterrupt):
        assert cli.main(["wait", "job-1"]) == 130


def test_a_status_that_is_not_json_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(client.HOST_ENV, "box")
    with (
        patch("subprocess.run", side_effect=ssh_answers("not json at all")),
        pytest.raises(client.ClientError, match="did not answer with a status"),
    ):
        client.wait("box", "job-1", interval=0.0)
