# A LoRA fine-tune with a hand-written loop. No Trainer, so nothing to hang a
# callback on: sparks is just an object the loop calls.
#
#   sparks submit --context ./examples/finetune --data ./examples/data \
#     --name lora-r16 -- python /app/lora_finetune.py --epochs 3
#
# Two parameter groups on purpose. The adapter wants a learning rate an order of
# magnitude above the layer norms underneath it, and one averaged series would
# describe neither, which is what log_group is for.

import argparse
import os
import pathlib
import time

import torch
from peft import LoraConfig, get_peft_model
from torch.utils.data import DataLoader, TensorDataset
from transformers import AutoModelForCausalLM, AutoTokenizer

from sparks.emit import RunMetrics, from_env

MODEL = "HuggingFaceTB/SmolLM2-135M"
BLOCK = 256
HELD_OUT = 8


def metrics_for(run_id: str | None) -> RunMetrics | None:
    inside_a_job = from_env(arm="lora")
    if inside_a_job is not None:
        return inside_a_job

    # Outside one, only build an emitter if somebody said where to push. The
    # base URL, not the write endpoint: sparks appends /api/v1/write itself.
    url = os.environ.get("SPARKS_PROMETHEUS_URL")
    if not url or not run_id:
        return None

    return RunMetrics(run_id=run_id, url=url, info={"model": MODEL})


def blocks_from(data: pathlib.Path, tokenizer: AutoTokenizer) -> torch.Tensor:
    files = sorted(data.glob("*.txt"))
    if not files:
        raise SystemExit(f"no .txt under {data}; pass the corpus with --data")

    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    # The corpus is one long stream we chunk ourselves; without this the
    # tokenizer warns that it exceeds the model's context, which it never sees.
    tokenizer.model_max_length = int(1e9)
    ids = tokenizer(text, return_tensors="pt").input_ids[0]
    usable = (ids.numel() // BLOCK) * BLOCK
    if usable == 0:
        raise SystemExit(f"corpus is shorter than one {BLOCK}-token block")

    return ids[:usable].view(-1, BLOCK)


def adapted(model: torch.nn.Module) -> torch.nn.Module:
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    )
    tuned = get_peft_model(model, lora)
    # Layer norms train alongside the adapter, slower. This is what makes two
    # parameter groups real rather than a demonstration of the API.
    for name, parameter in tuned.named_parameters():
        if "norm" in name:
            parameter.requires_grad = True

    return tuned


def parameter_groups(model: torch.nn.Module, lr: float) -> list[dict[str, object]]:
    adapter = [p for n, p in model.named_parameters() if p.requires_grad and "lora" in n]
    norms = [p for n, p in model.named_parameters() if p.requires_grad and "lora" not in n]
    return [
        {"name": "adapter", "params": adapter, "lr": lr},
        {"name": "norms", "params": norms, "lr": lr / 10},
    ]


def evaluate(model: torch.nn.Module, batches: list[torch.Tensor], device: str) -> float:
    model.eval()
    total = 0.0
    with torch.no_grad():
        for batch in batches:
            ids = batch.to(device)
            total += float(model(input_ids=ids, labels=ids).loss)

    model.train()
    return total / len(batches)


def main() -> None:
    parser = argparse.ArgumentParser(description="LoRA fine-tune reporting to sparks")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device {device}, model {MODEL}")

    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    # The client mounts --data read-only here, and the box has no idea where
    # your corpus lives, so read this path and never a laptop one.
    data = pathlib.Path(os.environ.get("SPARKS_DATA", "/data"))
    blocks = blocks_from(data, tokenizer)
    print(f"{blocks.shape[0]} blocks of {BLOCK} tokens")

    held_out = [blocks[i : i + 1] for i in range(min(HELD_OUT, blocks.shape[0]))]
    loader = DataLoader(
        TensorDataset(blocks[HELD_OUT:]), batch_size=args.batch_size, shuffle=True
    )

    model = adapted(AutoModelForCausalLM.from_pretrained(MODEL)).to(device)
    model.print_trainable_parameters()
    optimiser = torch.optim.AdamW(parameter_groups(model, args.lr))

    total = args.epochs * len(loader)
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=total)
    metrics = metrics_for(args.run_id)

    started = time.monotonic()
    step = 0
    try:
        for epoch in range(args.epochs):
            for (batch,) in loader:
                step += 1
                ids = batch.to(device)
                loss = model(input_ids=ids, labels=ids).loss
                loss.backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimiser.step()
                schedule.step()
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
                    tokens_per_sec=step * ids.numel() / elapsed,
                )
                metrics.log_group(
                    "training_learning_rate",
                    {
                        str(group["name"]): group["lr"]
                        for group in optimiser.param_groups
                    },
                )

            held = evaluate(model, held_out, device)
            print(f"epoch {epoch + 1}  held-out loss {held:.4f}")
            if metrics is not None:
                metrics.log(eval_loss=held)
    finally:
        if metrics is not None:
            metrics.end("finished")


if __name__ == "__main__":
    main()
