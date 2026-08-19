# sparks

Training runs on your DGX Spark, with the curves next to the hardware they ran on.

Submit a training job from your laptop with one command. sparks builds your image, ships it to
the box, queues the run and supervises it. Your loop reports its own loss and learning rate, and
they land in Grafana beside the GPU power and temperature from the same minutes.

![A training run in Grafana](https://raw.githubusercontent.com/vtemian/sparks/main/examples/screenshots/lora.png)

## Before you run it

sparks provisions nothing. It needs a box already set up by
[sparkup](https://github.com/vtemian/sparkup): Prometheus with the remote-write receiver, Grafana,
an image registry and the queue runner. On your own machine you need Docker and SSH to the box.

The distribution is `sparks-dgx`; the command and the import are both `sparks`.

## Install

```sh
uv tool install sparks-dgx
sparks setup you@your-box
```

`setup` remembers the box, lets your Docker push to its registry, restarts Docker, and says when
it is ready. The restart stops whatever containers you have running.

## Submit a job

```sh
sparks submit --context ./examples --data ./examples/data \
  --name lora-r16 -- python /app/lora_finetune.py --epochs 12
```

`--data` is `/data`. Scripts are `/app/...`; cwd is the shared dir, not the image.

## Instrument the loop

```python
from sparks.emit import track

with track(total=epochs * len(loader), tokens_per_step=batch_size * BLOCK) as run:
    for batch in loader:
        loss = train_one(batch)
        run.step(loss=float(loss))
```

`run.step` derives `step`, `progress`, `eta_seconds` and the rates. Off the box `track` reports
nothing, so the same script runs on your laptop unguarded.

## Watch it

```sh
sparks queue            # what is running and waiting
sparks logs <job>       # what it printed
sparks status <job>     # state, exit code, duration, energy
sparks wait <job>       # block until it ends
```

The [README](https://github.com/vtemian/sparks#readme) has the rest, and
[INSTALL_CLAUDE.md](https://github.com/vtemian/sparks/blob/main/INSTALL_CLAUDE.md) has the
configuration and the traps.

MIT.
