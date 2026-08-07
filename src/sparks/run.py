"""Naming a run, and the metadata that identifies it."""

import getpass
import os
import re
import subprocess
import time
from pathlib import Path


def new_run_id(
    name: str,
    user: str,
    when: float | None = None,
    attempt: int = 0,
    prefix: str = "run",
) -> str:
    """`<prefix>-YYYYmmdd-HHMMSS-<user>-<name>`, chronological as a string. The
    username is what makes a cross-user collision structurally impossible."""
    moment = time.time() if when is None else when
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    tie = f"-{attempt + 1}" if attempt else ""
    return f"{prefix}-{stamp}-{slug(user, 'unknown')}-{slug(name, 'run')}{tie}"


def slug(name: str, fallback: str = "") -> str:
    """A label-value-safe form: a run id ends up in a PromQL regex. A name that
    slugs to empty takes the fallback, so no id ends in a bare `-`."""
    return (
        re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")
        or fallback
    )


def current_user() -> str:
    """Who to ask about a run. Never raises: both of getpass's sources can be
    absent in a container."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- a missing account is not a failure
        return os.environ.get("USER", "unknown")


def git_sha(repo: Path | None = None) -> str:
    """Short HEAD sha, or `unknown` outside a checkout. Never raises: a run must
    not fail because it was launched from a tarball."""
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
