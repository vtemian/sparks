import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from sparks.emit import Run, RunMetrics, track
from sparks.metrics import LIFECYCLE
from sparks.series import InvalidLabelError


def names(drained: list[dict[str, Any]]) -> set[str]:
    return {d["metric"]["__name__"] for d in drained}


def child(**kw: Any) -> RunMetrics:
    # What track builds inside a job, minus the pump thread.
    return RunMetrics(
        run_id="run-9", url="http://unused", autostart=False, lifecycle=False, **kw
    )


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
    with pytest.raises(InvalidLabelError):
        RunMetrics(run_id="r", url="http://unused", autostart=False, info={"g-s": "a"})
    with pytest.raises(InvalidLabelError):
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


def test_track_outside_a_job_reaches_for_no_emitter_at_all(monkeypatch: Any) -> None:
    # The same training script must still run standalone without branching.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)

    assert track()._metrics is None


def test_track_inside_a_job_builds_a_child_carrying_its_labels(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("SPARKS_RUN_ID", "run-7")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")
    run = track(arm="real")

    assert run._metrics is not None
    assert run._metrics.run_id == "run-7"

    run.step(loss=1.0)
    sample = run._metrics._buffer.drain()[0]
    assert sample["metric"]["arm"] == "real"

    # Still a child: entering the block writes no lifecycle series.
    with run:
        assert run._metrics._buffer.drain() == []


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


def in_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKS_RUN_ID", "run-9")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://unused")


def nowhere_near_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)


def test_track_outside_a_job_yields_a_run_that_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same training script has to run on a laptop, and the whole point of
    # track is that the call site carries no `is not None` guard.
    nowhere_near_a_job(monkeypatch)

    with track(total=10) as run:
        run.step(loss=1.0)
        run.log(eval_loss=0.5)
        run.log_group("training_learning_rate", {"adapter": 2e-4})

    assert run._metrics is None
    assert run.count == 1


def test_track_inside_a_job_logs_through_a_child_emitter() -> None:
    emitter = child(labels={"arm": "lora"})
    run = Run(emitter)

    run.step(loss=0.25)

    drained = emitter._buffer.drain()
    assert "training_loss" in names(drained)
    assert drained[0]["metric"]["arm"] == "lora"


def ticking(*times: float) -> Callable[[], float]:
    values = iter(times)
    return lambda: next(values)


def test_a_tracked_step_derives_progress_from_the_total() -> None:
    emitter = child()
    run = Run(emitter, total=4)

    run.step(loss=1.0)

    drained = emitter._buffer.drain()
    progress = next(
        d for d in drained if d["metric"]["__name__"] == "training_progress"
    )
    assert progress["values"] == [0.25]


def test_without_a_total_progress_is_absent_rather_than_wrong() -> None:
    # A guessed denominator renders a progress bar that lies. Emitting nothing
    # is the honest failure.
    emitter = child()
    run = Run(emitter)

    run.step(loss=1.0)

    drained = emitter._buffer.drain()
    assert "training_progress" not in names(drained)
    assert "training_eta_seconds" not in names(drained)


def test_a_value_you_pass_wins_over_the_one_track_derived() -> None:
    emitter = child()
    run = Run(emitter, total=4)

    run.step(step=99.0)

    drained = emitter._buffer.drain()
    step = next(d for d in drained if d["metric"]["__name__"] == "training_step")
    assert step["values"] == [99.0]


def test_steps_per_second_is_measured_over_the_recent_window() -> None:
    run = Run(None, clock=ticking(0.0, 0.5))

    run.step()
    run.step()

    assert run.rate() == 2.0


def test_one_step_is_too_early_to_call_it_a_rate() -> None:
    # Seeding a mark at construction would give the first step a free
    # zero-length interval and report a rate several times the real one.
    run = Run(None, clock=ticking(0.0, 0.5))

    assert run.rate() is None

    run.step()

    assert run.rate() is None


def test_tokens_per_second_follows_the_step_rate() -> None:
    run = Run(None, tokens_per_step=1000, clock=ticking(0.0, 0.5))

    run.step()
    run.step()

    assert run.derived()["tokens_per_sec"] == 2000.0


def test_the_window_forgets_steps_older_than_itself() -> None:
    # Without maxlen the rate is a lifetime average, which is the bug this
    # replaces: a slow tail stays hidden behind a fast start.
    run = Run(None, window=2, clock=ticking(1.0, 2.0, 10.0))

    run.step()
    run.step()
    run.step()

    # One step in the eight seconds the window still remembers. Averaging over
    # the whole run instead would call it 0.22, hiding the stall.
    assert run.rate() == 0.125


def test_leaving_the_block_stops_the_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    # A child emitter's end() ignores its status -- the supervisor decides that
    # from how the process exited -- so all a Run owes is the stopped pump.
    monkeypatch.setenv("SPARKS_RUN_ID", "run-9")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")
    run = track()

    with run:
        run.step(loss=1.0)

    assert run._metrics is not None
    assert run._metrics._thread is not None
    assert not run._metrics._thread.is_alive()


def test_an_exception_leaves_the_block_without_being_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nowhere_near_a_job(monkeypatch)
    run = track()

    with pytest.raises(ValueError, match="boom"), run:
        raise ValueError("boom")

    assert run.count == 0


def test_eta_falls_as_the_run_advances() -> None:
    # Two steps a half-second apart is two per second, and eight of ten steps
    # remain, so four seconds.
    run = Run(None, total=10, clock=ticking(0.0, 0.5))

    run.step()
    run.step()

    assert run.derived()["eta_seconds"] == 4.0


def test_overrunning_the_total_does_not_report_more_than_all_of_it() -> None:
    # training_progress is declared 0-1 and the panel is a percentunit with a
    # max of 1, so an overrun renders as 167% complete and a negative eta.
    run = Run(None, total=3, clock=ticking(0.0, 1.0, 2.0, 3.0, 4.0))
    for _ in range(5):
        run.step()

    derived = run.derived()

    assert derived["progress"] == 1.0
    assert derived["eta_seconds"] == 0.0


def test_a_total_that_counts_backwards_is_refused() -> None:
    with pytest.raises(ValueError, match="total"):
        Run(None, total=-5)


def test_a_window_too_small_to_hold_an_interval_is_refused() -> None:
    # deque(maxlen=1) makes marks[0] and marks[-1] the same sample, so every
    # rate silently stays None for the life of the run.
    with pytest.raises(ValueError, match="window"):
        Run(None, window=1)


def test_no_keyword_is_reserved_out_from_under_a_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # track used to forward labels to from_env, so a label named autostart
    # bound to its argument and silently stopped the pump. track reads the
    # environment itself now, and every keyword it does not name is a label.
    monkeypatch.setenv("SPARKS_RUN_ID", "run-9")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")
    with track(autostart="no") as run:
        assert run._metrics is not None
        run.step(loss=1.0)

        assert run._metrics._buffer.drain()[0]["metric"]["autostart"] == "no"


def test_reporting_after_the_run_ended_says_so_rather_than_vanishing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing drains the buffer once the pump is stopped, and the whole point
    # of track is that the call site carries no guard, so a stray log after the
    # block is an easy mistake to make and an invisible one to debug.
    emitter = child()
    run = Run(emitter)
    run.end()

    run.step(loss=1.0)

    assert "already ended" in caplog.text
    assert emitter._buffer.drain() == []


def test_a_run_that_owns_its_lifecycle_records_a_start_and_a_crash() -> None:
    # track only ever builds children, where begin() and the status are both
    # ignored, so nothing else here would notice these two being deleted.
    emitter = make()
    run = Run(emitter)

    with pytest.raises(ValueError, match="boom"), run:
        emitter._buffer.drain()
        raise ValueError("boom")

    ended = {
        d["metric"]["__name__"]: d["metric"].get("status")
        for d in emitter._buffer.drain()
    }
    assert ended["training_run_status"] == "crashed"


def test_entering_the_block_writes_the_start_of_a_run_it_owns() -> None:
    emitter = make()

    with Run(emitter):
        assert "training_run_start_timestamp_seconds" in names(emitter._buffer.drain())


def test_every_way_of_reporting_stops_once_the_run_has_ended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # step returns before it reaches log, so a guard on log or log_group alone
    # would go unnoticed by a test that only calls step.
    emitter = child()
    run = Run(emitter)
    run.end()
    emitter._buffer.drain()

    run.step(loss=1.0)
    run.log(eval_loss=1.0)
    run.log_group("training_learning_rate", {"adapter": 1.0})

    assert emitter._buffer.drain() == []
    assert len([r for r in caplog.records if "already ended" in r.message]) == 1


def test_ending_a_run_twice_records_one_ending() -> None:
    # The sleep is the test: without it both endings share a millisecond and
    # the buffer drops the second, so a missing guard would look guarded.
    emitter = make()
    run = Run(emitter)
    run.end()
    time.sleep(0.002)

    run.end()

    # One dict per series carrying every sample, so count the values: counting
    # the dicts would read the same whether it ended once or twice.
    status = next(
        d
        for d in emitter._buffer.drain()
        if d["metric"]["__name__"] == "training_run_status"
    )
    assert len(status["values"]) == 1


def test_an_undeclared_name_is_refused_off_the_box_too() -> None:
    # Checking only when there is an emitter is how a typo passes every laptop
    # run and raises on the box, after submit already said it worked.
    run = Run(None)

    with pytest.raises(KeyError, match="training_perplexity"):
        run.step(perplexity=1.0)

    with pytest.raises(KeyError, match="training_nonsense"):
        run.log_group("training_nonsense", {"a": 1.0})


def test_the_runs_own_label_cannot_be_taken_from_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # It would relabel every sample away from the real run, and the supervisor
    # keeps writing the true id, so the dashboard shows a live run with no
    # training metrics and nothing says why.
    in_a_job(monkeypatch)

    with pytest.raises(ValueError, match="run_id"):
        track(run_id="somebody-elses-run")


def test_a_total_of_no_steps_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="total"):
        Run(None, total=0)


def test_tokens_that_count_backwards_are_refused() -> None:
    with pytest.raises(ValueError, match="tokens_per_step"):
        Run(None, tokens_per_step=-1000)


def test_a_refused_argument_leaves_no_pump_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPARKS_RUN_ID", "run-9")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")
    before = threading.active_count()

    with pytest.raises(ValueError, match="total"):
        track(total=-1)

    assert threading.active_count() == before


def test_a_job_with_no_prometheus_url_says_so_out_loud(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A laptop run is quiet on purpose, but a run id with no url is a
    # misconfigured box, and nothing else would ever mention it.
    monkeypatch.setenv("SPARKS_RUN_ID", "run-9")
    monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)

    run = track()

    assert run._metrics is None
    assert "SPARKS_PROMETHEUS_URL" in caplog.text
