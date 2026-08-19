from __future__ import annotations

import json
import logging
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sparks import dock
from sparks.client.remote import (
    ClientError,
    fetch_registry_url,
    registry_netloc,
    remember_host,
)

LOG = logging.getLogger("sparks")

INSECURE_KEY = "insecure-registries"

DOCKER_RETURN_SECONDS = 180.0

MACOS_RESTART = "osascript -e 'quit app \"Docker\"' && open -a Docker"
LINUX_RESTART = "sudo systemctl restart docker"


def on_macos() -> bool:
    return platform.system() == "Darwin"


def daemon_json_path() -> Path:
    if on_macos():
        return Path.home() / ".docker" / "daemon.json"

    return Path("/etc/docker/daemon.json")


def restart_hint() -> str:
    return MACOS_RESTART if on_macos() else LINUX_RESTART


def read_daemon(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientError(f"{path} is not readable JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ClientError(f"{path} holds {type(loaded).__name__}, not an object")

    return loaded


def insecure_list(daemon: dict[str, Any], path: Path) -> list[str]:
    listed = daemon.get(INSECURE_KEY, [])
    if not isinstance(listed, list):
        raise ClientError(f"{path}: {INSECURE_KEY} is not a list")

    return listed


def trust_registry(path: Path, netloc: str) -> bool:
    daemon = read_daemon(path)
    listed = insecure_list(daemon, path)
    if netloc in listed:
        return False

    daemon[INSECURE_KEY] = [*listed, netloc]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(daemon, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ClientError(f"cannot write {path}: {exc}. Try again with sudo") from exc

    return True


def trusted_registries() -> set[str]:
    try:
        config = dock.client().info().get("RegistryConfig") or {}
    except (dock.DockerException, OSError) as exc:
        raise ClientError(f"cannot ask Docker what it trusts: {exc}") from exc

    indexed = config.get("IndexConfigs") or {}
    return {name for name, entry in indexed.items() if not entry.get("Secure", True)}


def restart_docker() -> bool:
    # Only where it can be done without asking for a password. A Linux daemon
    # restart needs root, and prompting for it from a setup command is worse
    # than saying plainly what to run.
    if not on_macos():
        return False

    subprocess.run(["osascript", "-e", 'quit app "Docker"'], check=False)
    subprocess.run(["open", "-a", "Docker"], check=False)
    return wait_for_docker()


def wait_for_docker() -> bool:
    deadline = time.monotonic() + DOCKER_RETURN_SECONDS
    while time.monotonic() < deadline:
        try:
            dock.client().ping()
        except (dock.DockerException, OSError):
            time.sleep(2.0)
            continue

        return True

    return False


def trust_box_registry(host: str) -> int:
    # Asked first, so a box that never answered is not remembered as yours.
    netloc = registry_netloc(fetch_registry_url(host))
    remember_host(host)
    LOG.debug("box %s registers images at %s", host, netloc)

    if netloc in trusted_registries():
        print(f"sparks: ready; Docker already pushes to {netloc}")
        return 0

    path = daemon_json_path()
    if trust_registry(path, netloc):
        print(f"sparks: added {netloc} to {path}")

    print("sparks: restarting Docker, which stops any containers you have running")
    if restart_docker() and netloc in trusted_registries():
        print(f"sparks: ready; Docker now pushes to {netloc}")
        return 0

    print(f"sparks: restart Docker to pick it up:\n  {restart_hint()}")
    return 0


def ask_box() -> str | None:
    # Wheels cannot run setup. A TTY is the only place left to finish the
    # machine after `uv tool install`. A pipe must not hang waiting for one.
    if not sys.stdin.isatty():
        return None

    try:
        answer = input("sparks: the box, as ssh addresses it (you@your-box): ").strip()
    except EOFError:
        return None

    return answer or None


SKILL_HOMES = (Path(".claude") / "skills", Path(".agents") / "skills")


def packaged_skills() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"  # the wheel and a checkout


def skill_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []

    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def install_one(skill: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not dest.is_symlink():
        return f"SKIP    {dest} is a real directory"

    dest.unlink(missing_ok=True)
    dest.symlink_to(skill.resolve())
    return f"linked  {dest}"


def install_skills(*, home: Path | None = None, root: Path | None = None) -> list[str]:
    # Same two homes every harness already scans. A real directory there is
    # someone else's skill; leave it. A symlink we own is ours to repoint.
    base = Path.home() if home is None else home
    source = packaged_skills() if root is None else root
    return [
        install_one(skill, base / skill_home / skill.name)
        for skill in skill_dirs(source)
        for skill_home in SKILL_HOMES
    ]
