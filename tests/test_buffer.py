from sparks.buffer import Buffer
from sparks.series import Series

S = Series("training_loss", {"run_id": "r1"})
T = Series("training_step", {"run_id": "r1"})


def test_drain_returns_what_was_added_and_empties() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(T, 2.0, 1000)
    assert len(b.drain()) == 2
    assert b.drain() == []


def test_a_repeated_timestamp_on_one_series_is_dropped() -> None:
    # Two samples with the same ms timestamp and different values is a 400
    # that rolls back the entire batch, so it never reaches the wire.
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(S, 2.0, 1000)
    out = b.drain()
    assert len(out) == 1
    assert out[0]["values"] == [1.0]


def test_the_same_timestamp_on_different_series_is_kept() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(T, 5.0, 1000)
    assert len(b.drain()) == 2


def test_a_timestamp_older_than_one_already_sent_is_dropped() -> None:
    b = Buffer()
    b.add(S, 1.0, 2000)
    b.drain()
    b.add(S, 9.0, 1500)
    assert b.drain() == []


def test_samples_for_one_series_batch_into_a_single_entry() -> None:
    b = Buffer()
    b.add(S, 1.0, 1000)
    b.add(S, 2.0, 2000)
    out = b.drain()
    assert len(out) == 1
    assert out[0]["values"] == [1.0, 2.0]
    assert out[0]["timestamps"] == [1000, 2000]


def test_drain_sorts_out_of_order_input() -> None:
    # time.time() is not monotonic and log() interleaves from two threads, so a
    # later add can carry an earlier timestamp. Prometheus rejects out-of-order
    # samples, so the sort in drain() is load-bearing, not cosmetic.
    b = Buffer()
    b.add(S, 2.0, 2000)
    b.add(S, 1.0, 1000)
    out = b.drain()
    assert out[0]["timestamps"] == [1000, 2000]
    assert out[0]["values"] == [1.0, 2.0]
