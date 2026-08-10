import json
from pathlib import Path

import pytest

from sparks import spool, summary
from sparks.fire import cli

IMAGE = "spark.local:5000/demo:1"


def a_job(shared: Path, **state: object) -> spool.Entry:
    entry = spool.submit(
        shared / "queue",
        name="e0",
        user="rex",
        command=["python", "train.py"],
        image=IMAGE,
    )
    spool.advance(entry.path, **state)
    return spool.load(entry.path)


def test_queue_lists_empty_shared_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["queue", "--shared-dir", str(tmp_path)]) == 0
    assert "empty" in capsys.readouterr().out


def test_reserve_prints_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["reserve", "--name", "e0", "--shared-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(tmp_path / "queue"))


def test_queue_json_is_a_list_even_when_the_queue_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["queue", "--json", "--shared-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_queue_json_carries_the_job_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = a_job(tmp_path)
    assert cli.main(["queue", "--json", "--shared-dir", str(tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["job_id"] for row in rows] == [entry.job.job_id]


def test_logs_print_what_the_run_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = a_job(tmp_path, state=spool.RUNNING, run_id="run-1")
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / summary.OUTPUT_LOG).write_text("step 1 loss 4.2\n")
    code = cli.main(
        ["logs", entry.job.job_id, "--shared-dir", str(tmp_path)],
    )
    assert code == 0
    assert capsys.readouterr().out == "step 1 loss 4.2\n"


def test_logs_of_an_unknown_job_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_job(tmp_path)
    assert cli.main(["logs", "nope", "--shared-dir", str(tmp_path)]) == 1
    assert "no job matches" in capsys.readouterr().err


def test_status_json_carries_state_and_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = a_job(tmp_path, state=spool.QUEUED)
    code = cli.main(
        ["status", entry.job.job_id, "--json", "--shared-dir", str(tmp_path)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"]["state"] == spool.QUEUED
    assert payload["summary"] is None
    assert payload["job"]["command"] == ["python", "train.py"]


def test_status_without_json_is_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = a_job(tmp_path)
    assert cli.main(["status", entry.job.job_id, "--shared-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert entry.job.job_id in out
    assert IMAGE in out
