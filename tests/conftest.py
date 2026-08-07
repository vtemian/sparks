import os
from pathlib import Path

import pytest


@pytest.fixture
def textfile_dir(isolate_the_box: None) -> Path:
    return Path(os.environ["SPARKS_TEXTFILE_DIR"])


@pytest.fixture(autouse=True)
def isolate_the_box(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Created rather than merely named, because box.preflight quite rightly
    # complains about a directory that is not there. Off in its own temporary
    # directory rather than inside `tmp_path`, so that tests asserting on the
    # exact contents of their own tmp_path - "the atomic write left no temp
    # file behind" - do not have to know this fixture exists.
    monkeypatch.setenv("SPARKS_TEXTFILE_DIR", str(tmp_path_factory.mktemp("textfile")))
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(tmp_path / "no-such-box.toml"))
