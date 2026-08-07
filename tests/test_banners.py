"""Section-banner lint — see tests/check_banners.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.check_banners import is_banner, iter_python, violations_in


def test_dashed_title_is_a_banner() -> None:
    assert is_banner(
        "# -- reaching a box you are not sitting at -----------------------------------"
    )


def test_bar_only_is_a_banner() -> None:
    assert is_banner(
        "# --------------------------------------------------------------------------"
    )


def test_prose_comment_is_not_a_banner() -> None:
    assert not is_banner("# The trap: localhost inside a container is the container.")
    assert not is_banner("# noqa: BLE001 -- telemetry never kills a run")


@pytest.mark.parametrize(
    "path",
    iter_python(),
    ids=lambda path: str(path.relative_to(Path(__file__).resolve().parents[1])),
)
def test_tree_has_no_section_banners(path: Path) -> None:
    assert violations_in(path) == []
