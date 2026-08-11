---
name: operating-the-sparks-queue
description: Use when watching, diagnosing, stopping or resubmitting training jobs already on a sparks box - reading sparks queue, logs and status, working out why a run failed, and cancel/abort/retry/remove. Covers the machine-readable --json forms, the states, what each failure detail actually means, which Grafana board answers which question, and how to tell a stuck runner from a busy one.
---

# Operating the sparks queue

Every verb here runs on a laptop and travels over SSH to the box. Which box, in order:
`--host`, then `$SPARKS_HOST`, then whatever `sparks setup you@your-box` wrote to
`~/.config/sparks/config.toml`. What setup remembered is the fallback and never an override,
so a one-off `SPARKS_HOST` reaches another box without editing a file. There is no local
mode: a command that says there is no box yet never reached one at all.

## Read the queue

```sh
sparks queue          # live jobs, plus any that finished in the last 6 hours
sparks queue --all    # every job still on disk
sparks queue --json   # the same, as one object per job
```

**Parse `--json`, never the plain output.** The plain form is a space-padded table meant for
a person; a job named `running` would be misread as a state. Each row carries `job_id`,
`name`, `user`, `state`, `run_id`, `image`, `command`, `submitted_unix`, `started_unix`,
`finished_unix`, `exit_code` and `detail`.

States: `queued`, `building` and `running` are live. `finished`, `failed`, `cancelled` and
`aborted` are terminal. `unknown` means the state file could not be read, and the runner
will refuse to start that job rather than risk running it twice.

A job that finished more than six hours ago is gone from plain `queue`, but it is still on
disk: `queue --all` lists it, and `logs` and `status` resolve it by name or id as usual. An
empty `queue` is not evidence that a job never existed.

The runner takes **one job at a time**, waiting for it to finish before starting the next, and
gives that job every GPU on the box. A job sitting at `queued` is not stuck; it is behind the
one that is running.

## Naming a job

Every verb takes a full job id, a unique fragment of one, or the `--name` given at submit.
Ambiguity is refused rather than guessed at, with one exception: a single running job among
several finished ones is taken to be the one you meant.

## Follow a run

```sh
sparks wait <job>                    # blocks until it ends
sparks wait <job> --timeout 3600     # ...but not forever
```

**The exit code is the point.** 0 only when the job `finished`; 1 when it ended any other way
(`failed`, `cancelled`, `aborted`); 75 (`EX_TEMPFAIL`) when `--timeout` ran out with the job
still going. Those last two are deliberately different: a script has to be able to tell "it
broke" from "it is taking longer than I allowed". So this means what it looks like:

```sh
sparks wait "$JOB" && ./collect-results.sh
```

The terminal state goes to stdout; each state change is announced on stderr as it happens.
Ctrl-C exits 130 and leaves the job running on the box — `wait` only ever watches, and no
verb here stops a job except `cancel` and `abort`.

`wait` polls `status --json` every 10 seconds (`--interval`) rather than holding one SSH
connection open for the hours a run takes, so a network blip costs one poll rather than the
whole wait. Polling faster than the runner's own 2-second pass only produces identical
answers.

## Diagnose a failure

Work down this list and stop at the first step that answers.

**1. `sparks status <job>`** — where it got to and why it stopped.

`--json` gives three keys:
- `job` — what was submitted: command, image, git sha, whether the tree was dirty.
- `state` — where it is now: state, run_id, exit_code, and `detail` when the runner gave up
  on it.
- `summary` — the run's permanent record, `null` until the run ends: status, exit code,
  signal, duration, energy, final loss.

`state.state` and `summary.status` are **two different vocabularies and both are right**.
The queue says what happened to the *job* (`failed`); the record says what happened to the
*process* (`crashed`, `finished`, `cancelled`, `killed`, `oom`). A job reading
`state: failed, status: crashed` is one statement, not a contradiction. Do not treat the
absence of `summary` as a failure either — it is `null` for anything that has not ended.

**2. `sparks logs <job>`** — the last 200 lines the job printed. `--all` for the whole file,
`--tail N` for a different amount.

**3. If `logs` refuses with "has no run directory yet"**, the job never reached a container,
so there is no output to read. `status`'s `detail` names the reason. A failed pull says
`pull failed: …`, and `pull.log` in the job directory has the registry's own words; the
usual cause is a tag that was never pushed.

**4. If `logs` prints a `sparks could not run this job:` heading**, everything under it is
sparks' own `error.txt`, not your program's output — the container started but the command
did not. `exec: python: not found` means the command's absolute path is wrong for that
image.

### What each outcome means

- **`failed`, a real exit code, real output** — your program. Read the log.
- **exit 137** — SIGKILL, in practice the OOM killer.
- **`detail: the runner stopped while this job was running`** — the queue container restarted
  underneath it. Nothing is running it now; `sparks retry`.
- **`detail: job data/ directory is missing`** — submit did not finish uploading. Submit
  again.
- **`detail: job has no image`** — the job was committed without a tag. Rebuild and submit
  from a laptop.
- **`aborted`** — someone ran `sparks abort`; `detail` names the uid.
- **`cancelled`** — dropped before it started; `detail` names the uid.
- **Exit 78 from any verb** — the box has no `/etc/sparks/box.toml`, or it promises a path
  that does not exist. sparks refuses to guess those paths. Converge sparkup rather than
  working around it.

## Stop, resubmit, clean up

```sh
sparks cancel <job>   # drop it, only before it starts
sparks abort <job>    # stop it, started or not
sparks retry <job>    # a new job with the same image and data: no rebuild, no upload
sparks remove <job>   # delete a finished job and the data it kept
```

`retry` refuses a job that has not finished — running the same thing twice at once is never
what was meant. `remove` refuses one that has not finished; abort it first.

Only the account that submitted a job may cancel, abort, retry or remove it; root may
control any. Reading is not controlling: `queue`, `logs` and `status` are open to everyone
on the box.

`retry` is the cheap path. It hardlinks the original job's data rather than copying it and
reuses the tag already in the registry, so it costs nothing but a queue slot. Prefer it over
resubmitting whenever the code has not changed.

## The curves

The supervisor hands you the link: where the box's contract names a Grafana, the second line
of `launch.log` in the job directory is a deep link into `training-runs`, already scoped to
this run and to the minute before it started. It prints nothing there rather than guessing a
hostname, so on a box without one, `sparks status <job> --json` gives the id under
`state.run_id` and the dashboard's `$run_id` dropdown takes it. That dropdown is scoped to
the dashboard's time range, so with the default `now-3h` it lists recent runs only — widening
the range is what surfaces older ones.

Three boards, and each answers a different question:

- **`training-runs`** — one run: loss, held-out loss, gradient norm, learning rate and
  throughput, above the GPU power, utilisation and memory from the same minutes.
- **`sparks-queue`** — the queue right now: depth by state, longest wait, last runner pass,
  and the jobs table.
- **`sparks-overview`** — thirty days of runs and failures, total energy, and cost from a
  `tariff` textbox you set to your own price per kWh.

**A run that ends normally stops dead rather than flat-lining.** The emitter marks every
series it wrote stale on the way out, so `training_loss` vanishes from a bare query about
30 seconds after the run ends and only `last_over_time(...)` still finds it. That delay is
deliberate and it is two scrape intervals: a marker any sooner ends a sample that no query
step can reach, which is how runs used to lose their final epoch to a store that had it all
along. Two deliberate exceptions:
`training_run_info` is never staled, which is what keeps a finished run selectable in the
dropdown, and `training_run_active` always is, so anything built on it draws the run's real
span.

What does flat-line is a run that was **killed**. A signal skips the final flush, so an
aborted or OOM-killed job's last values sit on the graph for the five-minute lookback while
`training_run_status` already says how it ended. Read the status, not the tail of the curve.

A run that shows on the dashboard with no end and no status is one whose record was never
written. Check the job directory is writable by the submitting account; a full disk or a
wrong owner is the usual cause.

## When the box itself is the problem

`Last pass` on the queue dashboard is `sparks_queue_runner_heartbeat_timestamp_seconds`,
written on every pass the runner takes, including the ones that find nothing to do. If it is
minutes old, the container is up and `docker ps` is green and yet nothing will ever start —
that is the one failure the queue cannot report about itself, and the reason the heartbeat
exists at all.

Prometheus evaluates `monitoring/alerts/sparks.yml` on the box, but nothing routes it: there
is no Alertmanager, so "firing" means a series says so and somebody has to look. They are in
Prometheus's `/rules`, and queryable as `ALERTS{alertname="..."}`.

- `SparksQueueRunnerStuck` — the heartbeat above stopped advancing.
- `SparksRunIndexEmpty` — `sparks_run_info` is absent: the run index is empty or unreadable,
  which every other health signal reports as green. Also fires on a box that has simply never
  completed a run.
- `SparksTextfileError` and `SparksDuplicateSeries` — node_exporter could not read, or
  silently dropped, one of the `.prom` files. Neither shows up as a failed scrape.
- `SparksQueueBacklog` — more than five jobs queued for six hours. Not an error; work piling
  up behind something that is never going to finish looks exactly like this.

## Where the files actually are

`summary.json`, `output.log` and `error.txt` live in the box's shared directory under
`runs/<run_id>/`, and they are the source of truth — Prometheus is the live view. `logs` and
`status` read them for you, and are the supported way in; reach for `ssh` and a path only
when you need something they do not expose. In the job directory that is `pull.log`, and
`launch.log`, which is the supervisor's own stdout: the run id, the Grafana deep link, and
the status it recorded.

`sparks_runs.prom` in node_exporter's textfile directory is rebuilt from the summaries after
every run, so the run index survives losing the TSDB. The live queue is `sparks_queue.prom`
beside it.

To write or change the training code itself, use the `authoring-a-sparks-job` skill.
