from __future__ import annotations

from tests.check_unused import findings


def test_src_has_no_unused_code() -> None:
    assert findings() == []
