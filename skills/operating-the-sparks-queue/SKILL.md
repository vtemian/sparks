---
name: operating-the-sparks-queue
description: Use when watching, diagnosing, stopping or resubmitting training jobs already on a sparks box - reading sparks queue, logs and status, working out why a run failed, and cancel/abort/retry/remove. Covers the machine-readable --json forms, the states, and what each failure detail actually means.
---

# Operating the sparks queue

Every verb here runs on a laptop and travels over SSH to the box named by `$SPARKS_HOST` or
`--host`. There is no local mode: a command that tells you to set `SPARKS_HOST` never
reached the box at all.

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

Grafana's `training-runs` dashboard takes a `$run_id` variable; `sparks status <job> --json`
gives you that id under `state.run_id`. The dropdown is scoped to the dashboard's time
range, so with the default `now-3h` it lists recent runs only — widening the range is what
surfaces older ones.

A finished run's curves flat-line for about five minutes rather than stopping dead. Pushed
series are not marked stale automatically, so a killed run's non-lifecycle metrics hold
their last value for the lookback window. Expected, not a bug.

A run that shows on the dashboard with no end and no status is one whose record was never
written. Check the job directory is writable by the submitting account; a full disk or a
wrong owner is the usual cause.

## Where the files actually are

`summary.json`, `output.log` and `error.txt` live in the box's shared directory under
`runs/<run_id>/`, and they are the source of truth — Prometheus is the live view. `logs` and
`status` read them for you, and are the supported way in; reach for `ssh` and a path only
when you need something they do not expose, such as `pull.log` or `launch.log` in the job
directory.

`sparks_runs.prom` in node_exporter's textfile directory is rebuilt from the summaries after
every run, so the run index survives losing the TSDB. The live queue is `sparks_queue.prom`
beside it.

To write or change the training code itself, use the `authoring-a-sparks-job` skill.
