from __future__ import annotations

import json
import logging
import platform
from pathlib import Path
from typing import Any

from sparks import dock
from sparks.client.remote import ClientError, fetch_registry_url, registry_netloc

LOG = logging.getLogger("sparks")

INSECURE_KEY = "insecure-registries"

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


def trust_box_registry(host: str) -> int:
    netloc = registry_netloc(fetch_registry_url(host))
    LOG.debug("box %s registers images at %s", host, netloc)

    if netloc in trusted_registries():
        print(f"sparks: Docker already trusts {netloc}; nothing to do")
        return 0

    path = daemon_json_path()
    if trust_registry(path, netloc):
        print(f"sparks: added {netloc} to {path}")
    else:
        print(f"sparks: {netloc} is already in {path}, but Docker has not read it yet")

    print(f"sparks: restart Docker to pick it up:\n  {restart_hint()}")
    return 0
