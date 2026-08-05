"""Test-wide fixtures.

Both of these are safety belts, not conveniences. They exist because the box
this code runs on is also a box someone runs `make check` on, and a unit test
that reaches the real provisioning damages it:

- `SPARKS_TEXTFILE_DIR` keeps the run index inside `tmp_path`. Without it, a
  test that launches a run rewrites the box's production `sparks_runs.prom`, so
  `make check` on the box destroys the real run index.
- `SPARKS_BOX_CONFIG` points at a path that does not exist, so every test sees
  an unprovisioned box unless it says otherwise. Without it the suite would
  read `/etc/sparks/box.toml` on a provisioned box and record runs into the
  real shared tree.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture
def textfile_dir(isolate_the_box: None) -> Path:
    """Where the index landed during this test. Ask for it rather than rebuilding
    the path by hand: the autouse fixture below decides it, and a test that
    hardcodes the location breaks when that changes. Depends on that fixture
    explicitly rather than trusting autouse ordering."""
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
