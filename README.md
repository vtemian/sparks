# sparks

Training runs on the DGX Spark, with the curves in Grafana next to the hardware they ran on.

Needs a box provisioned by [sparkup](https://github.com/vtemian/sparkup): Prometheus with the
remote-write receiver, Grafana on `http://spark.local`, a local image registry, and the queue
runner. sparkup's `sparks` role writes `/etc/sparks/box.toml` (shared directory, textfile
directory, Prometheus URL, `registry_url`), and its registry role is what jobs push to.

## Client (laptop)

From a laptop with Docker and ssh:

```sh
export SPARKS_HOST=vlad@spark.local
sparks submit --data ./corpus --name e0 -- python train.py --data /data
sparks queue
```

The client builds the image from the current directory (or `--context`), pushes it to the box
registry, uploads `--data` into the job, and enqueues it. Inside the container that folder is
mounted at `/data` (`$SPARKS_DATA`); training code should read that path, not a laptop path.

The registry is plain HTTP on the LAN (same trust boundary as ssh). Docker on the laptop must
allow it, e.g. in `daemon.json`:

```json
{ "insecure-registries": ["spark.local:5000"] }
```

Then restart Docker Desktop / dockerd. Use the same host:port as `registry_url` in the box
contract.

```sh
sparks queue            # what's running and waiting
sparks cancel <job>     # drop a job that has not started
sparks abort <job>      # stop one whether or not it has started
sparks remove <job>     # delete a finished job
```

## Server (box)

The queue container runs `fire` (image ENTRYPOINT). It pulls the job image,
mounts `--data` at `/data`, and supervises the run. You do not install or invoke
a separate wrap binary.

## Instrument a run

```python
from sparks.emit import RunMetrics

with RunMetrics(run_id=..., url="http://127.0.0.1:9090", info={"model": ...}) as m:
    for step, batch in enumerate(batches):
        ...
        m.log(step=step, loss=float(loss))
```

`with` records `crashed` if the loop raises. Every push is wrapped, so a metrics outage cannot kill
a training run.

`info` is immutable metadata for the info metric. For a value that differs per parameter group, use
`m.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})`.

## Deploy to the box

```sh
make deploy                              # SPARKS_HOST defaults to spark.local
make deploy SPARKS_HOST=you@spark.local  # if your SSH login differs
```

Pushes the tree, installs it into the training venv, and hands Grafana both dashboards. Grafana picks
them up within 10 seconds; no restart, no root. The image registry and queue runner come from
sparkup (`make apply` there), not from this deploy.

## Develop

```sh
make check   # lint, mypy strict, tests, dashboard
make live    # the same against a real Prometheus in Docker
```
