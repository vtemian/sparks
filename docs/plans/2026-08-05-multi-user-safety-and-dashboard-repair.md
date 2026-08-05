# Repairing slice 2: multi-user safety, the dashboards, and honest energy

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** make `sparks` safe for two people to share a box, make both dashboards show the truth, and
stop recording energy numbers that are confidently wrong.

**Architecture:** Three tiers, in order. Tier 1 is what makes it unsafe for a colleague to use at
all. Tier 2 is what makes it useless to look at. Tier 3 is what makes the numbers mean what they
say. Ship tier 1 alone if you ship nothing else.

**Tech Stack:** Python 3.12, `flock`, Grafana 13.1.1, Prometheus 3.13.2, node_exporter 1.12.1.

---

## Where this came from

Five parallel reviews of `slice-2-launcher`, then three design agents. Everything below was
**proven with a runnable script or observed in a real Grafana**, not argued. The proof scripts live
in the session scratchpad under `rev-robust/`, `conc/` and `graftest/`.

The reassuring half first, so nobody re-does it: 34 of 50 mutations were caught by the test that
names the behaviour. `process.py`, `buffer.py`, the emitter split, `index._escape` and `render`'s
format rules are all genuinely well tested. `_escape` was run through real `promtool` and a real
node_exporter with 16 hostile payloads including a deliberate label-forgery attempt; all 16 passed
clean. The holes are elsewhere.

## The correction that matters most, because I wrote the wrong thing into the docs

`INSTALL_CLAUDE.md` claims `sort: 8` is Natural DESC, "verified from `VariableSort` in
`types.gen.ts` at v13.1.1". **That is wrong, and it was wrong because it was read rather than run.**

Observed in a real Grafana 13.1.1 with six runs and two legacy ids, testing every value 0 to 8:

```
sort 0,1     raw datasource order              auto-selects run-10-legacy
sort 2       alphabetical desc                 auto-selects run-alpha-x
sort 3       numerical asc                     auto-selects run-Zulu-x
sort 4       numerical desc                    auto-selects the NEWEST run
sort 5,6,7,8 byte-identical to sort 0          auto-selects run-10-legacy
```

`sort: 8` is **silently ignored**. Not natural sort, not any sort. The `@grafana/scenes`
`sortVariableValues` switch does have arms for 5 to 8, which is what the earlier agent read, but they
never fire at runtime. Use **`sort: 4`**, which keys on the leading number and so also handles the
`run-9` versus `run-10` case the old description worried about.

The compound effect is worse than the bug: opening `training-runs` at its default `now-3h` with
`sort: 8` auto-selected the **oldest** run in the window, one that had already scrolled out of it,
and the board rendered "No data" in all ten training panels. Fixing this one value changes the
landing experience entirely.

**Lesson for the whole repo: reading the source beat guessing, but running it beat reading the
source.** sparkup's own docs say trust the machine over the document. Apply it here.

---

# TIER 1 — multi-user safety

Every defect in this tier is "a colleague does it by accident", and every one was reproduced on real
Linux with two real accounts in group `bbm` on a real setgid 2775 tree.

## Task 1: a `run_id` that cannot collide

**Files:** Modify `src/sparks/run.py`, `src/sparks/launcher.py`, `src/sparks/demo.py`;
Test: `tests/test_run.py`

**The defect, proven.** Two users starting in the same minute get the identical id. One run
directory, one `summary.json` holding one person's record, the other's gone; `output.log` opened
`"wb"` truncated the first writer's log leaving a 27-byte NUL hole in the middle. Both printed the
same Grafana link.

**The fix is two things, and the second is the one that actually guarantees anything.**

Seconds plus the username makes a collision require one person launching two identically-named runs
in the same second. `mkdir` returning `FileExistsError` closes even that, and it is the only *hard*
guarantee available: it is atomic, and the launcher has to create the directory anyway. The real fix
is **removing the `exist_ok=True` that silently permitted the collision**.

```python
def new_run_id(
    name: str, user: str, when: float | None = None, attempt: int = 0
) -> str:
    """`run-YYYYmmdd-HHMMSS-<user>-<name>`, chronological as a string.

    The username is what makes a cross-user collision structurally impossible
    rather than merely unlikely, and usernames are unique on a box. The
    attempt suffix is the tie-break for one person launching the same name
    twice in one second; `reserve_run_dir` is what decides it, atomically.
    """
    moment = time.time() if when is None else when
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    tie = f"-{attempt + 1}" if attempt else ""
    return f"run-{stamp}-{slug(user, 'unknown')}-{slug(name, 'run')}{tie}"


def slug(name: str, fallback: str = "") -> str:
    return (
        re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")
        or fallback
    )
```

Two bugs fixed in passing, both found by writing the tests: `when or time.time()` treated
`when=0.0` as absent, and a `--name` of `""`, `"///"` or `"🎉"` slugged to empty and produced an id
ending in a bare `-`.

`demo.py` is a second call site. mypy catches it; grep alone would not.

**Tests.** Only `test_the_shape_is_stable` needs its regex changed. Add: two users at the same
instant get different ids; the same user and name twice in one second gets `-2`; ids still sort
chronologically; an empty and an emoji-only name both fall back.

## Task 2: directories that survive any umask

**Files:** Create `src/sparks/shared.py`; modify `src/sparks/launcher.py`, `src/sparks/process.py`

**The defect, measured on a real two-account box.** Setgid inheritance is *not* the problem: Linux
propagates the group from the 2775 parent at every umask. Only the permission bits are wrong.

```
umask 077   runs/ and run dir 2700   the other user cannot listdir runs/, so their
                                     load_all sees ZERO summaries and rebuild writes
                                     an EMPTY index, wiping the shared history
umask 022   runs/ and run dir 2755   the other user cannot create a run dir at all
umask 002   runs/ and run dir 2775   works, by luck
```

**`os.mkdir(mode=...)` is not a substitute for `chmod`**, because the mode argument is masked:

```
os.makedirs(mode=0o2775) at umask 000  ->  2775
os.makedirs(mode=0o2775) at umask 077  ->  2700   masked
mkdir + os.chmod(0o2775) at umask 077  ->  2775   chmod is never masked
```

Chmod the **directories and the files**. `summary.json` is already safe via `write_atomically`, but
`output.log` is opened `"wb"` and lands 0600 at umask 077, so the other person cannot read the log of
the run hogging their GPU.

**`os.umask()` is rejected**: it is process-global, returns the old value rather than scoping, is
racy against the launcher's own threads, and the training child inherits whatever it is left at.

**2775, not 2770**, verified: at 2770 the `nobody` account node_exporter drops to cannot read a
summary; at 2775 it can.

```python
DIR_MODE = 0o2775
FILE_MODE = 0o664


def make_dir(path: Path) -> Path:
    """Create a directory both group members can always use.

    The chmod is separate from the mkdir because mkdir's mode is masked by the
    caller's umask and chmod is not. It is attempted even when the directory
    already existed, so a tree left at 2700 heals the next time its owner runs.
    """
    if not path.parent.is_dir():
        make_dir(path.parent)
    with contextlib.suppress(FileExistsError):
        path.mkdir()
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)
    return path


def reserve_run_dir(runs_dir: Path, name: str, user: str) -> tuple[str, Path]:
    """Claim a run id by creating its directory. EEXIST is the collision check
    and it is atomic, which is the only hard uniqueness guarantee available."""
    make_dir(runs_dir)
    for attempt in range(1000):
        run_id = new_run_id(name, user, attempt=attempt)
        run_dir = runs_dir / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        with contextlib.suppress(OSError):
            os.chmod(run_dir, DIR_MODE)
        return run_id, run_dir
    raise RuntimeError(f"no free run id under {runs_dir} for {name!r}")
```

**`suppress(OSError)` is load-bearing and measured: chmod on a directory another user owns is
EPERM.** Without it, the second user's run dies on a directory the first user created.

**Migration is manual and mandatory.** Measured: the second user cannot chmod the first's 2700
`runs/`, and cannot create anything inside it. If the box already has such a tree, its owner or root
must run `chmod -R 2775 /srv/bbm/runs` once. New code cannot self-heal this for the other user.
Say so in `INSTALL_CLAUDE.md`.

**Verify** at the worst mix, with three runs at umask 077, 022 and 027: every directory 2775, every
log 0664, and both users' `load_all` sees all three.

## Task 3: a lock around the whole read-modify-write

**Files:** Modify `src/sparks/index.py`; add to `src/sparks/shared.py`

**The defect, reproduced with the real `rebuild`, two runs finishing ~4 ms apart, 40 trials:**

```
                original      with the lock
  50 runs        0/40 lossy    0/40
 500 runs        3/40 lossy    0/40
2000 runs       10/40 lossy    0/40
```

`write_atomically` makes the *write* atomic, not the read-modify-write around it. The window is
`load_all`, which grows from 0.9 ms at 10 runs to 363 ms at 5000, so this worsens all year.

**Lock the index directory's file descriptor. Do not use a lock file.** The obvious lockfile version
was written first and **failed in the end-to-end test, from this very bug class**:
`os.open(path, O_CREAT, 0o664)` is masked by umask too, so the first user creates it 0600 and the
second gets `PermissionError` forever. Locking the directory sidesteps it, because `make_dir`
already guarantees 2775.

Measured facts behind the choice: `flock` takes `LOCK_EX` on an `O_RDONLY` fd while `fcntl.lockf`
raises **EBADF** on one, and `O_RDONLY` is all a directory can be opened for. The kernel releases it
on process death: a contender acquired **0.0001 s** after the holder was SIGKILLed, so there is no
stale lock, no pid file and no reaper. It is advisory, so node_exporter is unaffected.

```python
@contextmanager
def exclusive(directory: Path, timeout: float = 30.0) -> Iterator[None]:
    """Serialise the index's read-modify-write across both users.

    Locks the directory itself rather than a lock file: a lock file created
    under umask 077 is 0600 and the other user can never open it, which is the
    same class of bug this whole tier exists to fix. flock is the only option
    on an O_RDONLY fd, which is all a directory can be opened for, and the
    kernel releases it on process death.
    """
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    msg = f"{directory} was locked for over {timeout:g}s"
                    raise TimeoutError(msg) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the lock
```

`rebuild` wraps **`load_all` and the write together**. Locking only the write is what the code
effectively does now and is why it loses rows. A wedged holder raises `TimeoutError`, `_rebuild`'s
existing broad except logs it, and the run still succeeds: this run's `summary.json` is already on
disk and the next rebuild picks it up.

Alternatives, rejected with evidence: **append-only** is fatal on format, because a second `# HELP`
for a family is `error while linting: second HELP line for metric name`, exit 1 from real promtool.
**Fragment per run** just moves the race into the combiner. **Accepting the loss** is the zero-code
option, rejected because it worsens with history, the row that goes missing is the newest one you
were about to go look at, and it only self-heals when someone runs again, so the last run of the day
stays missing overnight.

**Unverified: flock over NFS.** If `SPARKS_SHARED_DIR` ever becomes an NFS mount, re-measure.

## Task 4: text that cannot poison the shared index

**Files:** Modify `src/sparks/shared.py`, `src/sparks/launcher.py`, `src/sparks/summary.py`

**The defect, proven end to end.** `series.py` validates label *names* and never *values*. Python
decodes argv with surrogateescape, so `--name $'bad\xffname'` becomes a lone surrogate.
`json.dumps` is `ensure_ascii=True`, so it escapes it and **the write succeeds** — the file is
poisoned silently. Every later rebuild re-reads it and `f.write()` raises `UnicodeEncodeError`,
swallowed as a warning.

Proven: one poisoned run, then four healthy runs by the other user. Eight run directories on disk,
index frozen at three rows, mtime unchanged. `node_textfile_scrape_error` stays 0 and
`absent(sparks_run_info)` is false, so **neither alert fires**. The colleague's completed runs stop
appearing forever, with no signal anywhere.

**Sanitise on the way in, at one boundary.** Reject-at-CLI covers only `--name`; `user` comes from
`getpass` and the error string from an exception, and neither passes through argparse.
Escape-on-the-way-out means four places and one miss reinstates the bug.

```python
def clean(value: str, fallback: str = "unknown", limit: int = MAX_TEXT) -> str:
    """Make text safe to persist and to carry as a Prometheus label value.

    surrogateescape on the way out recovers the original bytes; replace on the
    way back turns exactly the undecodable ones into U+FFFD and leaves genuine
    UTF-8 alone. The limit is a separate concern: a label rides every row of
    the index forever.
    """
    text = value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return text[:limit] or fallback
```

Three call sites:

- **`launcher.launch()`**, three lines at the top. The child is still exec'd with the **original**
  argv, which round-trips through `execve` correctly; only the durable copy is cleaned.
- **`Summary.from_dict`** — cleans `run_name` and `user` on load. This is the line that **unfreezes a
  box that is already poisoned**, without anyone hand-editing files.
- **`_record_failed_launch`** — see below.

**Rejected: validating values in `Series`.** `RunMetrics.__init__` builds Series eagerly so a bad
label fails on the caller's thread, which means a raise there would **fail a training run over a
cosmetic label**. That contradicts this repo's consistent rule that telemetry never kills a run.

**An extra defect the reviews missed: the wrapper crashes outright.** In `_record_failed_launch`,
`(run_dir / "error.txt").write_text(error + "\n")` raises `UnicodeEncodeError` when the message
carries the bad byte, and it sits *inside* `launch()`'s `except`, so it escapes with a traceback.
Trigger: `sparks run -- ./nonexistent-$'\xff'`, because `FileNotFoundError`'s message carries the
surrogate straight from argv. Fix with `clean(str(e), "", limit=4000)`.

**Verify:** a non-UTF-8 `--name` produces `run_name="e0�"`, renders as `run_name="e0�"`, and the
file passes `promtool check metrics` exit 0 alongside rows containing quotes, backslashes and
newlines.

## Task 5: the record-writing path must not be able to lose the record

**Files:** Modify `src/sparks/launcher.py`

**The defect, proven.** `summary.save` and `metrics.end` sit *after* the try/except added in slice 2.
The child ran to completion, exit 0, then a `PermissionError` at `launcher.py:128`. No
`summary.json`, no index row, `metrics.end()` never called, and the wrapper exited 1 instead of the
child's 0. Because `training_run_info` is LIFECYCLE-exempt from stale marking, that run sits on the
dashboard **forever** with no end and no status.

That is exactly the phantom the existing guard's comment says it prevents. The guard covers the
launch and not the record.

Restructure `launch()` so **everything after the child is reaped runs under one `try/finally`**, and
so `metrics.end(status)` is called on every path including an exception while saving. Triggers are
mundane: a full disk, a quota, or the other user owning the directory.

## Task 6: the exit code, which nothing tests

**Files:** Test `tests/test_cli.py`

Mutation `C1` makes `_run` return `0` instead of `result.wrapper_exit`, and **the entire suite stays
green, unit and live**. Every caller that checks `$?` — a queue, a CI job, a shell `&&` — then sees
success for a crashed run. Mutation `C2` deletes the `run` dispatch entirely and is also green.

`tests/test_cli.py` stops at `build_parser` and `deep_link`; nothing ever calls `main()`. One test
kills both:

```python
def test_a_crashed_child_makes_the_cli_exit_nonzero(tmp_path: Path) -> None:
    # A queue or a shell && reads $?. Returning 0 for a crashed run is the
    # wrapper lying to its caller, and the whole suite passes with it.
    code = cli.main([
        "run", "--shared-dir", str(tmp_path), "--baseline-seconds", "0",
        "--", "sh", "-c", "exit 3",
    ])
    assert code == 3
```

---

# TIER 2 — the dashboards

Every item observed in a real Grafana 13.1.1 with real data, at browser TZ `Europe/Bucharest`.

## Task 7: the variable that picks the wrong run

**Files:** Modify `dashboards/training-runs.json`, `INSTALL_CLAUDE.md`

`"sort": 8` becomes `"sort": 4`. Rewrite the description and the `INSTALL_CLAUDE.md` claim; see the
top of this plan. State that 5 to 8 are ignored in 13.1.1, so nobody "restores" it.

## Task 8: the link that opens the wrong window

**Files:** Modify `dashboards/sparks-overview.json`

Observed `href` values, browser at +03:00, for `Started` = 1783276337000:

```
${__data.fields["Started"]}          -> 2026-07-05 21:32:17   the FORMATTED value
${__data.fields["Started"].text}     -> 2026-07-05 21:32:17   same
${__data.fields["Started"].raw}      -> ""                    .raw is not supported
${__data.fields["Started"].numeric}  -> 1783276337000         correct
```

Following the formatted one, the time picker read **`2026-07-06 00:32:17`**: parsed as UTC,
re-rendered at +03:00, opening three hours *after* the run started. Every panel empty.

Grafana data links cannot do arithmetic, so `to` needs its own field. Add a hidden query:

```json
{
  "refId": "H",
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "expr": "max by (run_id) ((sparks_run_start_timestamp_seconds + sparks_run_duration_seconds)) * 1000",
  "format": "table",
  "instant": true,
  "legendFormat": "__auto"
}
```

Rename `Value #H` to `Ended` and hide it with `custom.hidden` rather than
`organize.excludeByName` — verified, `custom.hidden` keeps the field in the frame so the link can
still resolve it, while excluding removes it. Then:

```json
"url": "/d/training-runs/training-runs?var-run_id=${__data.fields[\"run_id\"]}&from=${__data.fields[\"Started\"].numeric}&to=${__data.fields[\"Ended\"].numeric}"
```

Verified end to end: clicking the 30-day-old run produced
`?var-run_id=run-20260705-0900-charlie&from=1783276337000&to=1783276937000`, the picker read the
run's exact 10-minute window in local time, and every panel populated.

## Task 9: stat panels that read "No data" for anything not live

**Files:** Modify `dashboards/training-runs.json`

Correction to the review's premise: it is not *every* stat panel. Alive and Duration already worked,
because they already had `last_over_time`. Step, Epoch and Latest loss did not.

Every candidate tested against three runs (live, 20 h old, 30 d old) at three windows
(`now-3h`, `now-30d`, `now-90d`), 12 cells each:

- **Min interval `1m`** — no effect; it raises the step floor, it cannot lower a 67-minute step.
- **Max data points 5000** — produces a number, and it is the **wrong** one: 240 instead of 320,
  because the step lands mid-run. Actively dangerous.
- **`instant: true` on the bare metric** — "No data"; the 5-minute lookback cannot reach an old run.
- **Range query plus `last_over_time(expr[$__range])`** — works for old runs, **fails for the newest
  run** at wide windows, because Grafana aligns range-query steps to the boundary and the last point
  lands hours before a run that started 80 minutes ago.
- **`instant: true` plus `range: false` plus `last_over_time(expr[90d])`** — **12 of 12.** The only
  candidate that works everywhere.

`"range": false` must be set explicitly alongside `"instant": true`.

**`[30d]` is a live time bomb.** The 30-day-old run silently vanished the moment it aged past 30.00
days. **The window must exceed your Prometheus retention, not equal your dashboard's default range.**
Retention is 1y, so use `[90d]` only if you accept losing older runs from these tiles; otherwise
match retention.

## Task 10: annotations that shade the wrong runs and title themselves wrong

**Files:** Modify `dashboards/training-runs.json`

Three observations. Filtering works, it was simply never asked for: with two runs and one selected,
the unfiltered query shaded both. A label the series does not carry **cannot** be shown at all, which
is why `titleFormat: "{{run_name}}"` rendered the literal string `run_name` — `training_run_active`
carries only `run_id`. And **the emitter does not need to change**: a PromQL join produces a
pixel-identical tooltip to putting `run_name` on the series.

```json
"target": {
  "expr": "training_run_active{run_id=~\"$run_id\"} * on(run_id) group_left(run_name) max by (run_id, run_name) (training_run_info)",
  "refId": "Anno"
}
```

`max by (...)` on the right for the same reason panel 10 uses it: a duplicate info series would
otherwise be a hard query error.

## Task 11: the rest of what a user sees

**Files:** Modify `dashboards/sparks-overview.json`, `dashboards/training-runs.json`, `Makefile`

- **`sparks-overview.json` is never deployed.** `make deploy` scp's only `training-runs.json`. Ship
  the whole `dashboards/` directory. The README also says "the dashboard", singular.
- **Two stat panels share `gridPos {x:6,y:0}`**, so Grafana pushes one to a second row and the top
  row renders with a hole. Use `x` = 0 / 6 / 12 / 18.
- **"Failed" counts the run that is currently running.** With 3 finished, 2 failed and 1 running the
  tile reads 3, while its own description says "crashed, cancelled, killed or OOM". Use
  `count(sparks_run_info{status!~"finished|running"}) or vector(0)`.
- **`or vector(0)` only on "Failed"** turns a data outage into a reassuring zero while the other
  three tiles show "No data". Either all four get it or none do.
- **The Loss legend renders `bravo  s`** with a dangling "s": `legendFormat` uses `{{arm}}` and
  `{{seed}}` but the join only pulls `group_left(run_name, git_sha)`. Add `arm` and `seed` to both
  the `group_left` and the `max by`.
- **"Step" is unreadable**: `unit: "short"` renders 1912 as "2 K". Use `"none"`.
- **The `run_id` column is truncated** to `run-20260804-1350-` and it is the clickable link, so you
  cannot read which run you are opening. Fix by giving the numeric columns explicit
  `custom.width` and letting `run_id` auto-fill.
- **The tariff variable cannot be changed by anyone.** A `constant` renders as a bare chip with no
  input and ignores `?var-tariff=`, while its description claims "a variable means no dashboard
  rebuild". Use a `textbox`, or correct the description.
- **`training-runs.json`'s hardware panels carry no `{job=...}` filter** where every sparkup
  equivalent does. Safe today; in a harness where both jobs serve everything, panel 22 failed
  outright with `found duplicate series for the match group`. Add the filter.

---

# TIER 3 — honest numbers, and tests that would notice

## Task 12: a baseline that admits when it is somebody else's job

**Files:** Modify `src/sparks/energy.py`, `src/sparks/launcher.py`

**The defect.** `max(0.0, total - idle*seconds)` collapses two different facts: "this run drew
nothing above idle" and "the baseline belongs to the colleague's job". With their job running the
baseline is ~126 W against a real idle of 13 W, and marginal clamps to **0.0 for essentially any
run**. Both headline tiles and two table columns are built on it. A user comparing configs concludes
the 3am run's config is dramatically better.

**The signal to use is the GPU rail during the baseline window.** It needs no new sensor and no new
dependency, the `gpu` energy counter already exists as `energy4`, and the separation is large and
measured: **3.82 W idle against 73.7 W** under a saturating run. Sample-variance was considered and
rejected as a gate, because a *steady* neighbour has low variance and that is the motivating case.

**Read the baseline from the same counter as the run.** Today the baseline averages the `sys_total`
*gauge* while `total_joules` reads the `pkg` *accumulator*, so the subtraction crosses measurement
boundaries and imports every gauge problem including the 7% short-window integral error. A counter
delta across the baseline window *is* the average power, exactly. Keep the gauge loop as the fallback
for a box with power but no accumulator.

```python
BUSY_GPU_WATTS = 10.0
"""Above this the GPU rail was already working before the run started, so the
baseline is somebody else's job. The rail idles at 3.82 W here and draws 73.7 W
saturating; 10 W is 2.6x idle. Calibrated to THIS box, which is why every
summary.json now records idle_gpu_watts: the next revision of this number comes
from records rather than a second guess."""
```

`marginal_joules` returns `float | None`, **None and never 0.0**, whenever the baseline cannot carry
the subtraction — no baseline, a contended one, or a run that came in more than ~10% under its own
baseline (which means the neighbour stopped mid-run, so the baseline describes a box that no longer
exists).

**"Unknown" in Prometheus is an omitted sample, not NaN.** `render()` already drops `None`, which is
how a run with no final loss avoids recording a loss of zero. Publishing NaN was rejected: `sum()`
over NaN is NaN, so one contaminated run turns the headline tile into a broken-looking panel instead
of an honest one. Then `count(sparks_run_info) - count(sparks_run_marginal_energy_joules)` is the
number of excluded runs. Publish `sparks_run_idle_watts` unconditionally so a blank column has a
visible cause, and change the headline tile to `sum(sparks_run_energy_joules)`, which is always
present.

**Defer to slice 3: any enforcement.** No refuse-to-launch, no wait-until-quiet. The queue makes
exclusivity structural and anything built now to approximate it gets deleted. The *detection* keeps
earning its place afterwards: once the queue enforces exclusivity, `contended` firing means the queue
was bypassed.

## Task 13: `sources_agree`, which is wrong in both directions

**Files:** Modify `src/sparks/energy.py`, `src/sparks/launcher.py`; Test `tests/test_energy.py`

Returns **False when everything is fine**: a driver without
`nvmlDeviceGetTotalEnergyConsumption` gives 0.0, so the first branch fires on every run forever and
the log says "one counter probably reset" untruthfully. Returns **True when nothing was measured**: a
sensorless box via `0 <= 0`, and `_record_failed_launch` hard-codes it.

One correction to the earlier diagnosis: **integer-millijoule quantisation is not what breaks short
runs.** A 1.5 s run at the idle rail is ~5700 mJ, a 0.02% error. What breaks is **counter update
granularity** — a short window may catch one tick from one source and two from the other. Same fix,
but the minimum window is set by the slowest counter's tick, and **that number is unverified**:
measure it once by reading `energy4_input` at 10 Hz for 20 s and timing the steps.

Three states, not a boolean, and a **relative** tolerance. The ratio moves 2.1% across every regime
ever observed (1.198, 1.225, 1.223, 1.195), so `±0.5` absolute is 41% and catches a firmware reset
only after it has eaten 41% of the run. `±15%` is still 7x the observed spread.

## Task 14: a record that reproduces its own arithmetic

**Files:** Modify `src/sparks/summary.py`, `src/sparks/index.py`, `src/sparks/launcher.py`

`EnergyReading.seconds` is the counter window and it is thrown away, so
`total - idle*duration` misses the recorded `marginal` by 75% on a short run, and the index publishes
`sparks_run_energy_joules` and `sparks_run_duration_seconds` over **different windows** — so
`energy/duration`, the first derived number anyone computes, overstates by 6x on a short run.

Persist `window_seconds`, `baseline_seconds`, `idle_gpu_watts` and `gpu_sources`. Do **not** narrow
the counters to the child's duration; billing 5 s of cleanup at 13 W to a 1.5 s run is worse. Add
`sparks_run_energy_window_seconds` and `sparks_run_idle_watts` to `index.NUMERIC`, and
`energy_sources` as a label on `sparks_run_info` beside `status`.

Fix a real bug in the same edit: `energy_read_at` uses `time.time()`, which NTP can step mid-run,
the exact hazard `duration_seconds` avoids with `time.monotonic()`. Take the window on the monotonic
clock and the `max()` guard becomes dead code.

**Decision needed from Vlad.** `Summary.from_dict` calls `Energy(**data["energy"])`, so every
existing `summary.json` raises `TypeError` on load and `load_all` skips it with a log line. Per
CLAUDE.md, backward compatibility needs explicit approval, so the default is to break it. Check what
is actually on the box first (`ls /srv/bbm/runs`); if it is only the t1/t2/t4 acceptance runs, delete
them rather than write a shim.

## Task 15: the seam that makes energy testable

**Files:** Modify `src/sparks/launcher.py`; Test `tests/test_launcher.py`

`launch()` hardcodes `Sampler.detect()`, so on a development machine **every energy assertion is
`0.0 == 0.0`** and three mutations survive the whole suite: swapping the two GPU sources in the
record, recording the raw counter instead of the delta, and never sampling the baseline.

`Sampler` is already injectable. Add one parameter, `sampler: Sampler | None = None`. **No mock is
needed**: the fake sysfs tree is real files, and the *child process* advances the counters, which is
exactly what happens on the box. Write the child as a `sh -c` that echoes new values into the fake
counter files, with three different deltas so any value in the wrong field is visible.

## Task 16: robustness, and the tests that would notice

**Files:** Modify `src/sparks/energy.py`, `src/sparks/summary.py`; create `tests/conftest.py`;
modify `Makefile`, `tests/test_buffer.py`, `tests/test_run.py`, `tests/test_live.py`

- **`_read_micro` returns `None`, not 0.0**, and rejects the implausible. `float("nan")` and
  `float("inf")` do not raise, which is how a bare `NaN` token reaches `json.dumps` and produces a
  file every strict parser rejects. Use `math.isfinite`, a `MAX_PLAUSIBLE_WATTS` ceiling (a dead
  sensor reports the u32 sentinel, 4294967295 µW = 4295 W), and **`median` not `fmean`** — verified,
  59 samples of 13.06 W plus one sentinel gives mean 84.43 W and median 13.06 W.
- **`json.dumps(allow_nan=False)` is rejected**: it raises, `launch` does not wrap `summary.save`,
  and the result is a run that completed and then crashed the wrapper writing its own record. Losing
  one number beats losing the file. Sanitise at the boundary instead.
- **The unit suite writes to the host's real textfile directory.** Demonstrated: `test_launcher.py`
  overwrote the target's `sparks_runs.prom` with rows from pytest's `tmp_path`. On the box,
  `make check` therefore **destroys the production run index**. Add a `tests/conftest.py` with an
  autouse fixture pointing `SPARKS_TEXTFILE_DIR` at `tmp_path`.
- **`make live` never tears down on failure.** `harness-up`, `pytest`, `harness-down` are separate
  recipe lines, so a failing test aborts the target and leaves the container up with a dirty TSDB,
  which then makes the next run fail on stale cumulative counters that look like a fresh failure.
  Capture the exit code and tear down unconditionally.
- **`make check` passes with the promtool test skipped**, which is the state on any machine without
  promtool. That silently removes the only external validation of the `.prom` format. Either fail on
  the skip or run promtool from the pinned Docker image the way sparkup does.
- **`Buffer.drain` is never tested with out-of-order input** (mutation `B3` survives unit and live).
  `time.time()` is not monotonic and `log()` from two threads interleaves, so the sort is
  load-bearing. Three lines: add 2000 then 1000 in one window, assert `[1000, 2000]`.
- **`_beat` is never tested** (mutation `E7` survives unit and live). Every unit test uses
  `autostart=False`, and every live test finishes inside the 5-minute lookback so a frozen heartbeat
  is indistinguishable from a live one. Add a live test that waits past two flush cycles and asserts
  the heartbeat advanced.
- **`git_sha` is never checked to return a sha** — `assert sha == "unknown" or re.match(...)` passes
  against a permanently broken implementation. Assert equality with `git rev-parse --short HEAD`
  inside a checkout, and cover the tarball case with a `tmp_path` cwd.
- **The live suite never runs a child that emits.** The whole disjoint-series apparatus exists for
  two real writers on one `run_id`, and no test creates that against a real Prometheus. Add one
  launching a child that calls `emit.from_env(arm="real")` and logs a loss, asserting both the
  child's `training_loss` and the supervisor's four lifecycle series landed.
- **`prometheus_tsdb_out_of_order_samples_total` returns two series** on 3.13.2 (`type="float"` and
  `type="histogram"`), and `test_the_receiver_dropped_nothing_silently` reads `[0]`, so which one it
  checks is a coin flip. Use `sum(...)`.

## Task 17: the alerts nothing loads

**Files:** Modify `alerts/sparks.yml`, `INSTALL_CLAUDE.md`, and sparkup's `prometheus.yml.j2`

**Nothing loads this file.** There is no `rule_files:` in sparkup's Prometheus config, no
`alerting:` block and no Alertmanager, and `make deploy` does not ship it. Meanwhile
`INSTALL_CLAUDE.md` and a comment in `launcher.py` both say `SparksRunIndexEmpty` "fires forever" as
if it were live. `promtool check rules` passes, so the file is valid; it is simply inert. Either wire
it up or say plainly that it is a specification.

Three semantic fixes regardless:

- `rate(promhttp_metric_handler_errors_total{cause="gathering"}[15m]) > 0` with `for: 15m` will
  essentially **never fire**: a single error keeps the rate positive for exactly 15 minutes, so the
  pending window expires as the condition does. Use `increase(...[15m]) > 0` with no `for`.
- `absent(sparks_run_info)` fires on a fresh box that has simply never completed a run, and is
  indistinguishable from the box being down.
- `job="node_textfile"` only exists on sparkup's unmerged branch, so against today's `main` that rule
  is permanently silent. Nothing states the coupling.

Also: nothing validates `alerts/*.yml` in any gate. Run each `expr` through
`check_dashboard.metric_names`/`allowed`, plus `promtool check rules`.

---

## Order, and what to ship

Tier 1 is the shippable unit: after it, two people can use the box without destroying each other's
records. Tier 2 is a separate branch and can land the same day. Tier 3 changes the durable record
format, so it wants the `Summary` decision answered first.

**Definition of done for tier 1:** two accounts, three runs at three different umasks, both users
see all three in the index; a same-second same-name collision produces two distinct runs; a non-UTF-8
`--name` neither poisons the index nor crashes the wrapper; a `PermissionError` while saving still
leaves a status on the dashboard; and `sparks run -- sh -c 'exit 3'` exits 3.
