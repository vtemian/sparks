"""What the box provides, as the box itself declared it.

sparkup writes /etc/sparks/box.toml when it provisions a machine for sparks: the
shared tree runs are recorded in, the textfile directory node_exporter scrapes,
and the Prometheus and Grafana it stood up. This module reads that file and
nothing else. It creates no directories and invents no paths.

The reason it does not simply probe for those directories: probing cannot tell
"this box was never set up for sparks" from "it was set up and something
broke", and those need opposite advice. Nor can probing discover the values a
box chose - `spark_shared_dir` is overridable per host, so the location is
knowable only if the box says it. A guess here means a run recorded into a
directory nothing reads, which is the failure this module exists to prevent.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Not in the shared tree: /etc is where box configuration belongs, it is outside
# anything `make deploy` rsyncs with --delete, and it survives renaming the
# shared directory - which is a migration this box still has ahead of it.
PATH = Path("/etc/sparks/box.toml")

PATHS = ("shared_dir", "textfile_dir")
STRINGS = ("shared_group", "prometheus_url", "grafana_url", "registry_url")


class NotProvisionedError(Exception):
    """This box carries no sparks contract, so nothing here knows where to write."""


class MalformedError(Exception):
    """The contract exists but cannot be read. Someone edited it by hand."""


@dataclass(frozen=True)
class Box:
    """The provisioned box, exactly as declared. Never partially filled in: a
    contract missing a field is MalformedError, because a default here is the same
    guess this module refuses to make."""

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
        """Derived rather than declared, exactly like `runs_dir`.

        Not a guess of the kind this module refuses to make: the box states
        where its shared tree is, and this is a fixed place inside it. Adding a
        field would make every box provisioned by an older sparkup read as
        MalformedError for no gain.
        """
        return self.shared_dir / "queue"


def load(path: Path | None = None) -> Box | None:
    """The contract, or None when this box has none.

    None rather than an exception: "no contract" is a fact a caller decides what
    to do about. The CLI refuses to run; a library caller may carry on with
    explicit paths.
    """
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise MalformedError(f"{path} cannot be read: {e}") from e
    try:
        data = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise MalformedError(f"{path} is not valid TOML: {e}") from e
    missing = [f for f in (*PATHS, *STRINGS) if f not in data]
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
    """Everything wrong with what the contract promised, one complaint per
    problem so a reader fixes them in one pass instead of discovering the second
    after fixing the first.

    Prometheus reachability is deliberately absent. An unreachable Prometheus is
    degraded telemetry and telemetry never kills a run. A shared tree that
    cannot be written to is different in kind: the record of the run is lost,
    and recording the run is the whole job.
    """
    return [c for c in (_usable(target.runs_dir), _usable(textfile_dir(target))) if c]


def textfile_dir(target: Box | None = None) -> Path:
    """Where node_exporter reads .prom files from.

    The environment override beats the contract because tests/conftest.py sets
    it for the whole suite: on a developer's own provisioned box, a contract
    that won here would pull the unit tests onto the real index.
    """
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


def _usable(path: Path) -> str:
    if not path.is_dir():
        return f"{path} does not exist"
    # Effective ids, and no ownership requirement: on a shared box every user's
    # runs land in one setgid tree that someone else created.
    if not os.access(path, os.W_OK | os.X_OK, effective_ids=True):
        return f"{path} is not writable by this account"
    return ""
