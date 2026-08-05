# sparks

Training runs on the DGX Spark, with the curves in Grafana next to the hardware they ran on.

Needs a box provisioned by [sparkup](https://github.com/vtemian/sparkup): Prometheus with the
remote-write receiver, and Grafana on `http://spark.local`. sparkup's `sparks` role provisions the
rest and writes `/etc/sparks/box.toml`, which is where this framework learns the box's shared
directory, textfile directory and Prometheus URL.

Without that file `sparks run` refuses to start, exiting 78, rather than recording a run into a
directory nothing reads. On a machine sparkup does not manage, say where things are explicitly:

```sh
sparks run --shared-dir /tmp/runs --url "" -- python train.py   # empty url: no telemetry
```

## Watch a synthetic run

```sh
sparks demo --name acceptance
```

Prints a run id and a Grafana link. Loss, held-out loss, gradient norm and learning rate per
parameter group, throughput, and the GPU row underneath.

## Instrument a real one

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
them up within 10 seconds; no restart, no root.

## Develop

```sh
make check   # lint, mypy strict, tests, dashboard
make live    # the same against a real Prometheus in Docker
```
