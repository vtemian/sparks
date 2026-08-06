from pathlib import Path

import pytest

from sparks.fire import cli


def test_queue_lists_empty_shared_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["queue", "--shared-dir", str(tmp_path)]) == 0
    assert "empty" in capsys.readouterr().out


def test_reserve_prints_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["reserve", "--name", "e0", "--shared-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(tmp_path / "queue"))
