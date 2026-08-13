from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = (ROOT / "src" / "sparks", ROOT / "tests", ROOT / "examples", ROOT / "gates")


def docstring_of(node: ast.AST) -> ast.Constant | None:
    body = getattr(node, "body", None)
    if not body or not isinstance(body, list):
        return None

    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value

    return None


def violations_in(path: Path) -> list[str]:
    relative = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []

    module_doc = docstring_of(tree)
    if module_doc is not None:
        hits.append(f"{relative}:{module_doc.lineno}: module docstring — delete it")

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue

        found = docstring_of(node)
        if found is not None:
            hits.append(f"{relative}:{found.lineno}: docstring on {node.name} — delete")

    return hits


def check() -> list[str]:
    return [
        hit
        for root in SCAN
        for path in sorted(root.rglob("*.py"))
        for hit in violations_in(path)
    ]


def main() -> int:
    hits = check()
    if hits:
        print(
            "check_no_docstrings: the name and the signature are the documentation",
            file=sys.stderr,
        )
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)

        return 1

    print("check_no_docstrings: none found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
