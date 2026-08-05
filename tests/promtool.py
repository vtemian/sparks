"""promtool, from wherever this machine can get it.

promtool is the only external validation of two formats we otherwise only
check against our own beliefs: the `.prom` textfile the index writes, and the
alert rules. Both tests used to `skipif` when the binary was absent, which is
the state on every machine that has not installed Prometheus by hand, so
`make check` passed while validating neither. A skip that is the normal case is
not a safety net.

The pinned image is the same one the live harness runs, so the promtool doing
the checking is the promtool of the Prometheus that will read these files.
A local binary still wins when present, because it is far faster than docker.
"""

import shutil
import subprocess
from functools import cache
from pathlib import Path

IMAGE = "prom/prometheus:v3.13.2"
"""Pinned to tests/harness/compose.yml. Bump both together, or the format is
validated by a different version than the one that consumes it."""

LOCAL = shutil.which("promtool")
DOCKER = shutil.which("docker")

Result = subprocess.CompletedProcess[str]


@cache
def usable() -> bool:
    """Whether promtool can be run at all, one way or the other.

    `docker info` rather than merely finding the client on PATH: a client with
    no daemon behind it is the common case on a laptop, and it fails in a way
    that would read as the rules being invalid rather than unchecked.
    """
    if LOCAL is not None:
        return True
    if DOCKER is None:
        return False
    probe = subprocess.run(
        [DOCKER, "info"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


REASON = "promtool needs either a local binary or a working docker"


def check_metrics(text: str) -> Result:
    """`promtool check metrics` over `text`, which it reads on stdin."""
    if LOCAL is not None:
        return _run([LOCAL, "check", "metrics"], stdin=text)
    return _run([*_docker(), "check", "metrics"], stdin=text)


def check_rules(path: Path) -> Result:
    """`promtool check rules` over a rules file.

    The containerised form mounts the file's directory read-only rather than
    the repo, so the check cannot touch anything it is not reading.
    """
    if LOCAL is not None:
        return _run([LOCAL, "check", "rules", str(path)])
    mount = ["-v", f"{path.parent}:/work:ro"]
    return _run([*_docker(mount), "check", "rules", f"/work/{path.name}"])


def _docker(extra: list[str] | None = None) -> list[str]:
    """The container invocation, up to but not including promtool's own args."""
    return [
        str(DOCKER),
        "run",
        "--rm",
        "-i",
        *(extra or []),
        "--entrypoint",
        "promtool",
        IMAGE,
    ]


def _run(argv: list[str], stdin: str | None = None) -> Result:
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, check=False
    )
