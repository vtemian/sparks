"""Lint: no 1-2 statement private helpers or nested functions.

Ruff caps *maximum* size (PLR0915 / C901); it has no minimum. Tiny `_helpers`
and nested `def`s are noise — inline them. Exempt:

- class methods (the object's public shape, including Protocol stubs)
- module-level public names (CLI verbs, `main`, …)
- `@property` / `@abstractmethod`
- dunder methods
- bodies that are only `...` / `pass` / `NotImplementedError`

Run: uv run python tests/check_short_funcs.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"
MIN_STATEMENTS = 3


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def statement_count(body: list[ast.stmt]) -> int:
    return sum(1 for stmt in body if not _is_docstring(stmt))


def _is_ellipsis(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is ...
    )


def _is_not_implemented(stmt: ast.stmt) -> bool:
    if not (isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call)):
        return False
    func = stmt.exc.func
    return isinstance(func, ast.Name) and func.id == "NotImplementedError"


def _is_stub(body: list[ast.stmt]) -> bool:
    stmts = [stmt for stmt in body if not _is_docstring(stmt)]
    if len(stmts) != 1:
        return not stmts
    stmt = stmts[0]
    return isinstance(stmt, ast.Pass) or _is_ellipsis(stmt) or _is_not_implemented(stmt)


def _has_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef, names: set[str]
) -> bool:
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id in names:
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in names:
            return True
    return False


def _method_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ids.add(id(child))
    return ids


def _nested_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in parent.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ids.add(id(child))
    return ids


def _is_exempt(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool
) -> bool:
    if _is_stub(node.body):
        return True
    if _has_decorator(node, {"property", "cached_property", "abstractmethod"}):
        return True
    if node.name.startswith("__") and node.name.endswith("__"):
        return True
    return is_method


def is_too_short(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool,
    is_nested: bool,
) -> bool:
    if _is_exempt(node, is_method=is_method):
        return False
    if statement_count(node.body) >= MIN_STATEMENTS:
        return False
    if is_nested:
        return True
    # Module-level: only private helpers. Public names are the API.
    return node.name.startswith("_")


def violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    methods = _method_ids(tree)
    nested = _nested_ids(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not is_too_short(
            node, is_method=id(node) in methods, is_nested=id(node) in nested
        ):
            continue
        count = statement_count(node.body)
        kind = "nested" if id(node) in nested else "private"
        hits.append(
            f"{path}:{node.lineno}: {kind} {node.name!r} has {count} statement(s) "
            f"(need >= {MIN_STATEMENTS}) — inline it"
        )
    return hits


def main() -> int:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        hits.extend(violations_in(path))
    if not hits:
        return 0
    print(
        "check_short_funcs: private helpers and nested defs need "
        f">= {MIN_STATEMENTS} statements",
        file=sys.stderr,
    )
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
