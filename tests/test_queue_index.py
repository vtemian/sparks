from pathlib import Path

from sparks import index, spool
from tests import promtool

IMAGE = "spark.local:5000/demo:1"


def an_entry(
    tmp_path: Path,
    name: str,
    state: str = spool.QUEUED,
    user: str = "rex",
    **fields: object,
) -> spool.Entry:
    entry = spool.submit(
        tmp_path,
        name=name,
        user=user,
        command=["true"],
        image=IMAGE,
        when=1785849000.0,
    )
    if state != spool.QUEUED or fields:
        spool.set_state(entry.path, spool.State(state=state, **fields))  # type: ignore[arg-type]
    return spool.load(entry.path)


def test_a_queued_job_shows_up_with_its_identity(tmp_path: Path) -> None:
    rendered = index.render_queue([an_entry(tmp_path, "e0")], heartbeat=1785849100.0)
    assert 'state="queued"' in rendered
    assert 'name="e0"' in rendered
    assert 'user="rex"' in rendered


def test_depth_is_published_at_zero_so_the_series_exists(tmp_path: Path) -> None:
    rendered = index.render_queue([], heartbeat=1785849100.0)
    for state in (spool.QUEUED, spool.BUILDING, spool.RUNNING):
        assert f'sparks_queue_depth{{state="{state}"}} 0' in rendered


def test_depth_counts_what_is_there(tmp_path: Path) -> None:
    entries = [
        an_entry(tmp_path, "a"),
        an_entry(tmp_path, "b"),
        an_entry(tmp_path, "c", state=spool.RUNNING),
    ]
    rendered = index.render_queue(entries, heartbeat=1785849100.0)
    assert 'sparks_queue_depth{state="queued"} 2' in rendered
    assert 'sparks_queue_depth{state="running"} 1' in rendered


def test_the_heartbeat_is_always_written(tmp_path: Path) -> None:
    rendered = index.render_queue([], heartbeat=1785849100.0)
    assert "sparks_queue_runner_heartbeat_timestamp_seconds 1785849100" in rendered


def test_timestamps_are_values_not_prometheus_timestamps(tmp_path: Path) -> None:
    entry = an_entry(tmp_path, "e0", state=spool.RUNNING, started_unix=1785849050.0)
    rendered = index.render_queue([entry], heartbeat=1785849100.0)
    assert "sparks_queue_job_submitted_timestamp_seconds{" in rendered
    assert "} 1785849000" in rendered
    assert "sparks_queue_job_started_timestamp_seconds{" in rendered
    assert "} 1785849050" in rendered


def test_a_job_that_never_started_has_no_start_timestamp(tmp_path: Path) -> None:
    rendered = index.render_queue([an_entry(tmp_path, "e0")], heartbeat=1.0)
    assert "sparks_queue_job_started_timestamp_seconds" not in rendered


def test_the_image_label_is_present_but_empty_before_a_build(
    tmp_path: Path,
) -> None:
    queued = index.render_queue([an_entry(tmp_path, "e0")], heartbeat=1.0)
    built = index.render_queue(
        [an_entry(tmp_path, "e1", state=spool.RUNNING, image="sha256:abc")],
        heartbeat=1.0,
    )
    assert 'image=""' in queued
    assert 'image="sha256:abc"' in built


def test_the_run_id_joins_the_queue_to_the_run_index(tmp_path: Path) -> None:
    entry = an_entry(
        tmp_path, "e0", state=spool.RUNNING, run_id="run-20260806-1200-rex-e0"
    )
    rendered = index.render_queue([entry], heartbeat=1.0)
    assert 'run_id="run-20260806-1200-rex-e0"' in rendered


def test_an_empty_queue_still_produces_a_valid_file(tmp_path: Path) -> None:
    rendered = index.render_queue([], heartbeat=1785849100.0)
    assert rendered.endswith("\n")
    promtool.check_metrics(rendered)


def test_promtool_accepts_a_full_queue(tmp_path: Path) -> None:
    entries = [
        an_entry(tmp_path, "waiting"),
        an_entry(tmp_path, "compiling", state=spool.BUILDING),
        an_entry(
            tmp_path,
            "going",
            state=spool.RUNNING,
            image="sha256:abc",
            run_id="run-1",
            started_unix=1785849050.0,
        ),
        an_entry(
            tmp_path,
            "done",
            state=spool.FINISHED,
            exit_code=0,
            finished_unix=1785849090.0,
        ),
    ]
    promtool.check_metrics(index.render_queue(entries, heartbeat=1785849100.0))


def test_a_hostile_job_name_cannot_poison_the_file(tmp_path: Path) -> None:
    entry = an_entry(tmp_path, 'evil"} 1\nsparks_queue_depth{state="queued')
    rendered = index.render_queue([entry], heartbeat=1.0)
    promtool.check_metrics(rendered)


def test_publishing_writes_the_file_readably(tmp_path: Path) -> None:
    target = tmp_path / index.QUEUE_FILENAME
    index.publish_queue([], target, heartbeat=1785849100.0)
    assert target.exists()
    # 0600 is the mode mkstemp creates and the one node_exporter silently skips.
    assert target.stat().st_mode & 0o044
    assert "heartbeat" in target.read_text()


def test_publishing_replaces_rather_than_appends(tmp_path: Path) -> None:
    target = tmp_path / index.QUEUE_FILENAME
    index.publish_queue([an_entry(tmp_path, "gone")], target, heartbeat=1.0)
    index.publish_queue([], target, heartbeat=2.0)
    assert "gone" not in target.read_text()
