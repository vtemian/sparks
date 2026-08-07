from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from tests.check_short_funcs import SRC, is_too_short, violations_in

if TYPE_CHECKING:
    from pathlib import Path


def _func(src: str) -> ast.FunctionDef:
    tree = ast.parse(src)
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def test_private_one_liner_is_too_short() -> None:
    node = _func("def _helper(x):\n    return x + 1\n")
    assert is_too_short(node, is_method=False, is_nested=False)


def test_public_one_liner_is_allowed() -> None:
    node = _func("def host_from(x):\n    return x\n")
    assert not is_too_short(node, is_method=False, is_nested=False)


def test_method_one_liner_is_allowed() -> None:
    node = _func("def poll(self):\n    return self._child.poll()\n")
    assert not is_too_short(node, is_method=True, is_nested=False)


def test_nested_one_liner_is_too_short() -> None:
    node = _func(
        "def outer():\n    def write(x):\n        return x\n    return write\n"
    )
    nested = node.body[0]
    assert isinstance(nested, ast.FunctionDef)
    assert is_too_short(nested, is_method=False, is_nested=True)


def test_three_statements_ok() -> None:
    node = _func("def _helper(x):\n    a = x + 1\n    b = a + 1\n    return b\n")
    assert not is_too_short(node, is_method=False, is_nested=False)


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC)),
)
def test_src_has_no_short_private_helpers(path: Path) -> None:
    assert violations_in(path) == []
