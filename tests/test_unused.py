from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from gates.check_unused import SRC, findings

if TYPE_CHECKING:
    from pathlib import Path


def test_an_unused_function_and_constant_are_reported(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text(
        "def never_called() -> None:\n    return None\n\n\nUNUSED_CONST = 1\n",
        encoding="utf-8",
    )
    reported = "\n".join(findings([tmp_path]))
    assert "never_called" in reported
    assert "UNUSED_CONST" in reported


def test_only_the_first_path_is_held_to_account(tmp_path: Path) -> None:
    source = tmp_path / "src"
    suite = tmp_path / "tests"
    source.mkdir()
    suite.mkdir()
    (source / "mod.py").write_text("dead_in_source = 1\n", encoding="utf-8")
    (suite / "mod.py").write_text("dead_in_suite = 1\n", encoding="utf-8")
    reported = "\n".join(findings([source, suite]))
    assert "dead_in_source" in reported
    assert "dead_in_suite" not in reported


def test_an_ignored_name_is_dropped_and_its_neighbour_kept(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text(
        "idle_gpu_watts = 1\nbusy_gpu_watts = 2\n",
        encoding="utf-8",
    )
    reported = "\n".join(findings([tmp_path]))
    assert "busy_gpu_watts" in reported
    assert "idle_gpu_watts" not in reported


def test_a_tool_failure_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = subprocess.CompletedProcess(
        args=["vulture"], returncode=1, stdout="", stderr="no module named vulture"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: broken)

    with pytest.raises(RuntimeError, match="vulture failed: no module named vulture"):
        findings()


def test_dead_code_found_is_not_a_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = f"{SRC}/emit.py:1: unused function 'gone' (60% confidence)"
    found = subprocess.CompletedProcess(
        args=["vulture"], returncode=3, stdout=f"{hit}\n", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: found)

    assert findings() == [hit]


def test_src_has_no_unused_code() -> None:
    hits = findings()
    assert hits == [], "\n".join(hits)
