from sparks.metrics import LIFECYCLE, METRICS
from sparks.series import NAME


def test_every_declared_metric_is_a_valid_prometheus_name() -> None:
    # The dashboard checker trusts METRICS as its allowlist while Series
    # validates at emit time, so an invalid name here is a metric that is
    # dashboardable and unemittable: a panel that passes the gate and is
    # permanently empty on the box.
    assert METRICS
    for name in METRICS:
        assert NAME.match(name), name


def test_the_names_the_dashboard_depends_on_are_declared() -> None:
    for name in (
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_heartbeat_timestamp_seconds",
        "training_run_end_timestamp_seconds",
        "training_run_status",
        "training_loss",
        "training_step",
        "training_epoch",
    ):
        assert name in METRICS


def test_the_lifecycle_set_is_part_of_the_registry() -> None:
    # A name in LIFECYCLE but not METRICS would be exempt from stale marking
    # and rejected by log(), which is a contradiction nobody would notice.
    assert set(METRICS) >= LIFECYCLE


def test_the_lifecycle_set_is_not_everything() -> None:
    # If it were, no series would ever be marked stale and a finished run would
    # hold its last loss on the graph for the whole lookback window.
    assert set(METRICS) - LIFECYCLE
