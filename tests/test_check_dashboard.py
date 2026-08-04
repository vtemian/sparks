import pytest

from tests.check_dashboard import (
    ROOT,
    CheckFailed,
    check,
    dashboards,
    expressions,
    metric_names,
    substitute,
)


def test_variables_are_substituted_not_rejected() -> None:
    assert "$" not in substitute('training_loss{run_id=~"$run_id"}')


def test_rate_interval_is_substituted() -> None:
    assert "$" not in substitute("rate(training_step[$__rate_interval])")


def test_an_unknown_variable_is_an_error() -> None:
    with pytest.raises(CheckFailed):
        substitute('training_loss{x=~"$nope"}')


def test_the_metric_inside_a_join_group_is_found() -> None:
    # The shape every joined panel uses. A naive "identifier followed by
    # something" scan misses training_run_info here, because it is followed by
    # a closing paren, and the checker then silently skips it.
    found = metric_names(
        'training_loss{run_id=~"r"} * on(run_id) group_left(run_name, git_sha) '
        "max by (run_id, run_name, git_sha) (training_run_info)"
    )
    assert found == {"training_loss", "training_run_info"}


def test_label_names_are_not_mistaken_for_metrics() -> None:
    assert metric_names('node_hwmon_power_watt{label="sys_total"}') == {
        "node_hwmon_power_watt"
    }


def test_the_shipped_dashboard_passes() -> None:
    check()


def test_a_panel_querying_an_undeclared_metric_fails() -> None:
    with pytest.raises(CheckFailed):
        check(extra_exprs=["training_invented_metric"])


def test_a_metric_named_from_inside_the_label_set_is_found() -> None:
    # `{__name__="x"}` is a legal way to name a metric. Stripping the label set
    # before reading it reduces the whole expression to nothing, so a panel
    # querying an invented metric passes with an empty result set.
    assert metric_names('{__name__="totally_fake_metric"}') == {"totally_fake_metric"}
    assert metric_names('{__name__=~"training_invented.*"}') == {"training_invented.*"}
    assert "fake_metric" in metric_names(
        'training_loss{run_id=~"r"} or {__name__="fake_metric"}'
    )


def test_a_panel_hidden_in_a_collapsed_row_is_still_checked() -> None:
    # Grafana moves a row's children inside the row panel when it is collapsed,
    # so a one-level walk silently stops seeing them after a UI round-trip.
    collapsed = {
        "panels": [
            {
                "type": "row",
                "collapsed": True,
                "panels": [{"targets": [{"expr": "training_invented_metric"}]}],
            }
        ]
    }
    assert expressions(collapsed) == ["training_invented_metric"]


def test_the_variable_query_is_checked_too() -> None:
    # A typo here ships an empty Run dropdown and a blank dashboard, with every
    # panel query still passing.
    board = {
        "panels": [],
        "templating": {
            "list": [{"definition": "label_values(training_run_inf, run_id)"}]
        },
    }
    assert expressions(board) == ["training_run_inf"]


def test_every_shipped_dashboard_is_checked_not_just_one() -> None:
    # sparkup's equivalent hardcodes a single path, which is how a second board
    # ships unvalidated. Ours must find them all.
    found = {p.name for p in dashboards()}
    assert "training-runs.json" in found
    assert len(found) == len(list(dashboards()))
    assert found == {p.name for p in (ROOT / "dashboards").glob("*.json")}


def test_the_annotation_query_is_checked_too() -> None:
    # An annotation naming a metric nobody emits draws no region and fails
    # nothing else.
    board = {
        "panels": [],
        "annotations": {
            "list": [
                {
                    "datasource": {"type": "prometheus", "uid": "prometheus"},
                    "target": {"expr": "training_run_invented"},
                }
            ]
        },
    }
    assert "training_run_invented" in expressions(board)


def test_a_grafana_builtin_annotation_is_not_treated_as_promql() -> None:
    board = {
        "panels": [],
        "annotations": {
            "list": [
                {
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "target": {"type": "dashboard", "limit": 100},
                }
            ]
        },
    }
    assert expressions(board) == []
