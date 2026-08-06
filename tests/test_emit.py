import struct
from typing import Any

import pytest

from sparks.emit import RunMetrics, from_env
from sparks.metrics import LIFECYCLE
from sparks.series import InvalidLabel


def names(drained: list[dict[str, Any]]) -> set[str]:
    return {d["metric"]["__name__"] for d in drained}


def make(**kw: Any) -> RunMetrics:
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
        "training_run_active",
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


def test_shutdown_is_idempotent() -> None:
    # Must run with the pump started. With autostart=False `_stop` is None and
    # _shutdown returns on its first line, so the guard this test exists for is
    # never reached and the test passes having exercised nothing. The second
    # call is not hypothetical: _start registers _shutdown with atexit, so every
    # real run calls it twice.
    m = RunMetrics(run_id="run-1", url="http://127.0.0.1:1", autostart=True)
    m.begin()
    assert m._thread is not None and m._thread.is_alive()
    m.end("finished")
    assert not m._thread.is_alive(), "the pump thread should have stopped"
    m.end("finished")  # must not raise, and must not restart anything
    assert not m._thread.is_alive()


def test_a_push_failure_does_not_propagate() -> None:
    # A metrics outage must never kill a training run. The URL is unroutable.
    m = RunMetrics(run_id="run-1", url="http://127.0.0.1:1", autostart=True)
    m.begin()
    m.log(loss=0.5)
    m.end("finished")  # must not raise


def test_the_run_record_is_never_marked_stale() -> None:
    # end() writes the status and end-time samples, and _mark_stale runs a
    # millisecond later over everything the buffer has seen. Without the
    # lifecycle exemption it erases them immediately and training_run_status
    # never resolves. The live test caught this; this pins it.
    m = make()
    m.begin()
    m.log(loss=0.5)
    m._buffer.drain()  # what the pump would have sent, so seen() is populated
    m.end("finished")
    m._buffer.drain()

    # Assert on the batch _mark_stale actually builds. Re-deriving the filter
    # in the test would only check the test's own arithmetic, and would still
    # pass with the exemption deleted from the implementation.
    staled = {d["metric"]["__name__"] for d in m._stale_batch()}
    assert "training_loss" in staled
    assert not staled & LIFECYCLE, f"the run's own record was staled: {staled}"


def test_a_stale_marker_lands_after_the_sample_it_ends() -> None:
    # Sharing a millisecond with the last real sample is a duplicate-timestamp
    # 400, and remote-write rolls back the whole request, so one collision
    # means no series gets a marker at all.
    m = make()
    m.log(loss=0.5)
    m._buffer.drain()
    last = {s: ts for s, ts in m._buffer.seen().items() if s.name == "training_loss"}
    ((series, sent_at),) = last.items()
    marker = next(
        d for d in m._stale_batch() if d["metric"]["__name__"] == "training_loss"
    )
    assert marker["timestamps"][0] > sent_at
    assert series.name == "training_loss"


def test_an_invalid_label_is_refused_on_the_caller_thread() -> None:
    # Not five seconds later on the pump, where it would kill telemetry for the
    # rest of the run behind a bare threading traceback.
    with pytest.raises(InvalidLabel):
        RunMetrics(run_id="r", url="http://unused", autostart=False, info={"g-s": "a"})
    with pytest.raises(InvalidLabel):
        RunMetrics(
            run_id="r", url="http://unused", autostart=False, labels={"a b": "c"}
        )


def test_run_active_is_emitted_and_gets_a_stale_marker() -> None:
    # It must be staled, unlike the info metric, or the annotation region
    # overshoots by the whole lookback window.
    m = make()
    m.begin()
    m._buffer.drain()
    assert "training_run_active" in {d["metric"]["__name__"] for d in m._stale_batch()}


def test_a_child_emitter_writes_no_lifecycle_series() -> None:
    # The supervisor and the training child both hold a RunMetrics for one
    # run_id. Two writers on the SAME series interleave timestamps, which is an
    # out-of-order 400, and remote-write 1.0 rolls back the whole request,
    # destroying the batch that carried training_loss.
    child = RunMetrics(
        run_id="run-1", url="http://unused", autostart=False, lifecycle=False
    )
    child.begin()
    child.log(loss=0.5)
    child.end("finished")
    written = names(child._buffer.drain())
    assert written == {"training_loss"}
    assert not written & LIFECYCLE


def test_from_env_is_a_no_op_outside_sparks_run(monkeypatch: Any) -> None:
    # The same training script must still run standalone without branching.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)
    assert from_env() is None


def test_from_env_builds_a_child_emitter(monkeypatch: Any) -> None:
    monkeypatch.setenv("SPARKS_RUN_ID", "run-7")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:9090")
    m = from_env(arm="real", autostart=False)
    assert m is not None
    assert m.run_id == "run-7"
    m.log(loss=1.0)
    sample = m._buffer.drain()[0]
    assert sample["metric"]["arm"] == "real"
    m.begin()
    assert m._buffer.drain() == []  # still no lifecycle series


def test_a_child_cannot_write_a_supervisor_series() -> None:
    # Byte-identical series from two processes is the out-of-order rollback the
    # whole split exists to prevent, and it is silent: the batch carrying the
    # loss simply never lands. Enforced in code, not by convention.
    child = RunMetrics(
        run_id="run-1", url="http://unused", autostart=False, lifecycle=False
    )
    with pytest.raises(KeyError):
        child.log(run_active=1.0)
    with pytest.raises(KeyError):
        child.log(run_start_timestamp_seconds=1000.0)


def test_a_standalone_run_may_still_write_both_halves() -> None:
    # A bare `RunMetrics` has no child and owns everything. The guard is
    # asymmetric for exactly this reason; making it symmetric breaks that.
    m = make()
    m.begin()
    m.log(loss=0.5)
    m.log_group("training_grad_norm", {"lora": 1.0})
    assert "training_loss" in names(m._buffer.drain())


def test_a_stale_marker_actually_carries_the_stale_nan() -> None:
    # Nothing else in the suite checks the payload. Change _stale_batch to emit
    # 1.0 and everything else still passes, while every killed run's loss curve
    # flat-lines for the whole lookback window instead of stopping dead.
    # It must be THE stale NaN, 0x7FF0000000000002, not any NaN: an ordinary
    # NaN is a value, and only this bit pattern ends the series.
    m = make()
    m.log(loss=0.5)
    m._buffer.drain()
    marker = next(
        d for d in m._stale_batch() if d["metric"]["__name__"] == "training_loss"
    )
    (value,) = marker["values"]
    assert struct.pack("<d", value) == struct.pack("<Q", 0x7FF0000000000002)
