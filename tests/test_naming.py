"""House naming rules ruff cannot express.

pep8-naming (`N`) catches class/function shape. It does not ban abbreviations
like `submit_p` ("parser") or `qd` ("queue dir"). Those are forbidden here so
a reader never has to expand a suffix in their head.
"""

from __future__ import annotations

import ast
import re
from functools import singledispatch
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"

# Letter + single-letter suffix: submit_p, host_f, …
LETTER_SUFFIX = re.compile(r"^[a-z]+_[a-z]$")

BANNED_EXACT = frozenset({"qd", "p", "q"})

SRC_MODULES = sorted(SRC.rglob("*.py"))


def _is_banned(name: str) -> bool:
    bare = name.lstrip("_") or name
    if bare in BANNED_EXACT:
        return True
    return LETTER_SUFFIX.fullmatch(bare) is not None


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [name for elt in node.elts for name in _target_names(elt)]
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return []


@singledispatch
def _binding_targets(_node: ast.AST) -> list[tuple[ast.AST, int]]:
    return []


@_binding_targets.register
def _(node: ast.Assign) -> list[tuple[ast.AST, int]]:
    return [(target, node.lineno) for target in node.targets]


@_binding_targets.register
def _(node: ast.AnnAssign) -> list[tuple[ast.AST, int]]:
    if node.target is None:
        return []
    return [(node.target, node.lineno)]


@_binding_targets.register
def _(node: ast.For) -> list[tuple[ast.AST, int]]:
    return [(node.target, node.lineno)]


@_binding_targets.register
def _(node: ast.With) -> list[tuple[ast.AST, int]]:
    return [
        (item.optional_vars, node.lineno)
        for item in node.items
        if item.optional_vars is not None
    ]


def _bindings(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        found.extend(
            (name, lineno)
            for target, lineno in _binding_targets(node)
            for name in _target_names(target)
        )
    return found


@pytest.mark.parametrize(
    "path",
    SRC_MODULES,
    ids=[str(path.relative_to(SRC)) for path in SRC_MODULES],
)
def test_no_cryptic_abbreviated_locals(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = [
        f"{path.relative_to(SRC)}:{lineno}: {name}"
        for name, lineno in _bindings(tree)
        if _is_banned(name)
    ]
    assert not bad, "spell the word out (submit_parser, not submit_p):\n" + "\n".join(
        bad
    )
