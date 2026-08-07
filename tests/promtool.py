import shutil
import subprocess
from functools import cache
from pathlib import Path

IMAGE = "prom/prometheus:v3.13.2"

LOCAL = shutil.which("promtool")
DOCKER = shutil.which("docker")

Result = subprocess.CompletedProcess[str]


@cache
def usable() -> bool:
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
    if LOCAL is not None:
        return _run([LOCAL, "check", "metrics"], stdin=text)
    return _run([*_docker(), "check", "metrics"], stdin=text)


def check_rules(path: Path) -> Result:
    if LOCAL is not None:
        return _run([LOCAL, "check", "rules", str(path)])
    mount = ["-v", f"{path.parent}:/work:ro"]
    return _run([*_docker(mount), "check", "rules", f"/work/{path.name}"])


def _docker(extra: list[str] | None = None) -> list[str]:
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
