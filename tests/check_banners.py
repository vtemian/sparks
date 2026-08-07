"""Lint: no decorative section-banner comments.

Ruff has nothing for this. A line like

    # -- reaching a box you are not sitting at -----------------------------------

is a table of contents pretending to be a comment. Structure is modules and
functions; comments argue why. Dashed underlines and `# ====` bars fail here.

Run: uv run python tests/check_banners.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = (ROOT / "src" / "sparks", ROOT / "tests")

# `# -- title ---`, `# === title ===`, or a line that is mostly dashes/equals.
BANNER = re.compile(
    r"^#\s*(?:"
    r"[-=*]{3,}"  # `# ----` or `# ====`
    r"|[-=*]{2,}\s+\S.*[-=*]{2,}"  # `# -- title ---`
    r")\s*$"
)

SKIP_NAMES = frozenset({"check_banners.py", "check_names.py", "check_dashboard.py"})


def is_banner(line: str) -> bool:
    # Stripped, not rstripped: an indented banner inside a class body is the
    # same noise as one at column 0, and anchoring on `^#` let four of them
    # sit in process.py unflagged.
    return BANNER.match(line.strip()) is not None


def violations_in(path: Path) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if is_banner(line):
            hits.append(f"{path}:{lineno}: section banner — delete it")
    return hits


def iter_python() -> list[Path]:
    found: list[Path] = []
    for root in SCAN:
        found.extend(
            path for path in sorted(root.rglob("*.py")) if path.name not in SKIP_NAMES
        )
    return found


def main() -> int:
    hits: list[str] = []
    for path in iter_python():
        hits.extend(violations_in(path))
    if not hits:
        return 0
    print(
        "check_banners: no decorative section comments (# -- title --- / # ====)",
        file=sys.stderr,
    )
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
