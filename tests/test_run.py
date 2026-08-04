import re

from sparks.run import git_sha, new_run_id


def test_run_ids_sort_chronologically_as_strings() -> None:
    early = new_run_id("e0", when=1754300000.0)
    late = new_run_id("e0", when=1754390000.0)
    assert early < late


def test_the_shape_is_stable() -> None:
    rid = new_run_id("e0", when=1754300000.0)
    assert re.match(r"^run-\d{8}-\d{4}-e0$", rid), rid


def test_a_name_is_slugified_so_it_is_a_safe_label_value() -> None:
    assert new_run_id("E0 real/shuffled", when=1754300000.0).endswith(
        "-e0-real-shuffled"
    )


def test_git_sha_is_short_or_unknown() -> None:
    sha = git_sha()
    assert sha == "unknown" or re.match(r"^[0-9a-f]{7,12}$", sha)
