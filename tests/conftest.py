"""Test-wide fixtures.

The one here is a safety belt, not a convenience: `launcher.textfile_dir`
prefers node_exporter's real directory over `shared_dir/index` whenever that
directory exists, which it does on the box. Without this override, a unit test
that launches a run rewrites the box's production `sparks_runs.prom` from
pytest's `tmp_path`, so `make check` on the box destroys the real run index.
Pointing the override inside `tmp_path` for every test closes that.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_textfile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKS_TEXTFILE_DIR", str(tmp_path / "index"))
