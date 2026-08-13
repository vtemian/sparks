from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gates.check_names import SRC, is_banned, violations_in

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


def test_a_letter_parameter_is_reported_on_its_own_line(tmp_path: Path) -> None:
    module = tmp_path / "offender.py"
    module.write_text("def submit(\n    p,\n    e,\n) -> None: ...\n")

    assert [hit.removeprefix(f"{module}:") for hit in violations_in(module)] == [
        "2: 'p' — use a noun or verb, not a letter",
        "3: 'e' — use a noun or verb, not a letter",
    ]


def test_every_kind_of_parameter_is_seen(tmp_path: Path) -> None:
    module = tmp_path / "offender.py"
    module.write_text(
        "def submit(p, /, e, *f, g=1, **h) -> None: ...\n"
        "async def wait(k) -> None: ...\n"
        "def sorter() -> object:\n"
        "    return lambda q: q\n"
    )

    assert {hit.split("'")[1] for hit in violations_in(module)} == {
        "p",
        "e",
        "f",
        "g",
        "h",
        "k",
        "q",
    }


def test_self_and_cls_are_words_enough(tmp_path: Path) -> None:
    module = tmp_path / "clean.py"
    module.write_text(
        "class Queue:\n"
        "    def submit(self) -> None: ...\n"
        "    @classmethod\n"
        "    def load(cls) -> None: ...\n"
    )

    assert violations_in(module) == []


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC)),
)
def test_src_has_no_letter_locals(path: Path) -> None:
    assert violations_in(path) == []
