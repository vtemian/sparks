# sparks from a HuggingFace Trainer. sparks is not a TrainerCallback -- it is a
# plain object a loop calls -- so bridging it is this file, and nothing in
# sparks changes.
#
#   sparks submit --context . --data ./corpus --name sft \
#     -- python /app/hf_trainer_callback.py

from typing import Any

from transformers import (  # type: ignore[import-not-found]
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

from sparks.emit import from_env

# Trainer's key -> the sparks metric, without its `training_` prefix. Anything
# not named here is dropped rather than guessed at.
LOGGED = {
    "loss": "loss",
    "eval_loss": "eval_loss",
    "learning_rate": "learning_rate",
    "grad_norm": "grad_norm",
    "epoch": "epoch",
}


class SparksCallback(TrainerCallback):
    def __init__(self) -> None:
        self.metrics = from_env()

    def on_log(
        self, args: Any, state: Any, control: Any, logs: dict[str, float] | None = None
    ) -> None:
        if self.metrics is None or not logs:
            return

        values = {
            LOGGED[key]: float(value)
            for key, value in logs.items()
            if key in LOGGED and isinstance(value, int | float)
        }
        if state.max_steps:
            values["step"] = state.global_step
            values["progress"] = state.global_step / state.max_steps
        if values:
            self.metrics.log(**values)

    def on_train_end(self, args: Any, state: Any, control: Any, **kwargs: Any) -> None:
        if self.metrics is not None:
            self.metrics.end("finished")


def main() -> None:
    trainer = Trainer(
        model=...,
        args=TrainingArguments(output_dir="/tmp/out", logging_steps=10),
        train_dataset=...,
        callbacks=[SparksCallback()],
    )
    trainer.train()


if __name__ == "__main__":
    main()
