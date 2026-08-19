<h1 align="center">sparks</h1>

<p align="center">
  Training runs on your DGX Spark, with the curves next to the hardware they ran on.
</p>

---

Submit a training job from your laptop with one command. sparks builds your image, ships it to the
box, queues the run and supervises it. Your loop reports its own loss and learning rate, and they
land in Grafana beside the GPU power and temperature from the same minutes.

<p align="center">
  <img src="examples/screenshots/lora.png" alt="A training run in Grafana" />
</p>

## Before you run it

sparks provisions nothing. It needs a box already set up by
[sparkup](https://github.com/vtemian/sparkup): Prometheus with the remote-write receiver, Grafana,
an image registry and the queue runner. On your own machine you need Docker and SSH to the box.

## Install

```sh
uv tool install sparks-dgx
sparks setup you@your-box
```

`setup` remembers the box, lets your Docker push to its registry, restarts Docker, and says when it
is ready. The restart stops whatever containers you have running.

## Submit a job

[`examples/`](examples/) is a real LoRA fine-tune, and the run pictured above:

```sh
sparks submit --context ./examples --data ./examples/data \
  --name lora-r16 -- python /app/lora_finetune.py --epochs 12
```

`--data` is `/data`. Scripts are `/app/...`; cwd is the shared dir, not the image.

`--env KEY=VALUE` sets a variable in the job, and everyone on the box can read it. For a token use
`--secret KEY`, which takes the value from your shell and keeps it out of every record.

## Instrument the loop

```python
from sparks.emit import track

with track(total=epochs * len(loader), tokens_per_step=batch_size * BLOCK) as run:
    for batch in loader:
        loss = train_one(batch)
        run.step(loss=float(loss))
```

`run.step` derives `step`, `progress`, `eta_seconds` and the rates. A mapping is one series per group:
`run.step(learning_rate={"adapter": 2e-4, "norms": 2e-5})`.

Off the box `track` reports nothing, so the same script runs on your laptop unguarded. Metric names
are a closed set in `sparks.metrics.METRICS` and are checked either way, so a typo fails where you
can see it rather than on the box.

## Watch it

```sh
sparks queue            # what is running and waiting
sparks logs <job>       # what it printed
sparks status <job>     # state, exit code, duration, energy
sparks wait <job>       # block until it ends
sparks cancel <job>     # drop one that has not started
sparks abort <job>      # stop one that has
sparks retry <job>      # submit it again
sparks remove <job>     # delete a finished one
```

`<job>` is an id, a unique fragment of one, or a name. `queue` and `status` take `--json`.

## Ask an agent instead

`setup` installs two skills into `~/.claude/skills` and `~/.agents/skills`, where Claude Code,
Codex and Cursor look:

- `authoring-a-sparks-job` — instrument the loop, write the job Dockerfile, submit.
- `operating-the-sparks-queue` — watch a run, diagnose a failure, stop or resubmit it.

There is nothing to invoke. Ask for what you want — *"train this on the box and tell me when it
breaks"* — and the agent reads the right one and runs the commands above.

## Developing it

```sh
make check   # lint, mypy strict, tests, house rules, dashboards
make live    # the same against a real Prometheus in Docker
```

[INSTALL_CLAUDE.md](INSTALL_CLAUDE.md) has the rest: configuration, the three separate ways a change
reaches the box, and the traps.

---

MIT. See [LICENSE](LICENSE).
