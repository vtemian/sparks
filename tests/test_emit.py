from typing import Any

from sparks.emit import RunMetrics


def names(drained: list[dict[str, Any]]) -> set[str]:
    return {d["metric"]["__name__"] for d in drained}


def make(**kw: dict[str, str]) -> RunMetrics:
    # autostart=False keeps the pump thread out of the unit tests; the live
    # test in tests/test_live.py is what exercises the thread and the network.
    return RunMetrics(run_id="run-1", url="http://unused", autostart=False, **kw)


def test_begin_emits_identity_and_start_time() -> None:
    m = make(info={"model": "helium"})
    m.begin()
    out = m._buffer.drain()
    assert names(out) == {
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_heartbeat_timestamp_seconds",
    }


def test_info_carries_run_id_and_the_supplied_metadata() -> None:
    m = make(info={"model": "helium"})
    m.begin()
    info = next(
        d for d in m._buffer.drain() if d["metric"]["__name__"] == "training_run_info"
    )
    assert info["metric"]["run_id"] == "run-1"
    assert info["metric"]["model"] == "helium"
    assert info["values"] == [1.0]


def test_log_emits_one_series_per_named_value() -> None:
    m = make()
    m.log(step=3, loss=0.5)
    assert names(m._buffer.drain()) == {"training_step", "training_loss"}


def test_log_refuses_an_undeclared_metric() -> None:
    import pytest

    m = make()
    with pytest.raises(KeyError):
        m.log(not_a_real_metric=1.0)


def test_series_labels_land_on_every_sample() -> None:
    m = make(labels={"arm": "real", "seed": "0"})
    m.log(loss=0.5)
    loss = m._buffer.drain()[0]
    assert loss["metric"]["arm"] == "real"
    assert loss["metric"]["seed"] == "0"
    assert loss["metric"]["run_id"] == "run-1"


def test_end_emits_terminal_state_and_never_mutates_info() -> None:
    m = make()
    m.begin()
    m._buffer.drain()
    m.end("finished")
    out = m._buffer.drain()
    assert "training_run_end_timestamp_seconds" in names(out)
    status = next(d for d in out if d["metric"]["__name__"] == "training_run_status")
    assert status["metric"]["status"] == "finished"
    # The info metric must never be re-emitted with different labels: a second
    # label set means a second series and a red panel.
    infos = [d for d in out if d["metric"]["__name__"] == "training_run_info"]
    assert all("status" not in d["metric"] for d in infos)


def test_a_metric_labelled_by_group_keeps_the_group_label() -> None:
    m = make()
    m.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})
    out = m._buffer.drain()
    assert {d["metric"]["group"] for d in out} == {"lora", "tables"}
