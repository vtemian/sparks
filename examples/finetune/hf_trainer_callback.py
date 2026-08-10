# sparks from a HuggingFace Trainer. sparks is deliberately not a
# TrainerCallback -- it is a plain object a loop calls, which works in both
# worlds -- so bridging it is this file, and nothing in sparks changes.
#
#   sparks submit --context ./examples/finetune --data ./examples/data \
#     --name sft -- python /app/hf_trainer_callback.py
#
# Trainer hands on_log whatever it felt like logging, and the keys differ by
# version and by task, so map them explicitly. Passing them straight through
# raises on the first key sparks has never heard of.

import argparse
import os
import pathlib
import time
from typing import Any

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

from sparks.emit import from_env

MODEL = "HuggingFaceTB/SmolLM2-135M"
BLOCK = 256
HELD_OUT = 16

# Trainer's key -> the sparks metric, without its training_ prefix. Anything not
# named here is dropped rather than guessed at.
LOGGED = {
    "loss": "loss",
    "eval_loss": "eval_loss",
    "learning_rate": "learning_rate",
    "grad_norm": "grad_norm",
    "epoch": "epoch",
}


class SparksCallback(TrainerCallback):
    def __init__(self, tokens_per_step: int) -> None:
        self.metrics = from_env(arm="sft")
        self.tokens_per_step = tokens_per_step
        self.started = time.monotonic()

    def on_log(
        self,
        args: Any,
        state: Any,
        control: Any,
        logs: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> None:
        # **kwargs is load-bearing: Trainer passes the model, the tokenizer and
        # whatever else that version felt like, and the set changes between
        # releases. Without it the first log call raises TypeError.
        if self.metrics is None or not logs:
            return

        values = {
            LOGGED[key]: float(value)
            for key, value in logs.items()
            if key in LOGGED and isinstance(value, int | float)
        }
        # Trainer reports neither pace nor throughput, so derive both here: the
        # panels for them are empty otherwise, and a callback is the only place
        # that knows how many tokens a step consumed.
        if state.max_steps and state.global_step:
            per_step = (time.monotonic() - self.started) / state.global_step
            values["step"] = state.global_step
            values["progress"] = state.global_step / state.max_steps
            values["eta_seconds"] = per_step * (state.max_steps - state.global_step)
            values["tokens_per_sec"] = self.tokens_per_step / per_step
        if values:
            self.metrics.log(**values)

    def on_train_end(self, args: Any, state: Any, control: Any, **kwargs: Any) -> None:
        if self.metrics is not None:
            self.metrics.end("finished")


class Blocks(torch.utils.data.Dataset):
    def __init__(self, ids: torch.Tensor) -> None:
        usable = (ids.numel() // BLOCK) * BLOCK
        self.blocks = ids[:usable].view(-1, BLOCK)

    def __len__(self) -> int:
        return self.blocks.shape[0]

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        return {"input_ids": self.blocks[index]}

    def split(self, held_out: int) -> tuple["Blocks", "Blocks"]:
        return Blocks(self.blocks[held_out:]), Blocks(self.blocks[:held_out])


def corpus(data: pathlib.Path, tokenizer: AutoTokenizer) -> Blocks:
    files = sorted(data.glob("*.txt"))
    if not files:
        raise SystemExit(f"no .txt under {data}; pass the corpus with --data")

    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    # The corpus is one long stream we chunk ourselves; without this the
    # tokenizer warns that it exceeds the model's context, which it never sees.
    tokenizer.model_max_length = int(1e9)
    return Blocks(tokenizer(text, return_tensors="pt").input_ids[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="Trainer fine-tune reporting to sparks")
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    tokenizer.pad_token = tokenizer.eos_token
    data = pathlib.Path(os.environ.get("SPARKS_DATA", "/data"))
    blocks = corpus(data, tokenizer)
    train, held_out = blocks.split(HELD_OUT)

    trainer = Trainer(
        model=AutoModelForCausalLM.from_pretrained(MODEL),
        args=TrainingArguments(
            output_dir="/tmp/sft",
            num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch_size,
            learning_rate=5e-5,
            # Every logging_steps is one push, so this sets how dense the curve
            # in Grafana is, not just how chatty the console is.
            logging_steps=1,
            # Trainer logs eval_loss only when it evaluates, and that is the
            # only source for the held-out panel.
            eval_strategy="epoch",
            report_to=[],
            save_strategy="no",
        ),
        train_dataset=train,
        eval_dataset=held_out,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
        callbacks=[SparksCallback(args.batch_size * BLOCK)],
    )
    trainer.train()


if __name__ == "__main__":
    main()
