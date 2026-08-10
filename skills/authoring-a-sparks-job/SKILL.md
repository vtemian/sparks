---
name: authoring-a-sparks-job
description: Use when writing or preparing training code to run on a sparks box - instrumenting a loop with sparks.emit, writing the job Dockerfile, and submitting with sparks submit. Covers the closed metric vocabulary, the read-only /data mount, where output survives, and the container traps that make a job fail on the box after submit already succeeded.
---

# Authoring a sparks job

A job is three artifacts: a training script that reports through `sparks.emit`, a
`Dockerfile` in the build context that carries it, and one `sparks submit` command.

The client builds the image on a laptop and pushes it to the box registry. **The box never
builds.** Changing code means rebuilding and submitting again; there is no way to patch a
running job.

## Instrument the loop

Wrap the loop in `track`, and report one step at a time:

```python
from sparks.emit import track

with track(total=epochs * len(loader), tokens_per_step=batch_size * BLOCK, arm="lora") as run:
    for batch in loader:
        loss = train_one(batch)
        run.step(loss=float(loss))
```

Extra keyword arguments to `track` become labels on every sample.

`run.step` counts the step and derives `step`, `progress`, `eta_seconds`, `steps_per_sec` and
`tokens_per_sec` from it. **A value you pass wins over the derived one**, so
`run.step(step=global_step)` reports your own numbering.

What `track` was not given is not emitted, rather than guessed:

- no `total`, no `progress` and no `eta_seconds`. A guessed denominator draws a progress bar
  that lies, which is worse than an empty panel.
- no `tokens_per_step`, no `tokens_per_sec`.
- `epoch` is never derived. Pass it yourself: `run.step(loss=…, epoch=epoch + 1)`.

Rates are measured over a sliding window of the most recent steps, so **the first step reports
no rate at all**: a rate needs two of them. One missing point at the start of a run is not a
broken emitter.

Two steps landing in the same millisecond keep only the first: a series carries one value per
millisecond and the buffer drops the rest. A loop running faster than 1kHz is downsampled
rather than refused, so `training_step` shows gaps. Report every N steps if that matters.

`run.log(...)` reports without advancing the step counter: an `eval_loss` at the end of an
epoch is not a training step. `run.log_group(...)` is below.

Off the box, `track` yields a run whose every call is a no-op, so the same script runs on a
laptop **with no `is not None` guard around it**. Leaving the `with` block flushes and stops
the pump; an exception inside it propagates rather than being swallowed.

Outside a container, when you own the run id, use `RunMetrics` directly:

```python
from sparks.emit import RunMetrics

with RunMetrics(run_id="local-1", url="http://box:9090", info={"model": "helium-2b"}) as m:
    m.log(step=1, loss=4.2)
```

The context manager records `crashed` if the loop raises. Every push is wrapped, so a
metrics outage can never kill a training run.

`info` is immutable metadata carried on the run's info metric. `labels` are dimensions on
every sample: keep them few and low-cardinality.

### The metric names are a closed set

`run.step(loss=…)` writes `training_loss`: the key is the metric name minus its `training_`
prefix, in `step`, `log` and `RunMetrics.log` alike. A name not declared in `sparks.metrics.METRICS` raises `KeyError` rather than being
silently dropped, because a metric no dashboard can query is worse than an error.

Everything a training script may log:

```
progress         fraction of the whole job complete, 0-1
eta_seconds      estimated seconds remaining
epoch            fractional epoch
step             optimizer steps since this run began
loss             training loss for the last batch
grad_norm        gradient L2 norm
learning_rate    learning rate
steps_per_sec    optimizer steps per second over the last window
tokens_per_sec   tokens per second over the last window
eval_loss        held-out loss
```

Anything else in `METRICS` belongs to the box (`sparks_*`) or to the supervisor
(`training_run_*`). Check `src/sparks/metrics.py` before assuming a name exists.

### Values that differ per parameter group

```python
run.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})
```

Full metric name here, not the short key. The label is always `group`, whatever the metric.
Use it wherever one averaged series would describe neither half — a LoRA adapter and the
layer norms beneath it train at learning rates an order of magnitude apart, and a single
`training_learning_rate` series describes neither.

### What a child emitter must never write

The supervisor owns the run's lifecycle. A child emitter, which is what both `track` and
`from_env` give you inside a job, refuses `training_run_info`,
`training_run_start_timestamp_seconds`, `training_run_heartbeat_timestamp_seconds`,
`training_run_end_timestamp_seconds`, `training_run_status` and `training_run_active`. Two
writers on one series is a 400 from the remote-write receiver that rolls back the whole
batch — losing your metrics too, not just the duplicated one.

Leaving the `track` block, or calling `end()` on a `from_env` emitter, flushes and stops the
pump. `end`'s `status` argument is **ignored**: the supervisor decides the status from how the
process exited.

### Bridging a framework

sparks is deliberately not a `TrainerCallback` — it is a plain object a loop calls, which
works in both worlds. A callback owns no loop, so there is nothing for `track` to wrap: take
the emitter from the environment instead.

```python
from sparks.emit import from_env

metrics = from_env(arm="lora")          # extra kwargs become labels on every sample
...
if metrics is not None:
    metrics.log(**values)
```

`from_env` returns **None** anywhere but inside a job container. It reads `SPARKS_RUN_ID` and
`SPARKS_PROMETHEUS_URL`, which only the supervisor sets, so `from_env().log(...)` raises
`AttributeError` the first time anyone runs the script on a laptop. Guard it. Sparing you that
guard is most of what `track` is for, so reach for `from_env` only where there is no loop to
wrap.

Whichever way in, **map the framework's log keys to declared names explicitly**:

```python
LOGGED = {"loss": "loss", "eval_loss": "eval_loss", "learning_rate": "learning_rate"}

values = {
    LOGGED[key]: float(value)
    for key, value in logs.items()
    if key in LOGGED and isinstance(value, int | float)
}
```

Passing the framework's dict straight through raises on the first key sparks has never heard
of, and those keys vary by framework version and by task. A framework also tends to report
neither pace nor throughput, so `progress`, `eta_seconds` and `tokens_per_sec` have to be
derived in the adapter, which is the only place that knows how many tokens a step consumed.
That derivation is exactly what `run.step` does for you when you own the loop.

## Write the Dockerfile

These are the facts that make a correct-looking image fail on the box even though `submit`
reported success.

- **The working directory is the box's shared directory, not your image's `WORKDIR`.** Name
  scripts by absolute path: `python /app/train.py`. A relative path resolves against the
  shared directory and is not found.
- **The job runs as the submitting account's uid**, which does not exist in the image's
  `/etc/passwd`. Set `ENV HOME=/tmp` and any tool-specific cache directory
  (`TRITON_CACHE_DIR`, `HF_HOME`) somewhere writable, or the toolchain writes to `/` and the
  run dies — for torch, after the first backward pass, which looks like a mid-training
  crash rather than a permissions problem.
- **`/data` is read-only.** That is where `--data` lands, and `$SPARKS_DATA` names it. A
  script with a laptop path hard-coded fails on the box.
- **sparks must be installed in the image**, or `from sparks.emit import track` fails at
  import. A slim base has no `git`, so `pip install git+https://...` fails there; install the
  tarball instead, and pin a tag rather than a branch in anything you care about.
- **Bake model weights in.** A container that reaches Hugging Face on every start turns an
  outage there into a failed run. Set `HF_HUB_OFFLINE=1` once they are baked; it also stops
  the hub writing cache-miss markers into a read-only tree.
- **Match the box's CUDA.** The DGX Spark is GB10, compute capability sm_121, driver CUDA
  13.0. A cu128 torch build has no kernels for it and falls back to the CPU *silently* — the
  run looks healthy and simply never finishes.
- **Put the layers most likely to change last.** Multi-gigabyte torch layers above a
  frequently-edited one get pushed again on every submit.

Every one of those points failed a real run before it was written down. A minimal image that
satisfies all of them, for a job with no ML dependencies:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    "https://github.com/vtemian/sparks/archive/refs/heads/main.tar.gz"
ENV HOME=/tmp
COPY *.py /app/
```

Do not build `FROM` an image you found in the box registry. Those belong to other people's
jobs, they are not a base image, and the tag you borrowed can be overwritten or removed under
you. Start from a public base and install sparks yourself.

A larger instance carrying torch, CUDA and baked weights is `examples/Dockerfile` in the
sparks repository, which is worth reading before writing a GPU image of your own.

## Where output survives

The container is removed when the run ends, so anything written inside it is gone. Two
places persist:

- **stdout and stderr** land in the run's `output.log`, which `sparks logs` reads.
  `PYTHONUNBUFFERED=1` is already set, so prints arrive as they happen.
- **The shared directory** is mounted read-write at its own path and is the working
  directory. The run's own directory is:

  ```python
  checkpoints = Path.cwd() / "runs" / os.environ["SPARKS_RUN_ID"]
  ```

  Write checkpoints there. Anywhere else in the container is discarded.

## Submit

```sh
sparks submit --data ./corpus --name e0 -- python /app/train.py --epochs 3
```

- `--data` is **required** even for a job that reads nothing; pass an empty folder.
- `--context` defaults to the current directory and must contain a `Dockerfile`.
- `--image <tag>` skips build and push entirely and reuses a tag already in the registry.
- Everything after `--` is the command, run inside the container.

Submit prints the job id on stdout and nothing else — build and push progress go to stderr —
so `JOB=$(sparks submit --data ./corpus --name e0 -- python /app/train.py)` means what it
looks like.

If it dies at push, Docker on this machine is not allowed to talk to the box's plain-HTTP
registry. `sparks setup` fixes that; it needs a Docker restart afterwards.

## Prove the path before you spend an hour on it

Most of the ways a job fails are plumbing, not modelling, and every one of them is cheaper to
find with a script that does no training. Before the real submit, send a job whose only work
is to prove the path — it imports sparks, reads `/data`, pushes a few points, and exits:

```python
import os, pathlib, time
from sparks.emit import track

print("data:", sorted(pathlib.Path(os.environ["SPARKS_DATA"]).iterdir()))
with track(total=20) as run:
    for step in range(20):
        run.step(loss=1.0 / (step + 1))
        time.sleep(0.5)
```

```sh
JOB=$(sparks submit --data ./corpus --name smoke -- python /app/smoke.py)
sparks wait "$JOB" && sparks logs "$JOB"
```

That exercises build, push, pull, the uid the container runs as, the `/data` mount and the
metrics path — everything except your model. A failure here is never the training code.

Grafana floors its query step at the scrape interval, so a run shorter than a couple of
minutes arrives as one averaged point no matter how densely you push. Do not read that as a
broken emitter.

Once it works end to end, submit the real one. To follow it and to work out what went wrong,
use the `operating-the-sparks-queue` skill.
