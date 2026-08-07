"""Lint: no unused code in src/.

Ruff catches unused imports (F) and assigned-but-unread locals (F841). It does
not catch unused classes, functions, or signature slots the protocol still
requires. Vulture does. Scavenge `src/sparks` and `tests` together so a public
name the suite exercises is not reported, then fail on hits under `src/` at
min_confidence 60.

Schema fields that are persisted and never attributed in Python today go in
IGNORE_NAMES with a why — do not grow that set without the same kind of
argument.

Run: uv run python tests/check_unused.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sparks"
TESTS = ROOT / "tests"

MIN_CONFIDENCE = 60

# Names vulture cannot see a reader for, that are still load-bearing.
IGNORE_NAMES = frozenset(
    {
        # Energy schema: written into summary.json so a later revision can
        # recalibrate the busy-GPU threshold from records. No Python reader yet.
        "idle_gpu_watts",
    }
)


def findings() -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "vulture",
        str(SRC),
        str(TESTS),
        f"--min-confidence={MIN_CONFIDENCE}",
    ]
    if IGNORE_NAMES:
        cmd.extend(["--ignore-names", ",".join(sorted(IGNORE_NAMES))])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode not in (0, 3):
        # 0 = clean, 3 = dead code found. Anything else is a tool failure.
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise RuntimeError(f"vulture failed: {detail}")
    prefix = str(SRC)
    return [line for line in result.stdout.splitlines() if line.startswith(prefix)]


def main() -> int:
    hits = findings()
    if not hits:
        return 0
    print(
        "check_unused: dead code in src/ "
        "(ruff misses unused defs; vulture catches them)",
        file=sys.stderr,
    )
    for hit in hits:
        print(hit, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
