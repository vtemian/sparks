from pathlib import Path

from sparks.fire import control


def test_queue_dir_uses_shared_dir(tmp_path: Path) -> None:
    q = tmp_path / "queue"
    q.mkdir()
    assert control.queue_dir(tmp_path) == q
