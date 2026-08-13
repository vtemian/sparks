from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gates.check_private_prefix import SRC, violations_in

if TYPE_CHECKING:
    from pathlib import Path


def test_module_level_private_is_a_violation(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("def _helper() -> None:\n    return None\n", encoding="utf-8")
    assert violations_in(path)


def test_class_method_private_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "class Box:\n    def _helper(self) -> None:\n        return None\n",
        encoding="utf-8",
    )
    assert violations_in(path) == []


def test_nested_private_is_a_violation(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "def outer() -> None:\n    def _inner() -> None:\n        return None\n",
        encoding="utf-8",
    )
    assert violations_in(path)


def test_dunder_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "class Box:\n    def __enter__(self) -> None:\n        return None\n",
        encoding="utf-8",
    )
    assert violations_in(path) == []


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC)),
)
def test_src_module_funcs_are_not_underscore_prefixed(path: Path) -> None:
    assert violations_in(path) == []
