import pytest

from sparks.series import InvalidLabel, Series


def test_labels_are_sorted_and_hashable() -> None:
    a = Series("training_loss", {"seed": "0", "run_id": "r1"})
    b = Series("training_loss", {"run_id": "r1", "seed": "0"})
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_label_names_become_prometheus_labels() -> None:
    s = Series("training_loss", {"run_id": "r1"})
    assert s.as_metric() == {"__name__": "training_loss", "run_id": "r1"}


def test_rejects_a_label_name_prometheus_would_drop() -> None:
    # Prometheus drops these silently and still answers 200, so this is the
    # only place the mistake is ever visible.
    with pytest.raises(InvalidLabel):
        Series("training_loss", {"run-id": "r1"})


def test_rejects_a_reserved_label_name() -> None:
    with pytest.raises(InvalidLabel):
        Series("training_loss", {"__name__": "nope"})


def test_rejects_an_invalid_metric_name() -> None:
    with pytest.raises(InvalidLabel):
        Series("training loss", {})
