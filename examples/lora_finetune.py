# A LoRA fine-tune with a hand-written loop and no Trainer, submitted with the
# image your own project builds.
#
#   sparks submit --context . --data ./corpus --name lora-r16 \
#     -- python /app/lora_finetune.py --epochs 3

import argparse
import os
import pathlib
import time

import torch  # type: ignore[import-not-found]
from torch.utils.data import DataLoader  # type: ignore[import-not-found]

from sparks.emit import RunMetrics, from_env

DATA = pathlib.Path(os.environ.get("SPARKS_DATA", "/data"))


def metrics_for(model_name: str, run_id: str | None) -> RunMetrics | None:
    # Inside a job the supervisor has already exported the run id and the URL.
    inside_a_job = from_env(arm="lora")
    if inside_a_job is not None:
        return inside_a_job

    # Outside one, only build an emitter if somebody said where to push. The
    # base URL, not the write endpoint: sparks appends /api/v1/write itself.
    url = os.environ.get("SPARKS_PROMETHEUS_URL")
    if not url or not run_id:
        return None

    return RunMetrics(run_id=run_id, url=url, info={"model": model_name})


def build_model_and_data() -> tuple[torch.nn.Module, DataLoader, torch.optim.Optimizer]:
    raise NotImplementedError("your project's model, corpus and optimiser")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--model", default="helium-2b")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    model, loader, optimiser = build_model_and_data()
    metrics = metrics_for(args.model, args.run_id)

    total = args.epochs * len(loader)
    started = time.monotonic()
    step = 0

    # `with` records `crashed` if the loop raises, so a run that dies still
    # ends on the dashboard instead of flat-lining. Harmless when metrics is
    # None, because then there is nothing to close.
    try:
        for epoch in range(args.epochs):
            for batch in loader:
                step += 1
                loss = model(**batch).loss
                loss.backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimiser.step()
                optimiser.zero_grad(set_to_none=True)

                if metrics is None:
                    continue

                elapsed = time.monotonic() - started
                metrics.log(
                    step=step,
                    epoch=epoch + 1,
                    loss=float(loss),
                    grad_norm=float(grad_norm),
                    progress=step / total,
                    eta_seconds=(elapsed / step) * (total - step),
                    tokens_per_sec=batch["input_ids"].numel() / elapsed,
                )
                # One series per parameter group. A single training_learning_rate
                # would be wrong by 10x for whichever group it did not describe.
                metrics.log_group(
                    "training_learning_rate",
                    {
                        group.get("name", str(index)): group["lr"]
                        for index, group in enumerate(optimiser.param_groups)
                    },
                )

            if metrics is not None:
                metrics.log(eval_loss=evaluate(model, loader))
    finally:
        if metrics is not None:
            metrics.end("finished")


def evaluate(model: torch.nn.Module, loader: DataLoader) -> float:
    raise NotImplementedError("your held-out pass")


if __name__ == "__main__":
    main()
