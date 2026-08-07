from __future__ import annotations

import ast
import sys
from itertools import pairwise
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"


def ends_in_return(statement: ast.stmt) -> bool:
    tail: ast.stmt = statement
    while True:
        body = getattr(tail, "body", None)
        if not body:
            return isinstance(tail, ast.Return)

        tail = body[-1]


def violations_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue

        for statement, following in pairwise(body):
            if not ends_in_return(statement) or statement.end_lineno is None:
                continue
            # A comment between the two counts as separation: the eye already
            # has somewhere to rest.
            if following.lineno - statement.end_lineno != 1:
                continue

            hits.append(
                f"{path}:{statement.end_lineno}: blank line needed after "
                f"`{lines[statement.end_lineno - 1].strip()}`"
            )

    return sorted(hits)


def check() -> list[str]:
    return [hit for path in sorted(SRC.rglob("*.py")) for hit in violations_in(path)]


def main() -> int:
    hits = check()
    if hits:
        print("check_blank_after_return: give a guard clause room", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)

        return 1

    print("check_blank_after_return: every return has room after it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
