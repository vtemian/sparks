import contextlib
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sparks.run import new_run_id

DIR_MODE = 0o2775

FILE_MODE = 0o664
MAX_TEXT = 256


def make_dir(path: Path, mode: int = DIR_MODE) -> Path:
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
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        grab(fd, directory, timeout)
        yield
    finally:
        os.close(fd)  # releases the lock


def grab(fd: int, directory: Path, timeout: float) -> None:
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
    text = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return text[:limit] or fallback
