import os
from pathlib import Path

import pytest

from sparks import box

CONTRACT = """
shared_dir     = "{shared}"
shared_group   = "spark"
textfile_dir   = "{textfile}"
prometheus_url = "http://127.0.0.1:9090"
grafana_url    = "http://spark.local"
registry_url   = "http://spark.local:5000"
"""


def a_box(tmp_path: Path, *, runs: bool = True, textfile: bool = True) -> Path:
    """A contract file describing a provisioned box, with the directories it
    claims actually present unless a test asks otherwise."""
    shared = tmp_path / "srv" / "spark"
    tiles = tmp_path / "textfile"
    if runs:
        (shared / "runs").mkdir(parents=True)
    if textfile:
        tiles.mkdir()
    path = tmp_path / "box.toml"
    path.write_text(CONTRACT.format(shared=shared, textfile=tiles))
    return path


def test_registry_url_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "box.toml"
    path.write_text(
        'shared_dir = "/srv/spark"\nshared_group = "spark"\n'
        'textfile_dir = "/var/lib/node_exporter/textfile"\n'
        'prometheus_url = "http://127.0.0.1:9090"\n'
        'grafana_url = "http://spark.local"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(path))
    with pytest.raises(box.Malformed, match="registry_url"):
        box.load()


def test_registry_url_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "box.toml"
    path.write_text(
        'shared_dir = "/srv/spark"\nshared_group = "spark"\n'
        'textfile_dir = "/var/lib/node_exporter/textfile"\n'
        'prometheus_url = "http://127.0.0.1:9090"\n'
        'grafana_url = "http://spark.local"\n'
        'registry_url = "http://spark.local:5000"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(path))
    loaded = box.load()
    assert loaded is not None
    assert loaded.registry_url == "http://spark.local:5000"


def test_a_missing_contract_reads_as_an_unprovisioned_box(tmp_path: Path) -> None:
    # Not an exception: "no contract" is a normal answer that the caller decides
    # what to do about. Only the CLI turns it into a refusal.
    assert box.load(tmp_path / "absent.toml") is None


def test_a_contract_is_read_into_paths_and_urls(tmp_path: Path) -> None:
    loaded = box.load(a_box(tmp_path))
    assert loaded is not None
    assert loaded.shared_dir == tmp_path / "srv" / "spark"
    assert loaded.textfile_dir == tmp_path / "textfile"
    assert loaded.prometheus_url == "http://127.0.0.1:9090"
    assert loaded.grafana_url == "http://spark.local"
    assert loaded.registry_url == "http://spark.local:5000"
    assert loaded.shared_group == "spark"


def test_malformed_toml_names_the_file_rather_than_reading_as_absent(
    tmp_path: Path,
) -> None:
    # Absent means "never provisioned, go run sparkup". A file that exists but
    # does not parse means someone edited it, and saying "not provisioned"
    # would send them to fix the wrong thing.
    path = tmp_path / "box.toml"
    path.write_text('shared_dir = "unterminated\n')
    with pytest.raises(box.Malformed) as e:
        box.load(path)
    assert str(path) in str(e.value)


def test_a_contract_missing_a_field_is_malformed_not_defaulted(tmp_path: Path) -> None:
    path = tmp_path / "box.toml"
    path.write_text('shared_dir = "/srv/spark"\n')
    with pytest.raises(box.Malformed) as e:
        box.load(path)
    assert "textfile_dir" in str(e.value)


def test_a_provisioned_box_passes_preflight(tmp_path: Path) -> None:
    loaded = box.load(a_box(tmp_path))
    assert loaded is not None
    assert box.preflight(loaded) == []


def test_a_contract_promising_a_runs_directory_that_is_absent_complains(
    tmp_path: Path,
) -> None:
    loaded = box.load(a_box(tmp_path, runs=False))
    assert loaded is not None
    complaints = box.preflight(loaded)
    assert len(complaints) == 1
    # Names the directory, not the contract: the contract is right and the box
    # is wrong, so pointing at box.toml would send the reader to the wrong file.
    assert str(loaded.shared_dir / "runs") in complaints[0]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to read-only directories")
def test_a_runs_directory_i_cannot_write_to_complains(tmp_path: Path) -> None:
    loaded = box.load(a_box(tmp_path))
    assert loaded is not None
    runs = loaded.shared_dir / "runs"
    runs.chmod(0o555)
    try:
        complaints = box.preflight(loaded)
    finally:
        runs.chmod(0o755)
    assert len(complaints) == 1
    assert "not writable" in complaints[0]


def test_an_absent_textfile_directory_complains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The suite-wide override in conftest.py has to come off: it points at a
    # directory that exists, which is the whole thing under test here.
    monkeypatch.delenv("SPARKS_TEXTFILE_DIR")
    loaded = box.load(a_box(tmp_path, textfile=False))
    assert loaded is not None
    complaints = box.preflight(loaded)
    assert len(complaints) == 1
    assert str(loaded.textfile_dir) in complaints[0]


def test_both_directories_broken_reports_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One complaint per problem. Fixing the first and re-running to discover the
    # second is the loop this avoids.
    monkeypatch.delenv("SPARKS_TEXTFILE_DIR")
    loaded = box.load(a_box(tmp_path, runs=False, textfile=False))
    assert loaded is not None
    assert len(box.preflight(loaded)) == 2


def test_the_contract_path_is_overridable_for_a_box_sparkup_does_not_manage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(a_box(tmp_path)))
    loaded = box.load()
    assert loaded is not None
    assert loaded.shared_dir == tmp_path / "srv" / "spark"


def test_the_textfile_override_wins_over_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tests/conftest.py sets this for the whole suite so no test writes into the
    # box's real index. It has to beat a contract, or a developer's own box
    # would pull the suite back onto the real directory.
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(a_box(tmp_path)))
    monkeypatch.setenv("SPARKS_TEXTFILE_DIR", str(elsewhere))
    assert box.textfile_dir() == elsewhere


def test_the_textfile_directory_comes_from_the_contract_when_unoverridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(a_box(tmp_path)))
    monkeypatch.delenv("SPARKS_TEXTFILE_DIR", raising=False)
    assert box.textfile_dir() == tmp_path / "textfile"


def test_an_unprovisioned_box_has_nowhere_to_put_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old code answered `<shared_dir>/index` here, a directory nothing
    # scrapes. Raising is the point of this change: the caller either warns or
    # refuses, and either is better than writing where no one reads.
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("SPARKS_TEXTFILE_DIR", raising=False)
    with pytest.raises(box.NotProvisioned):
        box.textfile_dir()


def test_preflight_ignores_a_writable_directory_it_does_not_own(
    tmp_path: Path,
) -> None:
    # Group-writable and owned by someone else is the normal case on a shared
    # box: every user's runs land in one tree. Requiring ownership would fail
    # every account but the first.
    loaded = box.load(a_box(tmp_path))
    assert loaded is not None
    (loaded.shared_dir / "runs").chmod(0o2775)
    assert box.preflight(loaded) == []
    assert os.access(loaded.shared_dir / "runs", os.W_OK)
