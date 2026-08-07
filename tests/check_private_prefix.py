from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }


def _is_private_name(name: str) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return False
    return name.startswith("_")


def _kind(
    node: ast.FunctionDef | ast.AsyncFunctionDef, parents: dict[ast.AST, ast.AST]
) -> str:
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, ast.ClassDef):
            return "method"
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return "nested"
        if isinstance(current, ast.Module):
            return "module"
        current = parents.get(current)
    return "module"


def violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _parents(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_private_name(node.name):
            continue
        kind = _kind(node, parents)
        if kind == "method":
            continue
        hits.append(
            f"{path}:{node.lineno}: {kind} {node.name!r} — "
            f"only class methods may use a leading _; rename to {node.name[1:]!r}"
        )
    return hits


def main() -> int:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        hits.extend(violations_in(path))
    if not hits:
        return 0
    print(
        "check_private_prefix: only class methods may be named _foo",
        file=sys.stderr,
    )
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
