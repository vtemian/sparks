"""The launcher, wired end to end against real child processes.

No mocks: the thing under test is whether four modules agree about how a run
ended, and a fake would only confirm our own assumptions.
"""

import json
import signal
from pathlib import Path

import pytest

from sparks import summary
from sparks.energy import Sampler
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
    # macOS has no hwmon and no NVML. A missing sensor is a degraded reading,
    # never a failed run. Asserting the key exists proves nothing: `energy` is
    # a required frozen field, so asdict always produces it. Assert the values.
    result = launch(["true"], name="nrg", shared_dir=tmp_path, url=None)
    e = read_summary(tmp_path / "runs" / result.run_id)["energy"]
    assert isinstance(e, dict)
    assert e["total_joules"] == 0.0
    assert e["idle_watts"] == 0.0
    # No baseline was measured, so marginal is unknown (None), never 0.0, and
    # the sources are unmeasured rather than the old hard-coded "agree".
    assert e["marginal_joules"] is None
    assert e["gpu_sources"] == "unmeasured"


def test_energy_deltas_land_in_the_right_fields_with_an_injected_sampler(
    tmp_path: Path,
) -> None:
    # On a dev machine Sampler.detect() reads zeros and every energy assertion
    # is 0.0 == 0.0, which let three mutations survive: swapping the two GPU
    # sources, recording the raw counter instead of the delta, and not sampling
    # at all. The fake sysfs tree is real files and the child advances the
    # counters, exactly as happens on the box. Three distinct deltas make any
    # value in the wrong field visible.
    chip = tmp_path / "hwmon0"
    chip.mkdir()
    (chip / "name").write_text("spbm\n")
    (chip / "energy1_label").write_text("pkg\n")
    (chip / "energy1_input").write_text("1000000000\n")  # 1000.000 J
    (chip / "energy4_label").write_text("gpu\n")
    (chip / "energy4_input").write_text("500000000\n")  # 500.000 J
    nvml_file = tmp_path / "nvml_mj"
    nvml_file.write_text("200000\n")  # 200.000 J, reported in mJ

    sampler = Sampler(nvml=lambda: float(nvml_file.read_text()), hwmon=chip)
    child = [
        "sh",
        "-c",
        f"echo 1000009000 > {chip / 'energy1_input'}; "  # +0.009 J total
        f"echo 500003000 > {chip / 'energy4_input'}; "  # +0.003 J firmware
        f"echo 205000 > {nvml_file}",  # +5.000 J nvml
    ]
    result = launch(
        child,
        name="nrg",
        shared_dir=tmp_path,
        url=None,
        baseline_seconds=0.0,
        sampler=sampler,
    )
    e = read_summary(tmp_path / "runs" / result.run_id)["energy"]
    assert isinstance(e, dict)
    assert e["total_joules"] == pytest.approx(0.009)
    assert e["gpu_firmware_joules"] == pytest.approx(0.003)
    assert e["gpu_nvml_joules"] == pytest.approx(5.0)
    assert e["baseline_seconds"] == 0.0
    assert float(e["window_seconds"]) >= 0.0


def test_duration_is_the_monotonic_delta_not_the_wall_clock_pair(
    tmp_path: Path,
) -> None:
    # Asserting a plausible range passes just as well if duration were
    # `ended_unix - started_unix`. The monotonic property is only observable
    # when the two disagree, so assert they are independently derived.
    result = launch(
        ["sh", "-c", "sleep 0.3"], name="dur", shared_dir=tmp_path, url=None
    )
    s = read_summary(tmp_path / "runs" / result.run_id)
    wall = float(s["ended_unix"]) - float(s["started_unix"])  # type: ignore[arg-type]
    assert 0.2 < float(s["duration_seconds"]) < 5.0  # type: ignore[arg-type]
    # Same run, two clocks: close but not the same float. An implementation
    # that copied the wall delta would make these bit-identical.
    assert float(s["duration_seconds"]) != wall  # type: ignore[arg-type]


def test_a_cancelled_run_is_not_recorded_as_finished(tmp_path: Path) -> None:
    # The row the plan calls "the one that matters". The child traps SIGTERM,
    # checkpoints and exits 0 under its own power; the wrapper was the thing
    # signalled, so the run did NOT run to completion. Recording this as
    # `finished` is the dashboard lying about how runs end.
    from sparks.process import classify

    outcome = classify(returncode=0, interrupted_by=signal.SIGTERM)
    assert outcome.status == "cancelled"
    assert outcome.exit_code == 0
    assert outcome.signal_name == "SIGTERM"
    # Both fields set at once: an earlier draft wrongly called them exclusive.
    record = summary.Summary(
        run_id="r",
        run_name="n",
        user="u",
        git_sha="s",
        command=["true"],
        started_unix=1.0,
        ended_unix=2.0,
        duration_seconds=1.0,
        status=outcome.status,
        exit_code=outcome.exit_code,
        signal=outcome.signal_name,
        escalated_to_sigkill=False,
        energy=summary.Energy(
            total_joules=0.0,
            marginal_joules=None,
            gpu_nvml_joules=0.0,
            gpu_firmware_joules=0.0,
            idle_watts=0.0,
            idle_gpu_watts=0.0,
            window_seconds=0.0,
            baseline_seconds=0.0,
            gpu_sources="unmeasured",
        ),
    )
    summary.save(record, tmp_path)
    back = json.loads((tmp_path / "summary.json").read_text())
    assert back["status"] == "cancelled"
    assert back["exit_code"] == 0 and back["signal"] == "SIGTERM"


def test_a_command_that_does_not_exist_still_leaves_a_record(
    tmp_path: Path,
) -> None:
    # Without this the run has no summary.json and no index row, while a
    # LIFECYCLE-exempt training_run_info has already been pushed: a permanent
    # phantom run that never ends and never gets a status.
    result = launch(
        ["definitely-not-a-command"], name="typo", shared_dir=tmp_path, url=None
    )
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert s["status"] == "crashed"
    assert result.wrapper_exit != 0


def test_a_non_utf8_name_neither_poisons_the_index_nor_crashes(
    tmp_path: Path,
) -> None:
    # argv is surrogate-escaped; a lone surrogate persisted raw makes every
    # later rebuild raise UnicodeEncodeError and freezes the shared index.
    poisoned = b"e0\xff".decode("utf-8", "surrogateescape")
    result = launch(["true"], name=poisoned, shared_dir=tmp_path, url=None)
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert s["run_name"] == "e0\ufffd"
    # The whole record round-trips through UTF-8, and the rebuilt index does too.
    index_text = (tmp_path / "index" / "sparks_runs.prom").read_text(encoding="utf-8")
    assert result.run_id in index_text
    assert index_text.endswith("\n")


def test_a_non_utf8_command_does_not_crash_the_failed_launch_path(
    tmp_path: Path,
) -> None:
    # FileNotFoundError's message carries the bad byte straight from argv, so
    # error.txt's write_text used to raise inside launch()'s except and escape.
    bad = b"./nonexistent-\xff".decode("utf-8", "surrogateescape")
    result = launch([bad], name="typo", shared_dir=tmp_path, url=None)
    assert result.status == "crashed"
    s = read_summary(tmp_path / "runs" / result.run_id)
    assert s["status"] == "crashed"
    # error.txt was written and is valid UTF-8.
    (tmp_path / "runs" / result.run_id / "error.txt").read_text(encoding="utf-8")


def test_the_record_is_not_lost_when_saving_the_summary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child ran to completion; a PermissionError while saving must still
    # call metrics.end(status) and must not lie about the child's exit code,
    # or a LIFECYCLE-exempt training_run_info sits on the dashboard forever.
    import sparks.launcher as launcher_mod

    ended: list[str] = []

    class FakeMetrics:
        def begin(self) -> None: ...
        def end(self, status: str) -> None:
            ended.append(status)

    monkeypatch.setattr(
        launcher_mod, "_supervisor_metrics", lambda *a, **k: FakeMetrics()
    )

    def boom(record: object, run_dir: Path) -> Path:
        raise PermissionError("disk full / not yours")

    monkeypatch.setattr(summary, "save", boom)

    result = launch(
        ["sh", "-c", "exit 3"], name="save-fail", shared_dir=tmp_path, url="x"
    )
    # The status is faithful and the exit code is the child's, not the saver's.
    assert result.status == "crashed"
    assert result.wrapper_exit == 3
    # metrics.end was called with the real status despite the save blowing up.
    assert ended == ["crashed"]
