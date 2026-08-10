# Installing and using sparks

Everything an agent needs to install sparks, submit a training run, and find out what
happened to it.

sparks owns training runs — emitting their metrics, launching them, queueing them. The box
itself (Prometheus, Grafana, the image registry, the queue container) is provisioned by
sparkup. If a box has never been converged by sparkup, nothing here will run: sparks reads
a contract file sparkup writes, and refuses to start without it.

## The two halves

`sparks` is the laptop client: it builds the job image, pushes it to the box registry,
uploads the data folder, and enqueues the job.

`fire` is the box server, running as the queue container's entrypoint: it drains the
spool, pulls the image, starts the job and honours cancel and abort.

Queue verbs typed on a laptop travel over SSH to `fire-ctl` on the box, which runs
`fire <verb>` inside the queue container. Bulk data goes by rsync, not through that path.

Supervision of each training container is internal (`python -m sparks.fire.supervise`,
called by `fire`). It is not on `PATH` and you never invoke it. There is no `sparks run`,
no `sparks-runner`, no `sparks demo`.

## Install

On a laptop, into any venv:

```sh
uv pip install -e .          # from a checkout
sparks --help                # confirms the client is on PATH
```

On the box, sparks ships inside the queue image; converge sparkup rather than installing
by hand. To put a working tree on the box for development:

```sh
make deploy                            # SPARKS_HOST and SPARKS_VENV from local.mk
make deploy SPARKS_HOST=you@your-box   # or inline
```

That rsyncs the tree, installs it into `SPARKS_VENV` (the venv your *training* code runs
in), and copies `monitoring/dashboards/` where Grafana reads them; it rescans within 10
seconds, no restart and no root. Neither `SPARKS_HOST` nor `SPARKS_VENV` has a default and
deploy refuses without them. It also refuses early if the box has no contract, or if your
`SPARKS_SHARED_DIR` disagrees with the box's.

`make deploy` does **not** update the queue server. `fire` ships as a container image:
push, let CI build the tag, then converge sparkup to pull it.

## Configure

**`SPARKS_HOST`** is how the client finds the box. Export it, or pass `--host`:

```sh
export SPARKS_HOST=you@your-box
```

**`/etc/sparks/box.toml`** is written by sparkup and read on the box. It carries the shared
directory, the textfile directory, the Prometheus URL, the Grafana URL and `registry_url`.
sparks does not guess any of those: if the file is missing or a promised path does not
exist, `fire` and the supervisor refuse to start and exit **78** (`EX_CONFIG`), which is
distinct so a queue can tell a misconfigured box from a crashed job.

**Laptop Docker must allow the plain-HTTP registry.** It is HTTP on purpose — the LAN trust
boundary is the SSH one. Without this, `docker push` fails and submit dies before the job is
reserved:

```json
{ "insecure-registries": ["your-box:5000"] }
```

Restart Docker afterwards, and use the same host:port that `registry_url` names.

**`local.mk`** (untracked; copy `local.mk.example`) overrides `SPARKS_HOST`,
`SPARKS_SHARED_DIR` and `SPARKS_VENV` for `make deploy`. `SPARKS_VENV` has no default and
deploy refuses without it: it is the venv your training code runs in, which belongs to a
project sparks knows nothing about.

## Submit a run

```sh
sparks submit --data ./corpus --name e0 -- python train.py --data /data
```

`--data` is required. That folder is uploaded into the job and mounted **read-only at
`/data`** (also `$SPARKS_DATA`) inside the container. Training code must read that path — a
script with a laptop path hard-coded will fail on the box even though submit succeeded.

The image is built from the current directory, or `--context`. Pass `--image` to skip the
build and reuse a tag already in the registry. The box never builds: `fire` only pulls, so
changing code means rebuilding and submitting from a laptop.

## Manage the queue

```sh
sparks queue            # what is running and waiting
sparks queue --all      # include finished jobs
sparks logs <job>       # the last 200 lines the job printed; --all for every line
sparks status <job>     # one job in full: state, exit code, duration, energy
sparks wait <job>       # block until it ends; exit 0 only if it finished
sparks cancel <job>     # drop a job that has not started
sparks abort <job>      # stop one whether or not it has started
sparks retry <job>      # resubmit, reusing the image and data already on the box
sparks remove <job>     # delete a finished job
```

`<job>` is a full job id, a unique fragment of one, or a job name. Ambiguity is refused
rather than guessed, except that one running job among several finished ones is taken to be
the one you meant. Only the account that submitted a job may control it; root may control
any — but anyone may read any job's `queue`, `logs` and `status`, as reading is not
controlling.

`queue --json` and `status --json` are the machine-readable forms, and are what a script or
an agent should parse: the plain output is a padded table meant for a person. `status --json`
carries three keys — `job` (what was submitted), `state` (where it is now), and `summary`
(the run's permanent record, `null` until the run ends).

`logs` needs the job to have reached a run: before that it fails and names the state, with
the runner's `detail` when there is one (a failed pull says so). When the launch itself
failed there is no container output, so `logs` prints `error.txt` under a
`sparks could not run this job:` heading rather than passing it off as the job's own.

`wait` exits **0 only when the job finished**, 1 when it ended any other way, and 75
(`EX_TEMPFAIL`) when `--timeout` expired with the job still going, so `sparks wait "$JOB" &&
next-step` is safe and a script can tell a broken job from a slow one. It polls
`status --json` client-side every `--interval` seconds (10 by default) rather than blocking
on the box: `capture` gives up on any single SSH call after 120s, so a server-side `wait`
could never outlive two minutes, and an hours-long connection is the first thing a network
blip kills.

## Driving sparks from an agent

`skills/` holds two Claude Code skills, `authoring-a-sparks-job` and
`operating-the-sparks-queue`, installed with `ln -s "$PWD"/skills/* ~/.claude/skills/`.

They are split because the triggers are different — writing training code is not the same
task as working out why last night's run died — and they cross-reference each other.

When one of them wants something the CLI cannot do, **add the verb rather than teaching the
skill to work around it.** `logs` and `status` exist for exactly that reason: the previous
answer was to parse a padded table for a run id and `ssh` a path assembled from the box
contract, which is knowledge a skill should never have to carry. The box already resolves
those paths, so a new reader belongs in `fire/ctl.py` beside `queue`, proxied from
`client/cli.py` the way `cancel` is.

## Instrument a run

Training code wraps its loop in `track` and reports one step at a time:

```python
from sparks.emit import track

with track(total=epochs * len(loader), tokens_per_step=batch_size * BLOCK) as run:
    for batch in loader:
        loss = train_one(batch)
        run.step(loss=float(loss))
```

`track` picks up the run id and Prometheus URL the supervisor exported, so training code
needs no arguments. Anywhere else it yields a run whose every call is a no-op, which is why
**the call site carries no `is not None` guard** and the same script runs on a laptop.
Keyword arguments other than `total`, `tokens_per_step` and `window` become labels on every
sample, and no other name is reserved.

`run.step` counts the step and derives `step`, `progress`, `eta_seconds`, `steps_per_sec` and
`tokens_per_sec`. `progress` is clamped to 1 and `eta_seconds` to 0, because a run that
overshoots its own `total` would otherwise report 167% complete on a panel bounded at 0-1. A value you pass wins over the derived one, so `run.step(step=global_step)`
reports your own numbering. What `track` was not given is not emitted rather than guessed: no
`total` means no `progress` and no `eta_seconds`, no `tokens_per_step` means no
`tokens_per_sec`. A guessed denominator draws a progress bar that lies, which is worse than an
empty panel. `epoch` is not derived at all; pass it.

Rates are measured over a sliding window of the most recent steps, so the first step reports
no rate: a rate needs two of them. One missing point at the start of a run is not a bug.

`run.log(...)` reports without advancing the step counter, which is what an `eval_loss` at the
end of an epoch wants. `run.log_group("training_learning_rate", {"lora": 2e-4, "tables": 2e-5})`
reports a value that differs per parameter group, and takes the **full** metric name, not the
short key.

`track` is the only way training code gets an emitter. There was a second one, `from_env`,
and it was deleted rather than kept alongside: two ways in means half the examples on the
internet show the one with the `is not None` guard. A framework callback owns no loop, but the
run `track` returns works without a `with` block, so hold it and call `run.end()` when the
framework says training is over.

Outside a job, when you own the run id, `RunMetrics` is the whole emitter:

```python
from sparks.emit import RunMetrics

with RunMetrics(run_id=..., url="http://127.0.0.1:9090", info={"model": "helium-2b"}) as m:
    m.log(step=1, loss=4.2)
```

The context manager records `crashed` if the loop raises. Every push is wrapped, so a
metrics outage cannot kill a training run.

`info` is immutable metadata carried on the run's info metric. `labels` are dimensions on
every sample: keep them few and low-cardinality.

Metric names must be declared in `sparks.metrics.METRICS`; `run.step(loss=…)` writes
`training_loss`. An undeclared name raises rather than being silently dropped, because a
metric no dashboard can query is worse than an error.

## Where the results are

Each run gets a directory under the box's shared directory: `summary.json` (the permanent
record — status, exit code, signal, duration, energy), `output.log`, and `error.txt` if the
launch itself failed. Those files are the source of truth; Prometheus is the live view.

`sparks_runs.prom` in node_exporter's textfile directory is rebuilt from the summaries after
every run, so the index survives losing the TSDB. The live queue is `sparks_queue.prom`
beside it.

Grafana has one board, `training-runs`, with a `$run_id` variable. The dropdown is scoped to
the dashboard's time range, so with the default `now-3h` it lists recent runs only —
widening the range is what surfaces older ones.

## When something goes wrong

**Exit 78 from `fire` or the supervisor** means the box is not configured: `/etc/sparks/box.toml`
is missing, unreadable, or promises a path that does not exist. Converge sparkup; do not
work around it.

**Submit fails at push** — check `insecure-registries` above, and that `SPARKS_HOST` is
reachable. The error names the tag that failed.

**A job fails immediately with a pull error** — `sparks status <job>` says so in `detail`,
and `pull.log` in the job directory has the registry's own output. The usual cause is a tag
that was never pushed.

**A run shows on the dashboard with no end and no status** — that is a run whose record was
never written. Check the job directory is writable by the submitting account; a full disk or
a wrong owner is the usual cause.

**A finished run's curves flat-line for five minutes** rather than stopping dead. Pushed
series are not marked stale automatically, so a killed run's non-lifecycle metrics hold
their last value for the lookback window. This is expected.

## Develop

```sh
make check   # format, lint, mypy strict, tests, the custom checkers, dashboards
make live    # the same against a real Prometheus in Docker
make on-box  # the checks that only mean anything on real hardware; run ON the box
```

`make check` needs no Docker and is what CI runs. `make live` is required for anything
touching the emitter's threading or the launch and supervise seam: no unit test can catch a
second writer on a metric series.
