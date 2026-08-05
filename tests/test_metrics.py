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
        "training_progress",
        "training_eta_seconds",
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


def test_progress_metrics_are_not_lifecycle() -> None:
    assert "training_progress" in METRICS
    assert "training_eta_seconds" in METRICS
    assert "training_progress" not in LIFECYCLE
    assert "training_eta_seconds" not in LIFECYCLE


def test_run_active_is_declared_but_deliberately_not_lifecycle() -> None:
    # Measured on the box: training_run_info is LIFECYCLE-exempt from stale
    # marking, so it persists for the full 5m lookback. A 48s run's info series
    # ended 293s past the end, while stale-marked training_loss ended 12s
    # before it. An annotation built on info draws a region 7x the run.
    # training_run_active exists to be stale-marked, so the region is exact.
    assert "training_run_active" in METRICS
    assert "training_run_active" not in LIFECYCLE


def test_the_run_index_families_are_declared() -> None:
    # check_dashboard.py uses METRICS as its allowlist, so the overview
    # dashboard's queries fail the gate unless these are declared.
    for name in (
        "sparks_run_info",
        "sparks_run_start_timestamp_seconds",
        "sparks_run_duration_seconds",
        "sparks_run_energy_joules",
        "sparks_run_marginal_energy_joules",
        "sparks_run_gpu_nvml_energy_joules",
        "sparks_run_gpu_firmware_energy_joules",
        "sparks_run_final_loss",
    ):
        assert name in METRICS
