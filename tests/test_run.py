import re
import subprocess
from pathlib import Path

from sparks.run import current_user, git_sha, new_run_id


def test_run_ids_sort_chronologically_as_strings() -> None:
    early = new_run_id("e0", "alice", when=1754300000.0)
    late = new_run_id("e0", "alice", when=1754390000.0)
    assert early < late


def test_the_shape_is_stable() -> None:
    rid = new_run_id("e0", "alice", when=1754300000.0)
    assert re.match(r"^run-\d{8}-\d{6}-alice-e0$", rid), rid


def test_a_name_is_slugified_so_it_is_a_safe_label_value() -> None:
    assert new_run_id("E0 real/shuffled", "alice", when=1754300000.0).endswith(
        "-alice-e0-real-shuffled"
    )


def test_two_users_in_the_same_second_get_different_ids() -> None:
    # One run directory holding one person's summary.json, the other's gone.
    when = 1754300000.0
    assert new_run_id("e0", "alice", when=when) != new_run_id("e0", "bob", when=when)


def test_the_same_user_and_name_twice_in_one_second_disambiguates() -> None:
    when = 1754300000.0
    first = new_run_id("e0", "alice", when=when, attempt=0)
    second = new_run_id("e0", "alice", when=when, attempt=1)
    assert first != second
    assert second.endswith("-2")


def test_when_zero_is_an_instant_not_absent() -> None:
    # `when or time.time()` treated the epoch as unset and stamped "now".
    assert new_run_id("e0", "alice", when=0.0) == new_run_id("e0", "alice", when=0.0)


def test_an_empty_name_falls_back_rather_than_ending_in_a_bare_dash() -> None:
    assert new_run_id("", "alice", when=1754300000.0).endswith("-alice-run")
    assert new_run_id("///", "alice", when=1754300000.0).endswith("-alice-run")


def test_an_emoji_only_name_falls_back() -> None:
    assert new_run_id("🎉", "alice", when=1754300000.0).endswith("-alice-run")


def test_a_missing_user_falls_back() -> None:
    assert new_run_id("e0", "", when=1754300000.0).endswith("-unknown-e0")


def test_current_user_returns_something() -> None:
    assert current_user()


def test_git_sha_matches_git_inside_a_checkout() -> None:
    # `sha == "unknown" or re.match(...)` passes against a permanently broken
    # implementation that always returns "unknown". Inside a checkout it must
    # equal what git itself reports.
    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert git_sha() == expected


def test_git_sha_is_unknown_outside_a_checkout(tmp_path: Path) -> None:
    # Launched from a tarball there is no .git, and a run must not fail for it.
    assert git_sha(repo=tmp_path) == "unknown"
