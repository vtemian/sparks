"""Lint: variable names must be a noun or a verb, not a letter.

Ruff has no VNE001 yet (https://github.com/astral-sh/ruff/issues/3463). Until
it does, this is the rule: no `p =`, no `as e`, no `submit_p`. A name is one
or more snake_case words; each word is at least two letters. `_` alone is the
only single-letter form allowed (discard).

Run: uv run python tests/check_names.py
Exit 1 on the first file that fails, listing every hit.
"""

from __future__ import annotations

import ast
import re
import sys
from functools import singledispatch
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"

# submit_p, host_f — a real word never ends in a lone letter suffix like that.
LETTER_SUFFIX = re.compile(r"^[a-z]+_[a-z]$")

# Two-letter locals that are not words we want in this codebase.
BANNED_EXACT = frozenset({"qd"})

SINGLE_LETTER = re.compile(r"^[a-zA-Z]$")


def is_banned(name: str) -> bool:
    if name == "_":
        return False
    bare = name.lstrip("_") or name
    if SINGLE_LETTER.fullmatch(bare):
        return True
    if bare in BANNED_EXACT:
        return True
    return LETTER_SUFFIX.fullmatch(bare) is not None


def target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [name for elt in node.elts for name in target_names(elt)]
    if isinstance(node, ast.Starred):
        return target_names(node.value)
    return []


@singledispatch
def binding_targets(_node: ast.AST) -> list[tuple[ast.AST, int]]:
    return []


@binding_targets.register
def _(node: ast.Assign) -> list[tuple[ast.AST, int]]:
    return [(target, node.lineno) for target in node.targets]


@binding_targets.register
def _(node: ast.AnnAssign) -> list[tuple[ast.AST, int]]:
    if node.target is None:
        return []
    return [(node.target, node.lineno)]


@binding_targets.register
def _(node: ast.For) -> list[tuple[ast.AST, int]]:
    return [(node.target, node.lineno)]


@binding_targets.register
def _(node: ast.With) -> list[tuple[ast.AST, int]]:
    return [
        (item.optional_vars, node.lineno)
        for item in node.items
        if item.optional_vars is not None
    ]


@binding_targets.register
def _(node: ast.ExceptHandler) -> list[tuple[ast.AST, int]]:
    if node.name is None:
        return []
    # Synthetic Name so the shared reporter path stays one shape.
    return [(ast.Name(id=node.name, ctx=ast.Store()), node.lineno)]


@binding_targets.register
def _(node: ast.ListComp) -> list[tuple[ast.AST, int]]:
    return _from_comprehension(node)


@binding_targets.register
def _(node: ast.SetComp) -> list[tuple[ast.AST, int]]:
    return _from_comprehension(node)


@binding_targets.register
def _(node: ast.GeneratorExp) -> list[tuple[ast.AST, int]]:
    return _from_comprehension(node)


@binding_targets.register
def _(node: ast.DictComp) -> list[tuple[ast.AST, int]]:
    return _from_comprehension(node)


def _from_comprehension(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
) -> list[tuple[ast.AST, int]]:
    return [(gen.target, gen.target.lineno) for gen in node.generators]


def bindings(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        found.extend(
            (name, lineno)
            for target, lineno in binding_targets(node)
            for name in target_names(target)
        )
    return found


def violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        f"{path}:{lineno}: {name!r} — use a noun or verb, not a letter"
        for name, lineno in bindings(tree)
        if is_banned(name)
    ]


def main() -> int:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        hits.extend(violations_in(path))
    if not hits:
        return 0
    print("check_names: variable names must be a noun or verb", file=sys.stderr)
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
