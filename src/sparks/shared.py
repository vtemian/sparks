"""What two accounts sharing one box need from the filesystem: directories both
can write, an id neither can claim twice, a lock both can take, and text that is
safe to persist. Each function is the one boundary where its fix is applied."""

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sparks.run import new_run_id

DIR_MODE = 0o2775
"""setgid, group-writable. 2775 and not 2770: at 2770 the unprivileged scraper
account cannot read a summary."""

FILE_MODE = 0o664
MAX_TEXT = 256


def make_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create a directory both group members can always use. `mode` applies to
    `path` alone; parents created on the way get the default."""
    if not path.parent.is_dir():
        make_dir(path.parent)
    with contextlib.suppress(FileExistsError):
        path.mkdir()
    # Separate from the mkdir, whose mode the caller's umask masks and chmod's
    # does not. EPERM when another user owns the directory, which is not ours.
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
    """Claim an id by creating its directory, which is the collision check:
    `mkdir` is atomic, so two users starting in the same second cannot share."""
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
    """Serialise the index's read-modify-write across both users. The directory
    itself is locked, never a lock file the other user could not open."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        grab(fd, directory, timeout)
        yield
    finally:
        os.close(fd)  # releases the lock


def grab(fd: int, directory: Path, timeout: float) -> None:
    """Poll for the lock rather than block on it: a blocking `flock` has no
    deadline, so a wedged holder would hang every later rebuild forever."""
    now = time.monotonic()
    deadline = now + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                msg = f"{directory} was locked for over {timeout:g}s"
                raise TimeoutError(msg) from None
            time.sleep(0.05)
        else:
            return


def clean(value: str, fallback: str = "unknown", limit: int = MAX_TEXT) -> str:
    """Make text safe to persist and to carry as a Prometheus label value.

    argv arrives surrogateescape-decoded, and a lone surrogate reaching a later
    `f.write()` raises `UnicodeEncodeError` and freezes the shared index."""
    text = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return text[:limit] or fallback
