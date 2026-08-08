from __future__ import annotations

import ast
import sys
from itertools import pairwise
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sparks"

BLOCKS = ("body", "orelse", "finalbody")


def blocks_of(node: ast.AST) -> list[list[ast.stmt]]:
    found = [
        block
        for field in BLOCKS
        if isinstance(block := getattr(node, field, None), list) and block
    ]
    found += [h.body for h in getattr(node, "handlers", []) if h.body]
    found += [c.body for c in getattr(node, "cases", []) if c.body]

    return found


def exits_by_return(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Return):
        return True

    # Any arm that ends in a return makes this a guard: control either left
    # here or carried on to the sibling below, and those are two thoughts.
    return any(exits_by_return(block[-1]) for block in blocks_of(statement) if block)


def violations_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        for block in blocks_of(node):
            for statement, following in pairwise(block):
                if not exits_by_return(statement) or statement.end_lineno is None:
                    continue
                # A comment between the two already gives the eye somewhere
                # to rest.
                if following.lineno - statement.end_lineno != 1:
                    continue

                hits.append(
                    f"{path}:{following.lineno}: blank line needed before "
                    f"`{lines[following.lineno - 1].strip()}`"
                )

    return sorted(set(hits))


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
