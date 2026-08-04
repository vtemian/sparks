"""The launcher, wired end to end against real child processes.

No mocks: the thing under test is whether four modules agree about how a run
ended, and a fake would only confirm our own assumptions.
"""

import json
from pathlib import Path

from sparks.launcher import launch


def read_summary(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "summary.json").read_text())  # type: ignore[no-any-return]


def test_a_clean_run_is_recorded_as_finished(tmp_path: Path) -> None:
    result = launch(["true"], name="ok", shared_dir=tmp_path, url=None)
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert s["status"] == "finished"
    assert s["exit_code"] == 0
    assert s["signal"] is None
    assert result.wrapper_exit == 0


def test_a_failing_run_is_recorded_as_crashed(tmp_path: Path) -> None:
    result = launch(["sh", "-c", "exit 3"], name="bad", shared_dir=tmp_path, url=None)
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert s["status"] == "crashed"
    assert s["exit_code"] == 3
    assert result.wrapper_exit == 3


def test_the_child_inherits_the_run_id_and_url(tmp_path: Path) -> None:
    # This is the contract that lets the child emit its own metrics.
    result = launch(
        ["sh", "-c", "echo $SPARKS_RUN_ID; echo $SPARKS_PROMETHEUS_URL"],
        name="env",
        shared_dir=tmp_path,
        url="http://127.0.0.1:9090",
    )
    log = (tmp_path / "runs" / result.run_id / "output.log").read_text()
    assert result.run_id in log
    assert "http://127.0.0.1:9090" in log


def test_output_lands_in_the_log_file(tmp_path: Path) -> None:
    result = launch(
        ["sh", "-c", "echo hello"], name="out", shared_dir=tmp_path, url=None
    )
    assert "hello" in (tmp_path / "runs" / result.run_id / "output.log").read_text()


def test_the_run_index_is_rebuilt_from_the_summaries(tmp_path: Path) -> None:
    launch(["true"], name="a", shared_dir=tmp_path, url=None)
    launch(["sh", "-c", "exit 1"], name="b", shared_dir=tmp_path, url=None)
    index = (tmp_path / "index" / "sparks_runs.prom").read_text()
    assert index.count("sparks_run_info{") == 2
    assert 'status="finished"' in index
    assert 'status="crashed"' in index
    assert index.endswith("\n")


def test_the_index_file_is_world_readable(tmp_path: Path) -> None:
    # mkstemp creates 0600 and node_exporter drops privileges, so a 0600 file
    # is skipped with no error anybody notices.
    launch(["true"], name="perm", shared_dir=tmp_path, url=None)
    mode = (tmp_path / "index" / "sparks_runs.prom").stat().st_mode & 0o777
    assert mode == 0o644


def test_energy_is_recorded_even_with_no_sensors(tmp_path: Path) -> None:
    # Development happens on macOS, where there is no hwmon and no NVML. A
    # missing sensor is a degraded reading, never a failed run.
    result = launch(["true"], name="nrg", shared_dir=tmp_path, url=None)
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert "energy" in s
    assert isinstance(s["energy"], dict)


def test_duration_comes_from_the_monotonic_clock(tmp_path: Path) -> None:
    result = launch(
        ["sh", "-c", "sleep 0.3"], name="dur", shared_dir=tmp_path, url=None
    )
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert isinstance(s["duration_seconds"], float)
    assert 0.2 < s["duration_seconds"] < 5.0
