"""The filesystem primitives that let two accounts share one box safely."""

import stat
from pathlib import Path

import pytest

from sparks import shared


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- clean -------------------------------------------------------------------


def test_clean_turns_a_lone_surrogate_into_the_replacement_char() -> None:
    # argv arrives surrogate-escaped: --name $'bad\xffname' is a lone surrogate
    # that json.dumps escapes silently and every later f.write raises on.
    poisoned = b"e0\xff".decode("utf-8", "surrogateescape")
    cleaned = shared.clean(poisoned)
    assert cleaned == "e0\ufffd"
    # And it survives a real UTF-8 round trip, which the freeze bug proves the
    # raw value does not.
    cleaned.encode("utf-8")


def test_clean_leaves_genuine_utf8_alone() -> None:
    assert shared.clean("café — 日本語 🎉") == "café — 日本語 🎉"


def test_clean_truncates_to_the_limit() -> None:
    assert shared.clean("x" * 1000) == "x" * shared.MAX_TEXT


def test_clean_falls_back_when_empty() -> None:
    assert shared.clean("", "unknown") == "unknown"
    assert shared.clean("", "") == ""


# -- make_dir ----------------------------------------------------------------


def test_make_dir_creates_nested_dirs_at_2775(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "run-1"
    shared.make_dir(target)
    assert target.is_dir()
    assert mode_of(target) == shared.DIR_MODE
    assert mode_of(tmp_path / "runs") == shared.DIR_MODE


def test_make_dir_heals_a_tree_left_group_unreadable(tmp_path: Path) -> None:
    # A dir left 0700 by an earlier umask blinds the other user's load_all; the
    # chmod is attempted even when the dir already existed, so it heals.
    d = tmp_path / "runs"
    d.mkdir(mode=0o700)
    shared.make_dir(d)
    assert mode_of(d) == shared.DIR_MODE


# -- reserve_dir -----------------------------------------------------------


def test_reserve_dir_creates_a_2775_directory(tmp_path: Path) -> None:
    run_id, run_dir = shared.reserve_dir(tmp_path / "runs", "e0", "alice")
    assert run_dir.is_dir()
    assert run_dir.name == run_id
    assert mode_of(run_dir) == shared.DIR_MODE


def test_reserve_dir_disambiguates_a_same_second_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Freeze the clock so both reservations produce the same base id; the
    # atomic mkdir is what forces two distinct directories.
    monkeypatch.setattr("sparks.run.time.time", lambda: 1754300000.0)
    runs = tmp_path / "runs"
    first_id, first_dir = shared.reserve_dir(runs, "e0", "alice")
    second_id, second_dir = shared.reserve_dir(runs, "e0", "alice")
    assert first_id != second_id
    assert first_dir.is_dir() and second_dir.is_dir()
    assert second_id.endswith("-2")


# -- exclusive ---------------------------------------------------------------


def test_exclusive_releases_on_exit_so_the_next_holder_gets_it(
    tmp_path: Path,
) -> None:
    with shared.exclusive(tmp_path):
        pass
    # If the fd had leaked its lock, this would time out.
    with shared.exclusive(tmp_path, timeout=1.0):
        pass


def _acquire_briefly(directory: Path) -> None:
    with shared.exclusive(directory, timeout=0.2):
        pass


def test_exclusive_times_out_when_already_held(tmp_path: Path) -> None:
    # flock conflicts across two open descriptions even in one process, so a
    # second acquire exercises the contention path without a second process.
    with shared.exclusive(tmp_path), pytest.raises(TimeoutError):
        _acquire_briefly(tmp_path)
