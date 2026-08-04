import pytest

from tests.check_dashboard import CheckFailed, check, metric_names, substitute


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
