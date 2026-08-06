"""What two accounts sharing one box need from the filesystem.

Everything here exists because a colleague can, by accident, destroy the shared
run history: a run directory only its owner can read, a lock file only its
creator can open, a read-modify-write two runs enter at once, or a label value
that poisons every later rebuild. Each function is the narrow fix for one of
those, and each is the boundary where the fix is applied exactly once.
"""

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sparks.run import new_run_id

DIR_MODE = 0o2775
"""setgid, group-writable. 2775 not 2770: at 2770 an unprivileged scraper
account (node_exporter as `nobody`) cannot read a summary, measured; at 2775 it
can. The group is inherited from the setgid parent, so only the bits are ours to
set."""

FILE_MODE = 0o664
"""Group-readable, so the colleague whose GPU a run is hogging can read its log.
`output.log` is opened "wb" and lands 0600 under umask 077 without this."""

MAX_TEXT = 256
"""A cleaned name or user rides every row of the index forever, so it is capped.
Free text (a command, an error) passes a larger explicit limit."""


def make_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create a directory both group members can always use.

    The chmod is separate from the mkdir because mkdir's mode is masked by the
    caller's umask and chmod is not: `os.makedirs(0o2775)` at umask 077 lands
    2700. It is attempted even when the directory already existed, so a tree
    left at 2700 by an earlier umask heals the next time its owner runs.

    `suppress(OSError)` on the chmod is load-bearing and measured: chmod on a
    directory another user owns is EPERM, and without the suppression the second
    user's run dies on a directory the first user created.

    `mode` applies to `path` alone; parents created on the way get the default.
    The one caller that passes something else is the queue root, which needs the
    sticky bit and whose parents emphatically do not.
    """
    if not path.parent.is_dir():
        make_dir(path.parent)
    with contextlib.suppress(FileExistsError):
        path.mkdir()
    with contextlib.suppress(OSError):
        os.chmod(path, mode)
    return path


def reserve_dir(
    parent: Path,
    name: str,
    user: str,
    prefix: str = "run",
    when: float | None = None,
) -> tuple[str, Path]:
    """Claim an id by creating its directory.

    `mkdir` raising `FileExistsError` is the collision check, and it is the only
    *hard* uniqueness guarantee available: it is atomic, so two users starting
    in the same second get two distinct directories rather than one shared one.
    The attempt suffix breaks the tie for one person launching the same name
    twice in a second.

    `prefix` is `run` for a run and `job` for a queued job. The two share this
    function because they share the hazard: two accounts, one directory, one
    second.
    """
    make_dir(parent)
    for attempt in range(1000):
        reserved = new_run_id(name, user, when=when, attempt=attempt, prefix=prefix)
        directory = parent / reserved
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        with contextlib.suppress(OSError):
            os.chmod(directory, DIR_MODE)
        return reserved, directory
    raise RuntimeError(f"no free {prefix} id under {parent} for {name!r}")


@contextmanager
def exclusive(directory: Path, timeout: float = 30.0) -> Iterator[None]:
    """Serialise the index's read-modify-write across both users.

    Locks the directory itself rather than a lock file: a lock file created
    under umask 077 is 0600 and the other user can never open it, which is the
    same class of bug this whole module exists to fix. `flock` is the only
    option on an O_RDONLY fd (`fcntl.lockf` raises EBADF on one), and O_RDONLY is
    all a directory can be opened for. The kernel releases it on process death,
    so there is no stale lock, no pid file and no reaper. It is advisory, so
    node_exporter reading the file is unaffected.
    """
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    msg = f"{directory} was locked for over {timeout:g}s"
                    raise TimeoutError(msg) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the lock


def clean(value: str, fallback: str = "unknown", limit: int = MAX_TEXT) -> str:
    """Make text safe to persist and to carry as a Prometheus label value.

    Python decodes argv with surrogateescape, so `--name $'bad\\xffname'` reaches
    us as a lone surrogate that `json.dumps` escapes silently and every later
    `f.write()` then raises `UnicodeEncodeError` on, freezing the shared index.
    Encoding back out with surrogateescape recovers the original bytes; decoding
    with "replace" turns exactly the undecodable ones into U+FFFD and leaves
    genuine UTF-8 alone. The limit is a separate concern from the encoding.
    """
    text = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return text[:limit] or fallback
