import json
import stat
from pathlib import Path
from typing import Any

import pytest

from sparks.summary import (
    FILENAME,
    STATUSES,
    Energy,
    Summary,
    load,
    save,
    write_atomically,
)

ENERGY = Energy(
    total_joules=1810.0,
    marginal_joules=1186.0,
    gpu_nvml_joules=543.0,
    gpu_firmware_joules=663.0,
    idle_watts=13.0,
    sources_agree=True,
)


def a_summary(**over: Any) -> Summary:
    fields: dict[str, Any] = {
        "run_id": "run-20260805-1420-e0",
        "run_name": "e0",
        "user": "vlad",
        "git_sha": "abc1234",
        "command": ["python", "train.py"],
        "started_unix": 1785847319.0,
        "ended_unix": 1785847367.0,
        "duration_seconds": 48.02,
        "status": "finished",
        "exit_code": 0,
        "signal": None,
        "escalated_to_sigkill": False,
        "energy": ENERGY,
        "final_loss": 0.412,
    }
    fields.update(over)
    return Summary(**fields)


def test_a_summary_round_trips_through_save_and_load(tmp_path: Path) -> None:
    original = a_summary()
    path = save(original, tmp_path / "run-20260805-1420-e0")
    assert path.name == FILENAME
    assert load(path) == original


def test_save_creates_the_run_directory(tmp_path: Path) -> None:
    path = save(a_summary(), tmp_path / "runs" / "run-20260805-1420-e0")
    assert path.is_file()


def test_the_summary_is_world_readable(tmp_path: Path) -> None:
    # It is written into a shared directory and read back by whoever rebuilds
    # the index, which is not necessarily the user who wrote it.
    path = save(a_summary(), tmp_path / "run-20260805-1420-e0")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_a_run_without_a_final_loss_omits_the_key_entirely(tmp_path: Path) -> None:
    # Absent, never null: a null reads back as 0.0 in half the things that would
    # consume it, and "no loss recorded" is not a loss of zero.
    path = save(a_summary(final_loss=None), tmp_path / "r")
    data = json.loads(path.read_text())
    assert "final_loss" not in data
    assert load(path).final_loss is None


def test_a_genuine_exit_137_is_distinguishable_from_a_sigkill() -> None:
    # Both give the caller $? = 137. Only these three orthogonal fields tell
    # them apart, which is the whole reason they are not collapsed into one int.
    genuine = a_summary(status="crashed", exit_code=137, signal=None)
    signalled = a_summary(status="killed", exit_code=None, signal="SIGKILL")
    assert genuine != signalled
    assert (genuine.exit_code, genuine.signal) == (137, None)
    assert (signalled.exit_code, signalled.signal) == (None, "SIGKILL")


def test_a_cancelled_run_carries_both_an_exit_code_and_a_signal() -> None:
    # The child trapped SIGTERM, checkpointed and exited 0; the wrapper was the
    # one that was signalled. Both facts are true and both are recorded, so the
    # fields must not be treated as mutually exclusive.
    cancelled = a_summary(status="cancelled", exit_code=0, signal="SIGTERM")
    assert cancelled.exit_code == 0
    assert cancelled.signal == "SIGTERM"


def test_duration_disagreeing_with_the_wall_clock_pair_survives_a_round_trip(
    tmp_path: Path,
) -> None:
    # duration_seconds is a time.monotonic() delta and the pair are time.time():
    # they disagree whenever NTP steps the clock mid-run, and the monotonic one
    # is the correct duration. Nothing may "reconcile" them.
    stepped = a_summary(
        started_unix=1785847319.0, ended_unix=1785847919.0, duration_seconds=48.02
    )
    path = save(stepped, tmp_path / "r")
    assert load(path).duration_seconds == 48.02


def test_the_status_vocabulary_is_the_five_terminal_states() -> None:
    assert set(STATUSES) == {"finished", "crashed", "cancelled", "killed", "oom"}
    assert a_summary(status="oom").is_terminal
    assert not a_summary(status="running").is_terminal


def test_write_atomically_leaves_no_temp_file_when_rendering_raises(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sparks_runs.prom"
    target.write_text("the old index\n")

    def boom() -> str:
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        write_atomically(target, boom)

    assert target.read_text() == "the old index\n"
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomically_replaces_rather_than_truncating(tmp_path: Path) -> None:
    target = tmp_path / "sparks_runs.prom"
    target.write_text("old\n")
    write_atomically(target, lambda: "new\n")
    assert target.read_text() == "new\n"
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomically_overrides_the_0600_mkstemp_gives(tmp_path: Path) -> None:
    # This is the trap: renaming a 0600 temp file into place produces exactly
    # the unreadable file node_exporter skips in silence.
    target = tmp_path / "sparks_runs.prom"
    write_atomically(target, lambda: "x\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
