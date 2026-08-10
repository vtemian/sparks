import json
from pathlib import Path
from typing import Any

import pytest

from sparks import spool, summary
from sparks.fire import control

IMAGE = "spark.local:5000/demo:1"

ENERGY = summary.Energy(
    total_joules=36000.0,
    marginal_joules=18000.0,
    gpu_nvml_joules=543.0,
    gpu_firmware_joules=663.0,
    idle_watts=13.0,
    idle_gpu_watts=3.8,
    window_seconds=52.0,
    baseline_seconds=60.0,
    gpu_sources="agree",
)


def a_job(shared: Path, **state: Any) -> spool.Entry:
    entry = spool.submit(
        shared / "queue",
        name="e0",
        user="rex",
        command=["python", "train.py"],
        image=IMAGE,
    )
    spool.advance(entry.path, **state)
    return spool.load(entry.path)


def a_run(shared: Path, run_id: str, output: str = "", error: str = "") -> Path:
    directory = shared / "runs" / run_id
    directory.mkdir(parents=True)
    (directory / summary.OUTPUT_LOG).write_text(output)
    if error:
        (directory / summary.ERROR_FILE).write_text(error)
    return directory


def rendered_rows(text: str) -> dict[str, str]:
    split = (line.split(maxsplit=1) for line in text.splitlines())
    return {parts[0]: parts[1] if len(parts) > 1 else "" for parts in split}


def a_record(**over: Any) -> summary.Summary:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "run_name": "e0",
        "user": "rex",
        "git_sha": "abc1234",
        "command": ["python", "train.py"],
        "started_unix": 1785847319.0,
        "ended_unix": 1785847367.0,
        "duration_seconds": 48.0,
        "status": "finished",
        "exit_code": 0,
        "signal": None,
        "escalated_to_sigkill": False,
        "energy": ENERGY,
        "final_loss": 0.412,
    }
    fields.update(over)
    return summary.Summary(**fields)


def test_queue_dir_uses_shared_dir(tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    assert control.queue_dir(tmp_path) == queue


def test_runs_dir_sits_beside_the_queue(tmp_path: Path) -> None:
    assert control.runs_dir(tmp_path) == tmp_path / "runs"


def test_logs_are_what_the_run_printed(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.RUNNING, run_id="run-1")
    a_run(tmp_path, "run-1", output="step 1 loss 4.2\nstep 2 loss 3.9\n")
    assert control.logs(entry, tmp_path) == "step 1 loss 4.2\nstep 2 loss 3.9\n"


def test_logs_keep_only_the_last_tail_lines(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.RUNNING, run_id="run-1")
    a_run(tmp_path, "run-1", output="".join(f"step {n}\n" for n in range(500)))
    assert control.logs(entry, tmp_path, tail=3) == "step 497\nstep 498\nstep 499\n"


def test_logs_of_an_unstarted_job_say_why_there_is_nothing(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.FAILED, detail="pull failed: no such tag")
    with pytest.raises(control.ControlError, match="pull failed: no such tag"):
        control.logs(entry, tmp_path)


def test_logs_of_a_queued_job_point_at_status(tmp_path: Path) -> None:
    entry = a_job(tmp_path)
    with pytest.raises(control.ControlError, match="sparks status"):
        control.logs(entry, tmp_path)


def test_logs_label_a_launch_failure_rather_than_blurring_it(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.FAILED, run_id="run-1")
    a_run(tmp_path, "run-1", error="exec: python: not found\n")
    printed = control.logs(entry, tmp_path)
    assert "sparks could not run this job" in printed
    assert "exec: python: not found" in printed


def test_logs_refuse_when_the_run_wrote_nothing(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.RUNNING, run_id="run-1")
    a_run(tmp_path, "run-1")
    with pytest.raises(control.ControlError, match="no output yet"):
        control.logs(entry, tmp_path)


def test_status_carries_the_record_of_a_finished_run(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.FINISHED, run_id="run-1", exit_code=0)
    summary.save(a_record(), a_run(tmp_path, "run-1", output="done\n"))
    payload = control.status(entry, tmp_path)
    assert payload["summary"]["status"] == "finished"
    assert payload["summary"]["final_loss"] == 0.412
    assert payload["state"]["exit_code"] == 0


def test_status_has_no_record_before_the_run_starts(tmp_path: Path) -> None:
    payload = control.status(a_job(tmp_path), tmp_path)
    assert payload["summary"] is None
    assert payload["job"]["image"] == IMAGE


def test_status_survives_a_record_it_cannot_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    entry = a_job(tmp_path, state=spool.FINISHED, run_id="run-1")
    directory = a_run(tmp_path, "run-1", output="done\n")
    (directory / summary.FILENAME).write_text("{not json")
    assert control.status(entry, tmp_path)["summary"] is None
    assert "unreadable record" in caplog.text


def test_json_names_every_job_and_its_state(tmp_path: Path) -> None:
    a_job(tmp_path, state=spool.RUNNING, run_id="run-1")
    a_job(tmp_path)
    rows = json.loads(control.as_json(spool.entries(tmp_path / "queue")))
    assert [row["state"] for row in rows] == [spool.RUNNING, spool.QUEUED]
    assert rows[0]["run_id"] == "run-1"
    assert rows[1]["run_id"] is None
    assert rows[0]["command"] == ["python", "train.py"]


def test_a_zero_exit_code_is_shown_rather_than_treated_as_absent(
    tmp_path: Path,
) -> None:
    entry = a_job(tmp_path, state=spool.FINISHED, run_id="run-1", exit_code=0)
    summary.save(a_record(), a_run(tmp_path, "run-1"))
    rows = rendered_rows(control.render_status(control.status(entry, tmp_path)))
    assert rows["exit"] == "0"


def test_status_reports_energy_in_watt_hours(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.FINISHED, run_id="run-1")
    summary.save(a_record(), a_run(tmp_path, "run-1"))
    rows = rendered_rows(control.render_status(control.status(entry, tmp_path)))
    assert rows["energy"] == "10 Wh"
    assert rows["command"] == "python train.py"


def test_status_leaves_out_what_a_run_never_measured(tmp_path: Path) -> None:
    entry = a_job(tmp_path, state=spool.FAILED, run_id="run-1")
    record = a_record(status="crashed", final_loss=None)
    summary.save(record, a_run(tmp_path, "run-1"))
    rows = rendered_rows(control.render_status(control.status(entry, tmp_path)))
    assert "loss" not in rows
    assert "signal" not in rows
    assert rows["status"] == "crashed"
