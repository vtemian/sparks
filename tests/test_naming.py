from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.check_names import SRC, is_banned, violations_in

if TYPE_CHECKING:
    from pathlib import Path


def test_single_letter_is_banned() -> None:
    assert is_banned("p")
    assert is_banned("e")
    assert is_banned("f")
    assert is_banned("_p")


def test_discard_underscore_is_allowed() -> None:
    assert not is_banned("_")


def test_letter_suffix_abbreviation_is_banned() -> None:
    assert is_banned("submit_p")
    assert is_banned("reserve_p")


def test_real_words_are_allowed() -> None:
    assert not is_banned("parser")
    assert not is_banned("submit_parser")
    assert not is_banned("directory")
    assert not is_banned("exc")
    assert not is_banned("handle")


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC)),
)
def test_src_has_no_letter_locals(path: Path) -> None:
    assert violations_in(path) == []
