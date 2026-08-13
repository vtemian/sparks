from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gates.check_file_header import SRC, is_header_comment, violations_in

if TYPE_CHECKING:
    from pathlib import Path


def test_shebang_is_allowed() -> None:
    assert not is_header_comment("#!/usr/bin/env python3", lineno=1)


def test_hash_comment_is_banned() -> None:
    assert is_header_comment("# Not in the shared tree", lineno=5)


def test_prose_after_def_is_out_of_scope(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text('"""doc"""\n\ndef f() -> None:\n    # why\n    return\n')
    assert violations_in(path) == []


def test_comment_before_first_def_is_caught(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text('"""doc"""\n\n# bad\nX = 1\n\ndef f() -> None:\n    return\n')
    assert len(violations_in(path)) == 1


@pytest.mark.parametrize(
    "path",
    sorted(SRC.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC)),
)
def test_src_headers_have_no_hash_comments(path: Path) -> None:
    assert violations_in(path) == []
