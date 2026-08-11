import sys
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from sparks.emit import FLUSH_SECONDS, Pump, Run, RunRecord
from sparks.fire import launch as launcher
from sparks.run import new_run_id

URL = "http://127.0.0.1:19091"

pytestmark = pytest.mark.live


def query(expr: str) -> list[dict[str, Any]]:
    r = requests.get(f"{URL}/api/v1/query", params={"query": expr}, timeout=5)
    r.raise_for_status()
    result: list[dict[str, Any]] = r.json()["data"]["result"]
    return result


def wait_for(expr: str, seconds: float = 20.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = query(expr)
        if got:
            return got
        time.sleep(0.5)
    raise AssertionError(f"nothing matched {expr!r} within {seconds}s")


def test_a_run_is_visible_end_to_end() -> None:
    run_id = new_run_id("live", "test")
    # A record and a run, the way the box has them: the supervisor owns the
    # lifecycle series and the training child owns everything else.
    m = RunRecord(
        run_id=run_id,
        url=URL,
        info={"run_name": "live", "git_sha": "abc1234", "model": "test"},
    )
    m.begin()
    child = Run(Pump(URL), labels={"run_id": run_id, "arm": "real", "seed": "0"})
    for step in range(5):
        child.log(step=step, loss=1.0 / (step + 1))
        time.sleep(0.2)

    info = wait_for(f'training_run_info{{run_id="{run_id}"}}')
    assert info[0]["metric"]["git_sha"] == "abc1234"
    assert info[0]["value"][1] == "1"

    loss = wait_for(f'training_loss{{run_id="{run_id}"}}')
    assert loss[0]["metric"]["arm"] == "real"

    # The join every panel uses must resolve, and must not corrupt the value.
    joined = wait_for(
        f'training_loss{{run_id="{run_id}"}} * on(run_id) '
        f"group_left(run_name, git_sha) "
        f"max by (run_id, run_name, git_sha) (training_run_info)"
    )
    assert float(joined[0]["value"][1]) == float(loss[0]["value"][1])

    m.end("finished")
    status = wait_for(f'training_run_status{{run_id="{run_id}"}}')
    assert status[0]["metric"]["status"] == "finished"

    # The info metric must not have acquired a status label: a second label set
    # is a second series and turns every joined panel red.
    infos = query(f'training_run_info{{run_id="{run_id}"}}')
    assert len(infos) == 1, f"info metric split into {len(infos)} series"
    assert "status" not in infos[0]["metric"]


def test_a_finished_run_stops_dead_rather_than_flatlining() -> None:
    # The stale markers. Without them a finished run holds its last value for
    # the full 5 minute lookback window, so the graph lies about what is
    # currently happening.
    run_id = new_run_id("live-stale", "test")
    m = RunRecord(run_id=run_id, url=URL, info={"run_name": "live-stale"})
    m.begin()
    child = Run(Pump(URL), labels={"run_id": run_id})
    child.log(loss=0.5)
    wait_for(f'training_loss{{run_id="{run_id}"}}')
    child.end()
    m.end("finished")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not query(f'training_loss{{run_id="{run_id}"}}'):
            break
        time.sleep(0.5)
    else:
        raise AssertionError("training_loss still resolves after end(); stale markers")

    # The run is still in the historical record, it just is not current.
    assert query(f'last_over_time(training_loss{{run_id="{run_id}"}}[1h])')


def test_the_dashboard_variable_query_returns_the_run() -> None:
    run_id = new_run_id("live-var", "test")
    m = RunRecord(run_id=run_id, url=URL, info={"run_name": "live-var"})
    m.begin()
    wait_for(f'training_run_info{{run_id="{run_id}"}}')
    r = requests.get(
        f"{URL}/api/v1/label/run_id/values",
        params={"match[]": "training_run_info"},
        timeout=5,
    )
    r.raise_for_status()
    assert run_id in r.json()["data"]
    m.end("finished")


def test_the_receiver_dropped_nothing_silently() -> None:
    # Prometheus drops a series with an invalid label name and still answers
    # 200, counting it only here. A green push proves nothing on its own.
    dropped = query("prometheus_api_remote_write_invalid_labels_samples_total")
    assert not dropped or float(dropped[0]["value"][1]) == 0.0, dropped

    # sum(): on 3.13.2 this counter has two series, type="float" and
    # type="histogram", so reading [0] is a coin flip over which one is checked.
    ooo = query("sum(prometheus_tsdb_out_of_order_samples_total)")
    assert not ooo or float(ooo[0]["value"][1]) == 0.0, ooo


def test_a_crashed_run_still_lands_its_whole_record(tmp_path: Path) -> None:
    result = launcher.launch(
        ["sh", "-c", "exit 3"],
        name="live-crash",
        shared_dir=tmp_path,
        url=URL,
        baseline_seconds=0.0,
    )
    assert result.status == "crashed"

    for metric in (
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_end_timestamp_seconds",
        "training_run_status",
    ):
        got = wait_for(f'{metric}{{run_id="{result.run_id}"}}')
        assert got, f"{metric} was rolled back and never landed"

    status = query(f'training_run_status{{run_id="{result.run_id}"}}')
    assert status[0]["metric"]["status"] == "crashed"

    # And nothing was silently dropped on the way in.
    dropped = query("prometheus_api_remote_write_invalid_labels_samples_total")
    assert not dropped or float(dropped[0]["value"][1]) == 0.0
    ooo = query("sum(prometheus_tsdb_out_of_order_samples_total)")
    assert not ooo or float(ooo[0]["value"][1]) == 0.0, (
        "an out-of-order sample was accepted-and-counted; a second writer is racing"
    )


def test_the_heartbeat_advances_while_the_run_lives() -> None:
    # Every unit test uses autostart=False and every other live test finishes
    # inside the 5m lookback, so a frozen heartbeat is indistinguishable from a
    # live one and mutation E7 (never calling _beat) survives them all. Waiting
    # past two flush cycles and asserting the timestamp moved is what notices.
    run_id = new_run_id("live-beat", "test")
    m = RunRecord(run_id=run_id, url=URL, info={"run_name": "live-beat"})
    m.begin()
    metric = f'training_run_heartbeat_timestamp_seconds{{run_id="{run_id}"}}'
    first = float(wait_for(metric)[0]["value"][1])
    time.sleep(FLUSH_SECONDS * 2 + 1.0)
    try:
        later = float(query(metric)[0]["value"][1])
    finally:
        m.end("finished")
    assert later > first, "the heartbeat froze; _beat is not running each cycle"


def test_a_child_that_emits_lands_alongside_the_supervisor_lifecycle(
    tmp_path: Path,
) -> None:
    # The whole disjoint-series apparatus exists for two real writers on one
    # run_id: the supervisor owns the lifecycle, the child owns everything else.
    # No live test drove a real child against a real Prometheus until this one.
    child = [
        sys.executable,
        "-c",
        "import time\n"
        "from sparks.emit import track\n"
        "with track(arm='real') as run:\n"
        "    run.log(loss=0.25)\n"
        "    time.sleep(0.5)\n",
    ]
    result = launcher.launch(
        child, name="live-child", shared_dir=tmp_path, url=URL, baseline_seconds=0.0
    )
    assert result.status == "finished"

    # The child's own series landed, carrying the label it set. last_over_time,
    # because the child marked its series stale on the way out: by the time
    # launch() returns, a bare training_loss correctly resolves to nothing, and
    # that is the behaviour the stale-marker test above exists to guarantee.
    loss = wait_for(f'last_over_time(training_loss{{run_id="{result.run_id}"}}[1h])')
    assert loss[0]["metric"]["arm"] == "real"
    assert float(loss[0]["value"][1]) == pytest.approx(0.25)

    # And all four of the supervisor's lifecycle series landed for the same run.
    for metric in (
        "training_run_info",
        "training_run_start_timestamp_seconds",
        "training_run_end_timestamp_seconds",
        "training_run_status",
    ):
        assert wait_for(f'{metric}{{run_id="{result.run_id}"}}')


def test_a_tracked_child_lands_the_values_it_derived(tmp_path: Path) -> None:
    # track() derives progress and the rates rather than taking them from the
    # caller, so the only proof they are right is reading them back off a real
    # receiver. The sleep is load-bearing twice: it puts the two steps in
    # different milliseconds, which the buffer needs to keep both, and it gives
    # the rate a span to divide by.
    child = [
        sys.executable,
        "-c",
        "import time\n"
        "from sparks.emit import track\n"
        "with track(total=2, tokens_per_step=100, arm='tracked') as run:\n"
        "    run.step(loss=0.25, learning_rate={'adapter': 2e-4})\n"
        "    time.sleep(0.5)\n"
        "    run.step(loss=0.20, learning_rate={'adapter': 1e-4})\n",
    ]
    result = launcher.launch(
        child, name="live-tracked", shared_dir=tmp_path, url=URL, baseline_seconds=0.0
    )
    assert result.status == "finished"

    run_id = result.run_id
    progress = wait_for(f'last_over_time(training_progress{{run_id="{run_id}"}}[1h])')
    assert float(progress[0]["value"][1]) == pytest.approx(1.0)
    assert progress[0]["metric"]["arm"] == "tracked"

    step = wait_for(f'last_over_time(training_step{{run_id="{run_id}"}}[1h])')
    assert float(step[0]["value"][1]) == pytest.approx(2.0)

    # Two steps half a second apart is about two per second, and a hundred
    # tokens each makes the throughput follow it.
    rate = wait_for(f'last_over_time(training_steps_per_sec{{run_id="{run_id}"}}[1h])')
    assert float(rate[0]["value"][1]) == pytest.approx(2.0, rel=0.5)
    grouped = wait_for(
        f'last_over_time(training_learning_rate{{run_id="{run_id}"}}[1h])'
    )
    assert grouped[0]["metric"]["group"] == "adapter"

    tokens = wait_for(
        f'last_over_time(training_tokens_per_sec{{run_id="{run_id}"}}[1h])'
    )
    # Against the expected figure, not against the rate we just read: deriving
    # the expectation from the reading passes for any rate, right or wrong.
    assert float(tokens[0]["value"][1]) == pytest.approx(200.0, rel=0.5)
