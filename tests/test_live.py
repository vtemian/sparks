"""Against a real Prometheus from tests/harness-up.sh. No mocks: the thing
under test is the wire format and the receiver's opinion of it, and a fake
would only ever confirm our own assumptions.

    make live
"""

import sys
import time
from pathlib import Path
from typing import Any

import pytest
import requests

from sparks.emit import FLUSH_SECONDS, RunMetrics
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
    m = RunMetrics(
        run_id=run_id,
        url=URL,
        info={"run_name": "live", "git_sha": "abc1234", "model": "test"},
        labels={"arm": "real", "seed": "0"},
    )
    m.begin()
    for step in range(5):
        m.log(step=step, loss=1.0 / (step + 1))
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
    m = RunMetrics(run_id=run_id, url=URL, info={"run_name": "live-stale"})
    m.begin()
    m.log(loss=0.5)
    wait_for(f'training_loss{{run_id="{run_id}"}}')
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
    m = RunMetrics(run_id=run_id, url=URL, info={"run_name": "live-var"})
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
    """The regression test for the bug a unit test could not catch.

    An out-of-order sample makes Prometheus reject the WHOLE request, so a
    second writer racing the pump silently destroyed the batch carrying
    training_run_info, the start and end timestamps and the status. Every
    non-finished run lost its entire record: exactly the runs the wrapper
    exists to report on. summary.json said `crashed` and Prometheus held
    nothing at all.
    """
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
    m = RunMetrics(run_id=run_id, url=URL, info={"run_name": "live-beat"})
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
        "from sparks.emit import from_env\n"
        "m = from_env(arm='real')\n"
        "assert m is not None\n"
        "with m:\n"
        "    m.log(loss=0.25)\n"
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
