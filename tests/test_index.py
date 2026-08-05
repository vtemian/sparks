import re
import stat
from pathlib import Path
from typing import Any

import pytest

from sparks.index import FILENAME, rebuild, render
from sparks.summary import Energy, Summary, save
from tests import promtool
from tests.test_summary import a_summary

SECOND = {
    "run_id": "run-20260805-1500-e1",
    "run_name": "e1",
    "user": "ana",
    "status": "crashed",
    "started_unix": 1785849000.0,
    "ended_unix": 1785849012.5,
    "duration_seconds": 12.5,
    "exit_code": 3,
    "final_loss": None,
    "energy": Energy(
        total_joules=400.0,
        marginal_joules=200.0,
        gpu_nvml_joules=100.0,
        gpu_firmware_joules=122.0,
        idle_watts=13.0,
        idle_gpu_watts=3.8,
        window_seconds=14.0,
        baseline_seconds=60.0,
        gpu_sources="agree",
    ),
}

# Every rule this file exists to obey is visible here: one TYPE per family,
# before that family's first sample, so the samples of a family are grouped
# rather than a header repeated per run; `gauge` for the info metric, because
# `# TYPE ... info` is rejected outright; no explicit timestamps, which would
# make the collector skip the whole file; and a trailing newline, without which
# node_exporter drops it.
EXPECTED = (
    "\n".join(
        [
            "# HELP sparks_run_info Identity of a completed training run. Always 1.",
            "# TYPE sparks_run_info gauge",
            'sparks_run_info{run_id="run-20260805-1420-e0",user="vlad"'
            ',run_name="e0",status="finished",energy_sources="agree"} 1',
            'sparks_run_info{run_id="run-20260805-1500-e1",user="ana"'
            ',run_name="e1",status="crashed",energy_sources="agree"} 1',
            "# HELP sparks_run_start_timestamp_seconds Unix time the run started.",
            "# TYPE sparks_run_start_timestamp_seconds gauge",
            'sparks_run_start_timestamp_seconds{run_id="run-20260805-1420-e0"}'
            " 1785847319",
            'sparks_run_start_timestamp_seconds{run_id="run-20260805-1500-e1"}'
            " 1785849000",
            "# HELP sparks_run_duration_seconds Wall-clock duration of the run.",
            "# TYPE sparks_run_duration_seconds gauge",
            'sparks_run_duration_seconds{run_id="run-20260805-1420-e0"} 48.02',
            'sparks_run_duration_seconds{run_id="run-20260805-1500-e1"} 12.5',
            "# HELP sparks_run_energy_window_seconds Window the energy counters"
            " bracketed, wider than the run's duration.",
            "# TYPE sparks_run_energy_window_seconds gauge",
            'sparks_run_energy_window_seconds{run_id="run-20260805-1420-e0"} 52',
            'sparks_run_energy_window_seconds{run_id="run-20260805-1500-e1"} 14',
            "# HELP sparks_run_energy_joules Total energy drawn over the run.",
            "# TYPE sparks_run_energy_joules gauge",
            'sparks_run_energy_joules{run_id="run-20260805-1420-e0"} 1810',
            'sparks_run_energy_joules{run_id="run-20260805-1500-e1"} 400',
            "# HELP sparks_run_marginal_energy_joules Energy above idle,"
            " attributable to the run.",
            "# TYPE sparks_run_marginal_energy_joules gauge",
            'sparks_run_marginal_energy_joules{run_id="run-20260805-1420-e0"} 1186',
            'sparks_run_marginal_energy_joules{run_id="run-20260805-1500-e1"} 200',
            "# HELP sparks_run_idle_watts Idle baseline power measured before the run.",
            "# TYPE sparks_run_idle_watts gauge",
            'sparks_run_idle_watts{run_id="run-20260805-1420-e0"} 13',
            'sparks_run_idle_watts{run_id="run-20260805-1500-e1"} 13',
            "# HELP sparks_run_gpu_nvml_energy_joules GPU energy as NVML measures it.",
            "# TYPE sparks_run_gpu_nvml_energy_joules gauge",
            'sparks_run_gpu_nvml_energy_joules{run_id="run-20260805-1420-e0"} 543',
            'sparks_run_gpu_nvml_energy_joules{run_id="run-20260805-1500-e1"} 100',
            "# HELP sparks_run_gpu_firmware_energy_joules GPU energy at the"
            " firmware rail.",
            "# TYPE sparks_run_gpu_firmware_energy_joules gauge",
            'sparks_run_gpu_firmware_energy_joules{run_id="run-20260805-1420-e0"} 663',
            'sparks_run_gpu_firmware_energy_joules{run_id="run-20260805-1500-e1"} 122',
            "# HELP sparks_run_final_loss Training loss last reported by the run.",
            "# TYPE sparks_run_final_loss gauge",
            'sparks_run_final_loss{run_id="run-20260805-1420-e0"} 0.412',
        ]
    )
    + "\n"
)


def two_runs() -> list[Summary]:
    return [a_summary(), a_summary(**SECOND)]


def samples(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_the_index_is_exactly_this_text() -> None:
    assert render(two_runs()) == EXPECTED


def test_the_file_ends_with_a_newline() -> None:
    # Without it node_exporter drops the entire file, not just the last row.
    assert render(two_runs()).endswith("\n")


def test_runs_are_ordered_by_id_whatever_order_they_arrive_in() -> None:
    assert render(reversed(two_runs())) == EXPECTED


def test_each_family_is_typed_once_before_its_own_grouped_samples() -> None:
    text = render(two_runs())
    lines = text.splitlines()
    for name in {line.split()[2] for line in lines if line.startswith("# TYPE ")}:
        types = [i for i, line in enumerate(lines) if line == f"# TYPE {name} gauge"]
        rows = [i for i, line in enumerate(lines) if line.startswith(f"{name}{{")]
        assert len(types) == 1, name
        assert types[0] < rows[0], name
        # Contiguous, so no header can be repeated between two samples.
        assert rows == list(range(rows[0], rows[0] + len(rows))), name


def test_no_family_is_declared_info() -> None:
    # `# TYPE ... info` is rejected by node_exporter 1.12.1: only counter,
    # gauge, summary, untyped and histogram exist. An info metric is a gauge.
    assert "# TYPE sparks_run_info gauge" in render(two_runs())
    assert " info" not in "\n".join(
        line for line in render(two_runs()).splitlines() if line.startswith("# TYPE")
    )


def test_every_family_is_a_gauge_and_never_a_counter() -> None:
    # These are final measurements. rate() over them is nonsense, and a _total
    # suffix would invite exactly that.
    types = [
        line for line in render(two_runs()).splitlines() if line.startswith("# TY")
    ]
    assert types
    assert all(line.endswith(" gauge") for line in types)
    assert not any("_total" in line for line in types)


def test_label_values_escape_only_backslash_quote_and_newline() -> None:
    text = render([a_summary(run_name='a"b\\c\nd')])
    assert 'run_name="a\\"b\\\\c\\nd"' in text


def test_a_tab_in_a_label_value_is_passed_through_unescaped() -> None:
    # `\t` is not one of the three escapes the text format defines, and writing
    # one is a parse error that costs the whole file.
    text = render([a_summary(user="a\tb")])
    assert 'user="a\tb"' in text
    assert "\\t" not in text


def test_no_sample_carries_an_explicit_timestamp() -> None:
    # An explicit timestamp makes the textfile collector skip the entire file,
    # so a finished run can never be backdated to when it actually ran.
    for line in samples(render(two_runs())):
        assert len(line.rsplit("}", 1)[1].split()) == 1, line


def test_a_non_terminal_run_is_left_out(tmp_path: Path) -> None:
    # A run written as `running` and then rewritten as `finished` stales one
    # series and creates another, doubling churn for nothing.
    live = a_summary(run_id="run-20260805-1600-e2", run_name="e2", status="running")
    assert render([live]) == ""
    assert render([*two_runs(), live]) == EXPECTED


def test_a_family_no_run_has_a_value_for_is_not_declared_at_all() -> None:
    text = render([a_summary(final_loss=None)])
    assert "sparks_run_final_loss" not in text
    assert "sparks_run_duration_seconds" in text


def test_an_empty_index_is_empty_rather_than_headers_only() -> None:
    assert render([]) == ""


def test_rebuild_reads_every_run_directory(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for run in two_runs():
        save(run, runs / run.run_id)
    target = tmp_path / FILENAME

    assert rebuild(runs, target) == 2
    assert target.read_text() == EXPECTED


def test_the_written_index_is_world_readable(tmp_path: Path) -> None:
    # mkstemp creates 0600 and renaming that into place produces exactly the
    # unreadable file node_exporter skips without a word. This is the trap.
    runs = tmp_path / "runs"
    save(a_summary(), runs / "run-20260805-1420-e0")
    target = tmp_path / FILENAME

    rebuild(runs, target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_rebuild_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    save(a_summary(), runs / "run-20260805-1420-e0")
    target = tmp_path / "textfile" / FILENAME
    target.parent.mkdir()

    rebuild(runs, target)

    assert list(target.parent.iterdir()) == [target]


def test_rebuild_skips_a_summary_it_cannot_read(tmp_path: Path) -> None:
    # One corrupt file must not cost every other run its row, because the index
    # is rebuilt at the end of a run and an exception here would surface as the
    # run itself failing.
    runs = tmp_path / "runs"
    save(a_summary(), runs / "run-20260805-1420-e0")
    (runs / "run-20260805-1500-e1").mkdir()
    (runs / "run-20260805-1500-e1" / "summary.json").write_text("{ not json")
    target = tmp_path / FILENAME

    assert rebuild(runs, target) == 1
    assert "run-20260805-1420-e0" in target.read_text()


def test_rebuild_over_a_missing_runs_directory_writes_an_empty_index(
    tmp_path: Path,
) -> None:
    target = tmp_path / FILENAME
    assert rebuild(tmp_path / "runs", target) == 0
    assert target.read_text() == ""


@pytest.mark.skipif(not promtool.usable(), reason=promtool.REASON)
def test_promtool_accepts_the_rendered_index() -> None:
    weird: dict[str, Any] = {
        "run_id": "run-20260805-1600-e2",
        "run_name": 'a"b\\c\nd',
        "user": "a\tb",
    }
    done = promtool.check_metrics(render([*two_runs(), a_summary(**weird)]))
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_metric_names_are_the_ones_the_dashboard_queries() -> None:
    # Joules, never watt-hours: promtool warns on watt-hours and Prometheus
    # naming guidance is base units. Divide by 3600 in the panel.
    text = render(two_runs())
    assert "_watt_hours" not in text
    assert re.findall(r"^sparks_run_\w+", text, re.MULTILINE)
