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
    moment = time.time() if when is None else when
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    tie = f"-{attempt + 1}" if attempt else ""
    return f"{prefix}-{stamp}-{slug(user, 'unknown')}-{slug(name, 'run')}{tie}"


def slug(name: str, fallback: str = "") -> str:
    return (
        re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")
        or fallback
    )


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- a missing account is not a failure
        return os.environ.get("USER", "unknown")


def git_sha(repo: Path | None = None) -> str:
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
