# Example

A LoRA fine-tune of `SmolLM2-135M` against the corpus in `data/`, on a hand-written loop with no
`Trainer`. That is the shape sparks was built for: `track` wraps the loop and each `run.step`
reports one step, deriving progress, ETA and throughput rather than waiting for a callback the
framework owns.

```sh
sparks submit --context ./examples --data ./examples/data \
  --name lora-r16 -- python /app/lora_finetune.py --epochs 12
```

![the run in Grafana](screenshots/lora.png)

That is a real run: 2040 steps over twelve epochs, twenty-three minutes on the box.

The command names the script by absolute path because the container's working directory is the
box's shared directory, not the image's. `--data` is mounted read-only at `/data`, also
`$SPARKS_DATA`, and the corpus is synthetic — generated rather than collected, so the example needs
no download and carries no licence. Point it at your own text and it will read every `.txt` there.

Three things in the `Dockerfile` are worth copying into your own, each of which failed a run first.
It needs a C compiler, because torch builds Triton kernels on the first backward pass even though
nothing compiles at image time. It needs `HOME` on a writable path, because the job runs as
whoever submitted it rather than as root, and the Triton cache otherwise lands on `/`. And the
weights are baked in rather than pulled per run, so an outage at Hugging Face cannot fail a job.

The learning-rate panel carries two series because the adapter and the layer norms beneath it train
an order of magnitude apart, and one averaged series would describe neither. That is what
`run.log_group` is for. The held-out loss goes through `run.log`, which reports without advancing
the step counter, because an eval at the end of an epoch is not a training step.

One thing to know before your own first run: Grafana floors its query step at the scrape interval,
so anything shorter than a couple of minutes arrives as a single averaged point no matter how
densely you push.
