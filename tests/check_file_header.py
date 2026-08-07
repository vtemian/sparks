"""Lint: no `#` comments before the first top-level def/class.

Ruff has nothing for this. The head of a module is the docstring (optional),
imports, and bindings — not a second essay in `#` lines. Argue next to the
binding (a docstring on the constant) or in the module docstring.

Shebang and coding cookies are allowed. Module docstrings are allowed.

Run: uv run python tests/check_file_header.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"


def _first_def_or_class_line(tree: ast.Module) -> int | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.lineno
    return None


def is_header_comment(line: str, *, lineno: int) -> bool:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    if lineno == 1 and stripped.startswith("#!"):
        return False
    return "coding" not in stripped and "encoding" not in stripped


def violations_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    cutoff = _first_def_or_class_line(tree)
    if cutoff is None:
        cutoff = len(source.splitlines()) + 1

    hits: list[str] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if lineno >= cutoff:
            break
        if is_header_comment(line, lineno=lineno):
            hits.append(f"{path}:{lineno}: header `#` comment — move into a docstring")
    return hits


def main() -> int:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        hits.extend(violations_in(path))
    if not hits:
        return 0
    print(
        "check_file_header: no `#` comments before the first def/class",
        file=sys.stderr,
    )
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
