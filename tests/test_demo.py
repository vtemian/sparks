from itertools import pairwise

from sparks.demo import curve


def test_loss_decays_and_stays_positive() -> None:
    values = [curve(step, total=480, seed=0) for step in range(480)]
    assert all(v > 0 for v in values)
    assert sum(values[:20]) / 20 > sum(values[-20:]) / 20


def test_it_is_noisy_rather_than_monotonic() -> None:
    # A perfectly smooth curve reads as fake at a glance, and the demo has to be
    # indistinguishable from a real run for the dashboard to be worth trusting.
    values = [curve(step, total=480, seed=0) for step in range(480)]
    assert any(b > a for a, b in pairwise(values))


def test_the_same_seed_gives_the_same_curve() -> None:
    a = [curve(s, total=100, seed=7) for s in range(100)]
    b = [curve(s, total=100, seed=7) for s in range(100)]
    assert a == b


def test_different_seeds_give_different_curves() -> None:
    a = [curve(s, total=100, seed=1) for s in range(100)]
    b = [curve(s, total=100, seed=2) for s in range(100)]
    assert a != b
