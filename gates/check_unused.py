from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sparks"
TESTS = ROOT / "tests"
GATES = ROOT / "gates"

MIN_CONFIDENCE = 60

# Names vulture cannot see a reader for, that are still load-bearing.
IGNORE_NAMES = frozenset(
    {
        # Energy schema: written into summary.json so a later revision can
        # recalibrate the busy-GPU threshold from records. No Python reader yet.
        "idle_gpu_watts",
    }
)


def findings(paths: list[Path] | None = None) -> list[str]:
    scanned = [SRC, TESTS, GATES] if paths is None else paths
    cmd = [
        sys.executable,
        "-m",
        "vulture",
        *[str(path) for path in scanned],
        f"--min-confidence={MIN_CONFIDENCE}",
    ]
    if IGNORE_NAMES:
        cmd.extend(["--ignore-names", ",".join(sorted(IGNORE_NAMES))])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode not in (0, 3):
        # 0 = clean, 3 = dead code found. Anything else is a tool failure.
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise RuntimeError(f"vulture failed: {detail}")

    # Only the first path is held to account. The rest are scanned so their
    # references keep a name alive, not so their own dead code is reported.
    prefix = str(scanned[0])
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
