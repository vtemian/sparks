import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PATH = Path("/etc/sparks/box.toml")

PATHS = ("shared_dir", "textfile_dir")
STRINGS = ("shared_group", "prometheus_url", "grafana_url", "registry_url")


class NotProvisionedError(Exception): ...


class MalformedError(Exception): ...


@dataclass(frozen=True)
class Box:
    shared_dir: Path
    shared_group: str
    textfile_dir: Path
    prometheus_url: str
    grafana_url: str
    registry_url: str

    @property
    def runs_dir(self) -> Path:
        return self.shared_dir / "runs"

    @property
    def queue_dir(self) -> Path:
        return self.shared_dir / "queue"


def load(path: Path | None = None) -> Box | None:
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MalformedError(f"{path} cannot be read: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise MalformedError(f"{path} is not valid TOML: {exc}") from exc
    missing = [field for field in (*PATHS, *STRINGS) if field not in data]
    if missing:
        raise MalformedError(f"{path} is missing {', '.join(sorted(missing))}")
    return Box(
        shared_dir=Path(data["shared_dir"]),
        shared_group=str(data["shared_group"]),
        textfile_dir=Path(data["textfile_dir"]),
        prometheus_url=str(data["prometheus_url"]),
        grafana_url=str(data["grafana_url"]),
        registry_url=str(data["registry_url"]),
    )


def config_path() -> Path:
    override = os.environ.get("SPARKS_BOX_CONFIG")
    return Path(override) if override else PATH


def preflight(target: Box) -> list[str]:
    return [
        complaint
        for complaint in (usable(target.runs_dir), usable(textfile_dir(target)))
        if complaint
    ]


def textfile_dir(target: Box | None = None) -> Path:
    # The override beats the contract, or the suite runs against the real index.
    override = os.environ.get("SPARKS_TEXTFILE_DIR")
    if override:
        return Path(override)

    target = target or load()
    if target is None:
        raise NotProvisionedError(
            f"{config_path()} does not exist, so there is nowhere to publish the "
            "run index that Prometheus would scrape"
        )
    return target.textfile_dir


def usable(path: Path) -> str:
    if not path.is_dir():
        return f"{path} does not exist"

    if not os.access(path, os.W_OK | os.X_OK, effective_ids=True):
        return f"{path} is not writable by this account"

    return ""
