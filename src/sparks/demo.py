"""A synthetic training run.

The acceptance test for the whole pipeline: it must be indistinguishable from a
real run in Grafana. The shape is taken from a real fine-tuning loop on a small
corpus - 8 optimizer steps per epoch, 30 to 60 epochs, 6 to 12 s per epoch - so
the dashboard is tuned against a realistic cadence rather than a tight loop.
`--epochs` shortens it without changing that cadence.
"""

import math
import random
import time

from sparks.emit import RunMetrics
from sparks.run import current_user, git_sha, new_run_id

STEPS_PER_EPOCH = 8
EPOCHS = 40
STEP_SECONDS = 1.0


def curve(step: int, total: int, seed: int) -> float:
    """Exponential decay to a floor, plus reproducible noise."""
    # Seeded per step, not per run, so any step is reproducible on its own.
    # A tuple is not a supported seed type, hence the mix into one int.
    rng = random.Random(seed * 1_000_003 + step)
    decayed = 0.35 + 3.2 * math.exp(-4.0 * step / max(1, total))
    return max(0.01, decayed * (1.0 + rng.gauss(0.0, 0.06)))


def run(
    url: str,
    name: str = "demo",
    seed: int = 0,
    epochs: int = EPOCHS,
    step_seconds: float = STEP_SECONDS,
) -> str:
    """Play a full synthetic run and return its run_id.

    `epochs` and `step_seconds` are parameters rather than constants so an
    acceptance run can be shortened without editing the module the dashboard
    was tuned against.
    """
    total = epochs * STEPS_PER_EPOCH
    run_id = new_run_id(name, current_user())
    metrics = RunMetrics(
        run_id=run_id,
        url=url,
        info={
            "run_name": name,
            "git_sha": git_sha(),
            "model": "synthetic-2b",
            "dataset": "synthetic",
        },
        labels={"arm": "demo", "seed": str(seed)},
    )
    with metrics as m:
        started = time.monotonic()
        for step in range(total):
            time.sleep(step_seconds)
            loss = curve(step, total, seed)
            elapsed = time.monotonic() - started
            progress = (step + 1) / total
            eta = (total - step - 1) * (elapsed / max(step + 1, 1))
            m.log(
                step=step,
                epoch=step / STEPS_PER_EPOCH,
                loss=loss,
                steps_per_sec=(step + 1) / max(elapsed, 1e-6),
                tokens_per_sec=1180.0 * (step + 1) / max(elapsed, 1e-6),
                progress=progress,
                eta_seconds=eta,
            )
            m.log_group(
                "training_grad_norm", {"lora": loss * 1.7, "tables": loss * 0.2}
            )
            m.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})
            if (step + 1) % STEPS_PER_EPOCH == 0:
                m.log_group(
                    "training_eval_loss",
                    {"draw": loss * 1.1, "say": loss * 0.85},
                )
    return run_id
