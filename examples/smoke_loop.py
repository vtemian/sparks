# A fake fine-tune: no GPU, no ML libraries. Submit this first to prove the
# path works -- image pushed, data mounted, run recorded, curves in Grafana.
#
#   sparks submit --context ./examples --data ./examples/data \
#     --name smoke -- python /app/smoke_loop.py
#
# Absolute path because the container's working directory is the box's shared
# directory, not the image's. Every metric below is declared in
# sparks.metrics.METRICS; an undeclared name raises instead of vanishing.
#
# The default finishes in seconds, which is what a smoke test should do. Grafana
# floors its query step at the scrape interval, so a run that short arrives as a
# single averaged point: pass --steps and --step-seconds when you want a curve
# to look at rather than a pass/fail.

import argparse
import math
import os
import pathlib
import random
import time

from sparks.emit import from_env

BATCH_TOKENS = 4096


def main() -> None:
    parser = argparse.ArgumentParser(description="a fake fine-tune reporting to sparks")
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--step-seconds", type=float, default=0.05)
    args = parser.parse_args()

    # The client uploads --data into the job and mounts it read-only here.
    # Read this path, never a laptop path: the box has no idea where your
    # corpus lives.
    data = pathlib.Path(os.environ.get("SPARKS_DATA", "/data"))
    corpus = sorted(data.glob("*"))
    print(f"{len(corpus)} files under {data}")

    # None when you run this outside a job container, so the same script works
    # on your laptop with no Prometheus and no run id.
    metrics = from_env()
    if metrics is None:
        print("no SPARKS_RUN_ID: running unmetered")

    started = time.monotonic()
    loss = 2.4
    for step in range(1, args.steps + 1):
        time.sleep(args.step_seconds)
        loss = max(0.15, loss * 0.995 + random.uniform(-0.01, 0.01))

        if metrics is not None:
            elapsed = time.monotonic() - started
            metrics.log(
                step=step,
                loss=loss,
                epoch=step / args.steps,
                progress=step / args.steps,
                learning_rate=2e-4 * (1 - step / args.steps),
                grad_norm=abs(math.sin(step / 10)) * 2,
                tokens_per_sec=BATCH_TOKENS / args.step_seconds,
                eta_seconds=(elapsed / step) * (args.steps - step),
            )

        if step % 50 == 0:
            print(f"step {step:4}  loss {loss:.4f}")
            if metrics is not None:
                metrics.log(eval_loss=loss + random.uniform(0.0, 0.08))


if __name__ == "__main__":
    main()
