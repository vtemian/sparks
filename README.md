# sparks

Training runs on the DGX Spark, with the curves in Grafana next to the hardware they ran on.

Needs a box provisioned by [sparkup](https://github.com/vtemian/sparkup): Prometheus with the
remote-write receiver, Grafana, a local image registry, and the queue runner. sparkup's `sparks`
role writes `/etc/sparks/box.toml` (shared directory, textfile directory, Prometheus URL, Grafana
URL, `registry_url`), and sparks refuses to start without it rather than guessing those paths.

Two halves. `sparks` is the client you run on a laptop; `fire` is the server, running as the queue
container's entrypoint on the box. You never invoke `fire` directly.

---

# Using it

Install the client into any venv:

```sh
uv pip install -e .
```

Point it at your box. This is an environment variable, read by the client itself:

```sh
export SPARKS_HOST=you@your-box
```

Submit a run, then watch the queue:

```sh
sparks submit --data ./corpus --name e0 -- python train.py --data /data
sparks queue
```

The client builds an image from the current directory (or `--context`), pushes it to the box
registry, uploads `--data` into the job, and enqueues it. Inside the container that folder is
mounted read-only at `/data` (also `$SPARKS_DATA`); training code must read that path, not a
laptop path. Pass `--image` to skip the build and reuse a tag already in the registry.

```sh
sparks queue            # what is running and waiting
sparks queue --all      # include finished jobs
sparks cancel <job>     # drop a job that has not started
sparks abort <job>      # stop one whether or not it has started
sparks retry <job>      # resubmit, reusing the image and data already on the box
sparks remove <job>     # delete a finished job
```

`<job>` is a full id, a unique fragment of one, or a job name. Ambiguity is refused rather than
guessed at.

[`examples/`](examples/) has three runnable starting points: a dependency-free smoke run to prove
the path works, a hand-written LoRA loop, and a HuggingFace `Trainer` callback.

The registry is plain HTTP on the LAN, the same trust boundary as ssh, so Docker on the laptop
must be told to allow it. In `daemon.json`, using the same host:port as `registry_url` in the box
contract:

```json
{ "insecure-registries": ["your-box:5000"] }
```

Restart Docker afterwards. Without this, `docker push` fails and submit dies before the job is
reserved.

## Instrumenting a run

```python
from sparks.emit import RunMetrics

with RunMetrics(run_id=..., url="http://127.0.0.1:9090", info={"model": "helium-2b"}) as m:
    for step, batch in enumerate(batches):
        ...
        m.log(step=step, loss=float(loss))
```

The context manager records `crashed` if the loop raises, and every push is wrapped, so a metrics
outage cannot kill a training run. Inside a job container `sparks.emit.from_env` picks up the run
id and Prometheus URL the supervisor exported, so training code needs no arguments.

Metric names must be declared in `sparks.metrics.METRICS`; `m.log(loss=…)` writes `training_loss`.
An undeclared name raises rather than being silently dropped.

---

# Developing it

```sh
make check   # format, lint, mypy strict, tests, the house rules, dashboards
make live    # the same against a real Prometheus in Docker
```

`make check` needs no Docker and is what CI runs. `make live` is required for anything touching
the emitter's threading or the launch/supervise seam: no unit test can catch a second writer on a
metric series.

Which box the Makefile talks to lives in `local.mk`, untracked. Copy `local.mk.example` and edit
it. The Makefile exports `SPARKS_HOST`, so `make sparks ARGS="queue"` uses the same value as
`make deploy` — one source of truth for a checkout. Setting it in your shell instead is fine and
is what the client alone needs.

`monitoring/` holds what the box needs to display a run: `dashboards/` for Grafana, `alerts/` for
Prometheus. Both are checked by `make check` against the metrics this code actually emits, so a
panel querying something nothing produces fails here rather than showing an empty graph.

## Getting a change onto the box

Three separate paths, depending on what you changed.

**The queue server (`fire`, and anything it imports).** It ships as a container image. Push to
`main`, let CI build `ghcr.io/…/sparks:main`, then converge sparkup, which pulls the tag and
recreates the runner. `make deploy` does **not** update it.

**The metrics library (`sparks.emit`), for training code to import.** That is what `make deploy`
is for: it rsyncs the tree and installs it into `SPARKS_VENV`, the venv your training project
runs in. There is no default for that path and deploy refuses without it, because it belongs to a
project this one knows nothing about.

**Dashboards and alerts.** `make deploy` also copies `monitoring/dashboards/` to the shared
directory Grafana watches; it rescans within ten seconds, no restart and no root. The alert rules
are vendored into sparkup and land with a converge.

---

MIT. See [LICENSE](LICENSE).
