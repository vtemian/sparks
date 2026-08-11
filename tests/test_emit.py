import os
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from sparks.emit import RECORD_ONLY, SCRAPE_SECONDS, Pump, Run, RunRecord, track
from sparks.metrics import LIFECYCLE
from sparks.series import InvalidLabelError


def names(drained: list[dict[str, Any]]) -> set[str]:
    return {d["metric"]["__name__"] for d in drained}


def sent(run: Run) -> list[dict[str, Any]]:
    assert run._pump is not None
    return run._pump._buffer.drain()


def pump_of(run: Run) -> Pump:
    assert run._pump is not None
    return run._pump


def pump() -> Pump:
    # autostart=False keeps the pump thread out of the unit tests; the live
    # test in tests/test_live.py is what exercises the thread and the network.
    return Pump("http://unused", autostart=False)


def child(**kw: Any) -> Run:
    # What track builds inside a job, minus the thread.
    kw.setdefault("labels", {"run_id": "run-9"})
    return Run(pump(), **kw)


def make(**kw: Any) -> RunRecord:
    return RunRecord(run_id="run-1", url="http://unused", autostart=False, **kw)


def test_begin_emits_identity_and_start_time() -> None:
    m = make(info={"model": "helium"})
    m.begin()
    out = m._pump._buffer.drain()
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
        d
        for d in m._pump._buffer.drain()
        if d["metric"]["__name__"] == "training_run_info"
    )
    assert info["metric"]["run_id"] == "run-1"
    assert info["metric"]["model"] == "helium"
    assert info["values"] == [1.0]


def test_log_emits_one_series_per_named_value() -> None:
    m = child()
    m.log(step=3, loss=0.5)

    drained = pump_of(m)._buffer.drain()
    assert names(drained) == {"training_step", "training_loss"}
    assert len(drained) == 2


def test_log_refuses_an_undeclared_metric() -> None:
    run = child()
    with pytest.raises(KeyError):
        run.log(not_a_real_metric=1.0)


def test_series_labels_land_on_every_sample() -> None:
    run = child(labels={"run_id": "run-1", "arm": "real", "seed": "0"})
    run.log(loss=0.5, step=3)

    carried = [
        (d["metric"]["arm"], d["metric"]["seed"], d["metric"]["run_id"])
        for d in sent(run)
    ]
    assert carried == [("real", "0", "run-1")] * 2


def test_end_emits_terminal_state_and_never_mutates_info() -> None:
    m = make()
    m.begin()
    m._pump._buffer.drain()
    m.end("finished")
    out = m._pump._buffer.drain()
    assert "training_run_end_timestamp_seconds" in names(out)
    status = next(d for d in out if d["metric"]["__name__"] == "training_run_status")
    assert status["metric"]["status"] == "finished"
    # The info metric must never be re-emitted with different labels: a second
    # label set means a second series and a red panel.
    infos = [d for d in out if d["metric"]["__name__"] == "training_run_info"]
    assert all("status" not in d["metric"] for d in infos)


def test_shutdown_is_idempotent() -> None:
    # Must run with the pump started. With autostart=False `_stop` is None and
    # close() returns on its first line, so the guard this test exists for is
    # never reached. The second call is not hypothetical: _start registers
    # close with atexit, so every real run calls it twice.
    record = RunRecord(run_id="run-1", url="http://127.0.0.1:1", autostart=True)
    record.begin()
    assert record._pump._thread.is_alive()
    record.end("finished")
    assert not record._pump._thread.is_alive(), "the pump thread should have stopped"
    record.end("finished")  # must not raise, and must not restart anything
    assert not record._pump._thread.is_alive()


def test_a_push_failure_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A metrics outage must never kill a training run. The URL is unroutable.
    run = Run(Pump("http://127.0.0.1:1"), labels={"run_id": "run-1"})
    run.log(loss=0.5)

    run.end()  # must not raise

    assert "dropped 1 series" in caplog.text


def test_the_run_record_is_never_marked_stale() -> None:
    # end() writes the status, and _mark_stale runs a millisecond later over
    # everything the buffer has seen. Without the exemption it erases the
    # status immediately and the run never resolves. The live test caught this.
    record = make()
    record.begin()
    record._pump._buffer.drain()  # what the pump would have sent, so seen() fills
    record.end("finished")
    record._pump._buffer.drain()

    # Assert on the batch _mark_stale actually builds. Re-deriving the filter
    # here would only check the test's own arithmetic.
    staled = {d["metric"]["__name__"] for d in record._pump._stale_batch()}
    assert "training_run_active" in staled, "the run's span never ends"
    assert not staled & LIFECYCLE, f"the run's own record was staled: {staled}"


def test_a_stale_marker_leaves_the_sample_it_ends_long_enough_to_be_queried() -> None:
    # A marker a millisecond later is ordered correctly and still unreachable:
    # Prometheus evaluates at discrete steps and Grafana floors the step at the
    # scrape interval, so every step falls outside a window that narrow. The
    # sample is accepted, /api/v1/series lists it, and no query returns it --
    # every run loses its final epoch and a run that logs once loses all of it.
    run = child()
    run.log(loss=0.5)
    pump_of(run)._buffer.drain()
    sent_at = next(
        ts
        for series, ts in pump_of(run)._buffer.seen().items()
        if series.name == "training_loss"
    )

    marker = next(
        d
        for d in pump_of(run)._stale_batch()
        if d["metric"]["__name__"] == "training_loss"
    )

    # One step is the least that can land inside the window. Asserting the gap
    # rather than the constant: what matters is that a query can see it.
    assert marker["timestamps"][0] - sent_at >= SCRAPE_SECONDS * 1000


def test_an_invalid_label_is_refused_on_the_caller_thread() -> None:
    # Not five seconds later on the pump, where it would kill telemetry for the
    # rest of the run behind a bare threading traceback.
    with pytest.raises(InvalidLabelError):
        RunRecord(run_id="r", url="http://unused", autostart=False, info={"g-s": "a"})

    with pytest.raises(InvalidLabelError):
        Run(pump(), labels={"a b": "c"})


def test_run_active_is_emitted_and_gets_a_stale_marker() -> None:
    # It must be staled, unlike the info metric, or the annotation region
    # overshoots by the whole lookback window.
    m = make()
    m.begin()

    assert "training_run_active" in names(m._pump._buffer.drain())
    assert "training_run_active" in {
        d["metric"]["__name__"] for d in m._pump._stale_batch()
    }


def test_a_child_emitter_writes_no_lifecycle_series() -> None:
    # The record and the training child both report for one run_id. Two writers
    # on the SAME series interleave timestamps, which is an out-of-order 400,
    # and remote-write rolls back the whole request, destroying the batch that
    # carried training_loss. A Run has no method for those series at all, and
    # refuses them by name as well.
    run = child()
    run.log(loss=0.5)

    assert names(sent(run)) == {"training_loss"}
    assert not hasattr(run, "begin")
    # RECORD_ONLY, not LIFECYCLE: the guard also refuses training_run_active,
    # which LIFECYCLE does not contain, and nothing else covers it.
    for name in sorted(RECORD_ONLY):
        with pytest.raises(KeyError, match="belongs to the supervisor"):
            run.log(**{name.removeprefix("training_"): 1.0})


def test_track_inside_a_job_builds_a_child_carrying_its_labels(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("SPARKS_RUN_ID", "run-7")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")
    run = track(arm="real")

    run.step(loss=1.0)

    sample = sent(run)[0]
    assert sample["metric"]["run_id"] == "run-7"
    assert sample["metric"]["arm"] == "real"
    run.end()


def test_the_record_writes_its_own_series_and_has_no_way_to_write_yours() -> None:
    # The old asymmetric guard let one class do both jobs depending on a flag.
    # Now the record simply has no log(), so the split is structural.
    record = make()

    record.begin()

    assert names(record._pump._buffer.drain()) >= {
        "training_run_info",
        "training_run_start_timestamp_seconds",
    }
    assert not hasattr(record, "log")


def test_a_stale_marker_actually_carries_the_stale_nan() -> None:
    # Nothing else in the suite checks the payload. Change _stale_batch to emit
    # 1.0 and everything else still passes, while every killed run's loss curve
    # flat-lines for the whole lookback window instead of stopping dead.
    # It must be THE stale NaN, 0x7FF0000000000002, not any NaN: an ordinary
    # NaN is a value, and only this bit pattern ends the series.
    m = child()
    m.log(loss=0.5)
    pump_of(m)._buffer.drain()
    marker = next(
        d
        for d in pump_of(m)._stale_batch()
        if d["metric"]["__name__"] == "training_loss"
    )
    (value,) = marker["values"]
    assert struct.pack("<d", value) == struct.pack("<Q", 0x7FF0000000000002)


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
        run.log(learning_rate={"adapter": 2e-4})

    assert run._pump is None
    assert run.count == 1


def ticking(*times: float) -> Callable[[], float]:
    values = iter(times)
    return lambda: next(values)


def test_a_tracked_step_derives_progress_from_the_total() -> None:
    run = child(total=4)

    run.step(loss=1.0)

    drained = sent(run)
    progress = next(
        d for d in drained if d["metric"]["__name__"] == "training_progress"
    )
    assert progress["values"] == [0.25]


def test_without_a_total_progress_is_absent_rather_than_wrong() -> None:
    # A guessed denominator renders a progress bar that lies. Emitting nothing
    # is the honest failure.
    run = child()

    run.step(loss=1.0)

    drained = sent(run)
    assert "training_progress" not in names(drained)
    assert "training_eta_seconds" not in names(drained)


def test_a_value_you_pass_wins_over_the_one_track_derived() -> None:
    run = child(total=4)

    run.step(step=99.0)

    drained = sent(run)
    step = next(d for d in drained if d["metric"]["__name__"] == "training_step")
    assert step["values"] == [99.0]


def test_one_step_is_too_early_to_call_it_a_rate() -> None:
    # Seeding a mark at construction would give the first step a free
    # zero-length interval and report a rate several times the real one.
    run = Run(None, clock=ticking(0.0))

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

    assert run._pump is not None
    assert run._pump._thread is not None
    assert not run._pump._thread.is_alive()


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
    # remain, so four seconds. One more step at the same pace leaves seven.
    run = Run(None, total=10, clock=ticking(0.0, 0.5, 1.0))

    run.step()
    run.step()
    early = run.derived()["eta_seconds"]

    run.step()

    assert early == 4.0
    assert run.derived()["eta_seconds"] < early


def test_overrunning_the_total_does_not_report_more_than_all_of_it() -> None:
    # training_progress is declared 0-1 and the panel is a percentunit with a
    # max of 1, so an overrun renders as 167% complete and a negative eta.
    run = Run(None, total=3, clock=ticking(0.0, 1.0, 2.0, 3.0, 4.0))
    for _ in range(5):
        run.step()

    derived = run.derived()

    assert derived["progress"] == 1.0
    assert derived["eta_seconds"] == 0.0


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
        assert run._pump is not None
        run.step(loss=1.0)

        assert sent(run)[0]["metric"]["autostart"] == "no"


def test_a_record_carries_the_status_it_was_ended_with() -> None:
    record = make()
    record.begin()
    record._pump._buffer.drain()

    record.end("crashed")

    ended = {
        d["metric"]["__name__"]: d["metric"].get("status")
        for d in record._pump._buffer.drain()
    }
    assert ended["training_run_status"] == "crashed"


def test_every_way_of_reporting_stops_once_the_run_has_ended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # step returns before it reaches log, so a guard on log or log_group alone
    # would go unnoticed by a test that only calls step.
    run = child()
    run.end()
    sent(run)

    run.step(loss=1.0)
    run.log(eval_loss=1.0)
    run.log(learning_rate={"adapter": 1.0})

    assert sent(run) == []
    assert len([r for r in caplog.records if "already ended" in r.message]) == 1


def test_ending_a_record_twice_records_one_ending() -> None:
    # The sleep is the test: without it both endings share a millisecond and
    # the buffer drops the second, so a missing guard would look guarded.
    record = make()
    record.begin()
    record.end("finished")
    time.sleep(0.002)

    record.end("finished")

    # One dict per series carrying every sample, so count the values: counting
    # the dicts would read the same whether it ended once or twice.
    status = next(
        d
        for d in record._pump._buffer.drain()
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
        run.log(nonsense={"a": 1.0})


def test_the_runs_own_label_cannot_be_taken_from_under_it() -> None:
    # It would relabel every sample away from the real run, and the supervisor
    # keeps writing the true id, so the dashboard shows a live run with no
    # training metrics and nothing says why.
    with pytest.raises(ValueError, match="run_id"):
        track(run_id="somebody-elses-run")


def test_a_total_of_no_steps_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="total"):
        Run(None, total=0)


def test_a_refused_argument_leaves_no_pump_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not dead arrange, unlike its neighbours: without a reachable box track
    # would build no pump at all and the assertion below would hold trivially.
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

    assert run._pump is None
    assert "SPARKS_PROMETHEUS_URL" in caplog.text


def test_a_step_reports_a_mapping_as_one_series_per_group() -> None:
    run = child()

    run.step(
        loss=0.5,
        learning_rate={"adapter": 2e-4, "norms": 2e-5},
    )

    by_group = {
        d["metric"]["group"]: d["values"][0]
        for d in sent(run)
        if d["metric"]["__name__"] == "training_learning_rate"
    }
    assert by_group == {"adapter": 2e-4, "norms": 2e-5}


def test_a_grouped_value_takes_the_short_key_like_every_other() -> None:
    # log_group used to want "training_learning_rate" while step and log took
    # the key without its prefix. One convention, not two.
    run = child()

    run.log(learning_rate={"adapter": 1.0})

    assert names(sent(run)) == {"training_learning_rate"}


def test_a_step_still_reports_its_plain_values_alongside_a_mapping() -> None:
    run = child()

    run.step(loss=0.5, learning_rate={"adapter": 1.0})

    assert names(sent(run)) >= {
        "training_loss",
        "training_learning_rate",
    }


def test_a_value_is_refused_where_the_caller_can_see_it_fail() -> None:
    # Off the box the value used to be stored untouched, so a tensor or a None
    # passed every laptop run and raised inside the training loop on the box,
    # after submit had already reported success.
    run = Run(None)

    with pytest.raises((TypeError, ValueError)):
        run.step(loss=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="could not convert"):
        run.step(loss="not a number")  # type: ignore[arg-type]


def test_a_grouped_value_is_refused_off_the_box_too() -> None:
    # Worse than the plain path: off the box the mapping was never iterated at
    # all, so neither its keys nor its values were ever looked at.
    run = Run(None)

    with pytest.raises(ValueError, match="could not convert"):
        run.step(learning_rate={"adapter": "not a number"})  # type: ignore[dict-item]


def test_a_group_name_that_is_not_a_name_is_refused() -> None:
    # Two keys that str() alike become one series on the wire sharing a
    # timestamp, which is a duplicate-sample 400, and remote-write rolls back
    # the whole request rather than the one series.
    run = Run(None)

    with pytest.raises(TypeError, match="group"):
        run.step(learning_rate={0: 1e-4})  # type: ignore[dict-item]


def test_the_group_label_cannot_be_taken_from_under_the_emitter() -> None:
    # No environment needed: track refuses the reserved label before it looks.
    with pytest.raises(ValueError, match="group"):
        track(group="mine")


def test_a_token_count_of_zero_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="tokens_per_step"):
        Run(None, tokens_per_step=0)


def test_the_rate_averages_the_window_rather_than_the_last_interval() -> None:
    # Four marks where the two readings disagree: 0.3/s across the window,
    # 0.125/s across the last interval alone. Smoothing that jitter is the
    # entire reason the window exists, and two marks cannot tell them apart.
    run = Run(None, window=20, clock=ticking(0.0, 1.0, 2.0, 10.0))
    for _ in range(4):
        run.step()

    assert run.rate() == 0.3


def test_a_step_reports_the_pace_it_measured() -> None:
    # rate() being right is not the same as step() sending it: deleting the
    # line that puts it in derived() kills a dashboard panel in silence.
    run = Run(None, clock=ticking(0.0, 0.5))

    run.step()
    run.step()

    assert run.derived()["steps_per_sec"] == 2.0


def test_the_first_step_reports_itself_as_step_one() -> None:
    run = child()

    run.step(loss=1.0)

    step = next(d for d in sent(run) if d["metric"]["__name__"] == "training_step")
    assert step["values"] == [1.0]


def test_a_step_after_the_end_does_not_advance_the_counter() -> None:
    # log's guard masks step's, so the buffer stays empty either way while the
    # counter drifts and a clock mark is consumed.
    run = child()
    run.end()

    run.step(loss=1.0)

    assert run.count == 0


def test_track_hands_the_run_what_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Everything else builds a Run directly, so track's own forwarding was the
    # one path with no test: Run(metrics) alone kept the suite green.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.delenv("SPARKS_PROMETHEUS_URL", raising=False)

    run = track(total=8, tokens_per_step=64, window=3)

    assert run.total == 8
    assert run.tokens_per_step == 64
    assert run._marks.maxlen == 3


def test_a_mapping_cannot_reach_a_supervisor_series_either() -> None:
    # The plain path was pinned; the grouped one became reachable only when a
    # mapping value did.
    run = child()

    with pytest.raises(KeyError, match="belongs to the supervisor"):
        run.log(run_active={"a": 1.0})


def test_a_value_reaches_the_buffer_as_a_float() -> None:
    # Without the coercion an int or a tensor reaches Buffer and raises on the
    # pump thread, where _flush's broad except drops the whole batch behind a
    # warning nobody reads.
    run = child()

    run.log(step=3)
    run.log(learning_rate={"lora": 2})

    assert [type(v) for d in sent(run) for v in d["values"]] == [
        float,
        float,
    ]


def test_a_metric_cannot_change_shape_within_a_run() -> None:
    # Both land, as different series with the same name, so the receiver says
    # nothing and the panel draws two identically-labelled lines.
    run = Run(None)
    run.log(loss=0.5)

    with pytest.raises(ValueError, match="loss"):
        run.log(loss={"a": 0.75})


def test_a_metric_that_started_grouped_stays_grouped() -> None:
    run = Run(None)
    run.log(learning_rate={"adapter": 1.0})

    with pytest.raises(ValueError, match="learning_rate"):
        run.log(learning_rate=1.0)


def test_the_same_shape_twice_is_fine() -> None:
    run = Run(None)

    run.log(loss=0.5)
    run.log(loss=0.4)
    run.log(learning_rate={"adapter": 1.0})
    run.log(learning_rate={"adapter": 0.5})


def test_a_url_without_a_run_id_mints_one_and_owns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On the box, launched by hand. Nobody else writes this run's identity, so
    # the dashboard's run picker would never list it.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track(name="e0", info={"model": "helium-2b"}) as run:
        assert run.run_id.startswith("run-")
        assert "e0" in run.run_id
        assert run._record is not None
        run.step(loss=1.0)

    assert run._record._ended


def test_a_supervised_run_never_writes_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The supervisor already began one. A second writer on those series is a
    # 400 that rolls back the whole batch, losing the training metrics too.
    monkeypatch.setenv("SPARKS_RUN_ID", "run-from-supervisor")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track(name="ignored", info={"model": "x"}) as run:
        assert run._record is None
        assert run.run_id == "run-from-supervisor"


def test_no_url_mints_nothing_however_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nowhere_near_a_job(monkeypatch)

    with track(name="e0") as run:
        assert run._record is None
        assert run._pump is None
        assert run.run_id == ""


def test_the_minted_id_reaches_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # So a second track in the same process becomes a child rather than
    # minting a second run, and so a subprocess inherits the right id.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track(name="e0") as run:
        assert os.environ["SPARKS_RUN_ID"] == run.run_id


def test_an_exception_ends_the_record_as_crashed() -> None:
    # __exit__ derives the status rather than taking one, so read it off the
    # series the record actually wrote. make() builds a threadless record, so
    # nothing but this test drains the buffer and the terminal samples survive.
    record = make()
    record.begin()
    record._pump._buffer.drain()
    run = child(labels={"run_id": "run-1"}, record=record)

    with pytest.raises(RuntimeError), run:
        raise RuntimeError("boom")

    ended = {
        d["metric"]["__name__"]: d["metric"].get("status")
        for d in record._pump._buffer.drain()
    }
    assert ended["training_run_status"] == "crashed"


def test_a_refused_shape_sends_nothing_from_that_call() -> None:
    # grad_norm is new and fine; loss is the conflict, and it comes second. The
    # grouped grad_norm must not have gone out already, or a caller who catches
    # the error has half a step recorded.
    run = child()
    run.log(loss=0.5)
    sent(run)

    with pytest.raises(ValueError, match="loss"):
        run.log(grad_norm={"a": 1.0}, loss={"b": 0.5})

    assert sent(run) == []


def test_a_minted_id_leaves_the_environment_when_the_run_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing cleared it before, so a second phase attached itself to the first
    # phase's dead run: record already ended, run_active already staled, and
    # the dashboard showing it finished.
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track(name="phase1") as run:
        assert os.environ["SPARKS_RUN_ID"] == run.run_id

    assert "SPARKS_RUN_ID" not in os.environ


def test_a_second_run_after_the_first_ends_is_a_run_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPARKS_RUN_ID", raising=False)
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track(name="phase1") as first:
        pass
    with track(name="phase2") as second:
        pass

    assert second.run_id != first.run_id
    assert second._record is not None


def test_a_supervised_run_leaves_the_supervisors_id_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The supervisor owns that variable and the container outlives this run.
    monkeypatch.setenv("SPARKS_RUN_ID", "run-from-supervisor")
    monkeypatch.setenv("SPARKS_PROMETHEUS_URL", "http://127.0.0.1:1")

    with track() as run:
        assert run._record is None

    assert os.environ["SPARKS_RUN_ID"] == "run-from-supervisor"
