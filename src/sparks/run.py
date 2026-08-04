"""Naming a run, and the metadata that identifies it."""

import re
import subprocess
import time
from pathlib import Path


def new_run_id(name: str, when: float | None = None) -> str:
    """`run-YYYYmmdd-HHMM-<name>`, so runs sort chronologically as strings.

    That matters because the Grafana variable sorts them as strings and picks
    the first, which is how the newest run ends up selected on load."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(when or time.time()))
    return f"run-{stamp}-{slug(name)}"


def slug(name: str) -> str:
    """A label-value-safe form: a run id ends up in a PromQL regex."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def git_sha(repo: Path | None = None) -> str:
    """Short HEAD sha, or `unknown` outside a checkout.

    Never raises: a run must not fail because it was launched from a tarball."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or Path.cwd(),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return out.stdout.strip() or "unknown"
