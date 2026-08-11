# Installing and using sparks

Everything an agent needs to install sparks and its skills, submit a training run, and find
out what happened to it.

sparks owns training runs: emitting their metrics, launching them, queueing them. The box
itself (Prometheus, Grafana, the image registry, the queue container) is provisioned by
sparkup. If a box has never been converged by sparkup, nothing here will run: sparks reads a
contract file sparkup writes, and refuses to start without it.

`sparks` is the laptop client. `fire` is the box server, running as the queue container's
entrypoint; you never invoke it, and neither the supervisor
(`python -m sparks.fire.supervise`) nor any `sparks run`, `sparks-runner` or `sparks demo`
exists to be called. Queue verbs typed on a laptop travel over SSH to `fire-ctl` on the box,
which runs `fire <verb>` inside the queue container. Bulk data goes by rsync, not that path.

## Install the client

On a laptop, as a tool rather than into a project venv: the client runs from your own
training project and needs nothing from a sparks checkout.

```sh
uv tool install git+https://github.com/vtemian/sparks@v0.1.0
sparks setup you@your-box
```

`setup` reads the box's contract, remembers the box, writes its registry into Docker's
`daemon.json`, restarts Docker and verifies the registry took. It restarts only on macOS,
where no password is needed; elsewhere it prints the command. A box it cannot reach is not
remembered.

Once the package is on PyPI this becomes `uv tool install sparks-dgx`. The distribution is
`sparks-dgx` because PyPI's `sparks` is held by a project abandoned in 2019; the command and
the import stay `sparks`, so the odd name is seen once.

Pin the tag. Without one you track `main`, and Docker's layer cache keeps serving whatever
sparks an image was first built with, however far main has moved. The `git+` form shells out
to `git`; on a machine without it,
`uv tool install https://github.com/vtemian/sparks/archive/refs/tags/v0.1.0.tar.gz` is the
same package. From a checkout, `make install` does the tool install and `sparks setup`.

On the box, sparks ships inside the queue image. Converge sparkup rather than installing by
hand; `make deploy` does **not** update the queue server.

## Install the skills

`skills/` holds two Claude Code skills: `authoring-a-sparks-job` for writing training code
and its Dockerfile, `operating-the-sparks-queue` for watching and diagnosing runs. They are
split because the triggers differ, and they cross-reference each other.

```sh
ln -s "$PWD"/skills/* ~/.claude/skills/
```

Read `authoring-a-sparks-job` before writing a job. It carries the container traps that make
a correct-looking image fail on the box after submit already reported success.

## Configure

**Which box** the client talks to: `--host` wins, then `SPARKS_HOST`, then whatever `setup`
remembered in `~/.config/sparks/config.toml` (`$XDG_CONFIG_HOME` if set). The stored host is
only ever a fallback, so `SPARKS_HOST=other-box sparks queue` reaches another box without
editing anything.

**`/etc/sparks/box.toml`** is written by sparkup and read on the box. It carries the shared
directory, the textfile directory, the Prometheus URL, the Grafana URL and `registry_url`.
sparks guesses none of those: if the file is missing or a promised path does not exist,
`fire` and the supervisor exit **78** (`EX_CONFIG`), which is distinct so a queue can tell a
misconfigured box from a crashed job.

**Laptop Docker must allow the plain-HTTP registry.** `sparks setup` does this for you. By
hand it is the same host:port that `registry_url` names, followed by a Docker restart:

```json
{ "insecure-registries": ["your-box:5000"] }
```

**`local.mk`** (untracked; copy `local.mk.example`) sets `SPARKS_HOST`, `SPARKS_SHARED_DIR`
and `SPARKS_VENV` for `make deploy`. `SPARKS_VENV` has no default and deploy refuses without
it: it is the venv your training code runs in, which belongs to a project sparks knows
nothing about.

## Submit a run

```sh
sparks submit --data ./corpus --name e0 -- python /app/train.py --data /data
```

`--data` is required. That folder is uploaded into the job and mounted **read-only at
`/data`** (also `$SPARKS_DATA`). Training code must read that path: a script with a laptop
path hard-coded fails on the box even though submit succeeded.

Name the script by absolute path. The container's working directory is the box's shared
directory, not the image's, so a bare `python train.py` does not resolve.

The image is built from the current directory, or `--context`. Pass `--image` to skip the
build and reuse a tag already in the registry. The box never builds: `fire` only pulls, so
changing code means rebuilding and submitting from a laptop.

## Manage the queue

```sh
sparks queue            # what is running and waiting
sparks queue --all      # include finished jobs
sparks logs <job>       # the last 200 lines the job printed; --all for every line
sparks status <job>     # one job in full: state, exit code, duration, energy
sparks wait <job>       # block until it ends
sparks cancel <job>     # drop a job that has not started
sparks abort <job>      # stop one whether or not it has started
sparks retry <job>      # resubmit, reusing the image and data already on the box
sparks remove <job>     # delete a finished job
```

`<job>` is a full job id, a unique fragment of one, or a job name. Ambiguity is refused
rather than guessed, except that one running job among several finished ones is taken to be
the one you meant. Only the account that submitted a job may control it; root may control
any. Anyone may read any job's `queue`, `logs` and `status`.

`queue --json` and `status --json` are what a script or an agent should parse; the plain
output is a padded table meant for a person. `status --json` carries three keys: `job` (what
was submitted), `state` (where it is now), and `summary` (the permanent record, `null` until
the run ends).

`wait` exits **0 only when the job finished**, 1 when it ended any other way, and 75
(`EX_TEMPFAIL`) when `--timeout` expired with the job still going, so `sparks wait "$JOB" &&
next-step` is safe and a script can tell a broken job from a slow one.

`logs` needs the job to have reached a run: before that it fails and names the state, with
the runner's `detail` when there is one. When the launch itself failed there is no container
output, so `logs` prints `error.txt` under a `sparks could not run this job:` heading rather
than passing it off as the job's own.

## Instrument a run

```python
from sparks.emit import track

with track(total=epochs * len(loader), tokens_per_step=batch_size * BLOCK) as run:
    for batch in loader:
        loss = train_one(batch)
        run.step(loss=float(loss))
```

`track` picks up the run id and Prometheus URL the supervisor exported, so training code
needs no arguments. Anywhere else it reports nothing, which is why **the call site carries no
`is not None` guard**. It still checks names, labels and values there, so a typo fails on a
laptop rather than on the box.

`run.step` derives `step`, `progress`, `eta_seconds`, `steps_per_sec` and `tokens_per_sec`.
A value you pass wins over the derived one. What `track` was not given is not emitted rather
than guessed: no `total` means no `progress` and no `eta_seconds`, no `tokens_per_step` means
no `tokens_per_sec`. `epoch` is not derived; pass it. Rates come from a sliding window, so
the first step reports none.

`run.log(...)` reports without advancing the step counter, which is what an `eval_loss` at
the end of an epoch wants. A value that differs per group is a mapping rather than a number,
`run.step(learning_rate={"adapter": 2e-4, "norms": 2e-5})`, giving one series per group
labelled `group`. Any declared metric takes one, but a metric may not change shape mid-run.

Metric names are a closed set in `sparks.metrics.METRICS`; `run.step(loss=…)` writes
`training_loss`, and an undeclared name raises. Keyword arguments other than `total`,
`tokens_per_step` and `window` become labels, except `run_id` and `group`, which are the
emitter's and are refused.

A framework callback owns no loop, but the run `track` returns works without a `with` block:
hold it and call `run.end()` when training is over. Build it once — two `track` calls in one
process are two writers on one series.

## Where the results are

Each run gets a directory under the box's shared directory: `summary.json` (the permanent
record — status, exit code, signal, duration, energy), `output.log`, and `error.txt` if the
launch itself failed. Those files are the source of truth; Prometheus is the live view.

`sparks_runs.prom` in node_exporter's textfile directory is rebuilt from the summaries after
every run, so the index survives losing the TSDB. The live queue is `sparks_queue.prom`
beside it.

Grafana has one board, `training-runs`, with a `$run_id` variable. The dropdown is scoped to
the dashboard's time range, so with the default `now-3h` it lists recent runs only; widening
the range is what surfaces older ones.

## When something goes wrong

**Exit 78 from `fire` or the supervisor** means the box is not configured:
`/etc/sparks/box.toml` is missing, unreadable, or promises a path that does not exist.
Converge sparkup; do not work around it.

**Submit fails at push** — check `insecure-registries` above, and that `SPARKS_HOST` is
reachable. The error names the tag that failed.

**A job fails immediately with a pull error** — `sparks status <job>` says so in `detail`,
and `pull.log` in the job directory has the registry's own output. The usual cause is a tag
that was never pushed.

**A job fails on `import sparks`** — the image installed sparks from a branch URL, and
Docker's layer cache served the version it was first built with. Pin a tag.

**A run shows on the dashboard with no end and no status** — that run's record was never
written. Check the job directory is writable by the submitting account; a full disk or a
wrong owner is the usual cause.

**A finished run's curves flat-line for five minutes** rather than stopping dead. Pushed
series are not marked stale automatically, so a killed run's non-lifecycle metrics hold their
last value for the lookback window. This is expected.
