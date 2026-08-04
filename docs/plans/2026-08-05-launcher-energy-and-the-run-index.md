# sparks slice 2: the launcher, energy, and the run index

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `sparks run -- <cmd>` wraps a training command, reports faithfully how it ended, measures
what it cost in energy, and leaves a permanent record that outlives Prometheus retention.

**Architecture:** The wrapper is the parent process and owns lifecycle, energy and the run
directory; the child owns its own training metrics and pushes them itself using the `run_id` and
Prometheus URL it inherits through the environment. Durable per-run facts go to the node_exporter
textfile collector, not remote-write, because a `.prom` file is re-scraped forever while pushed
series expire.

**Tech Stack:** Python 3.12, `nvidia-ml-py` (NVML), node_exporter textfile collector, Prometheus
3.13.2, Grafana 13.1.1, cgroup v2.

---

## What this slice does NOT need, and why that is the headline

**No Grafana credentials. No service account. No token file.** That question blocked this slice
three times; the answer is that it was the wrong question.

Grafana's Prometheus datasource builds region annotations natively. Its transformer filters samples
to `value > 0` and merges consecutive samples within one query step into a single region with
`time` and `timeEnd`. Grafana's own documentation for 13.1.1 states the opposite on both counts
("Every data point returned creates an annotation. There is no automatic filtering of zero values",
and that range annotations are "limited"); the shipped implementation in
`@grafana/prometheus/dist/esm/annotations.mjs` does exactly what we need. Trust the implementation.

So the shaded run region is a JSON edit to a dashboard's `annotations.list`, with no auth anywhere.
For the record, the alternative was measured and priced: an Editor service account works, but every
path to a token runs through an authenticated API call, there is no declarative provisioning for
service accounts in Grafana OSS, `GF_SECURITY_ADMIN_PASSWORD` is a **silent no-op on an existing
instance** (it applies only at first-ever start), and the only working primitive is
`grafana cli admin reset-admin-password` against a live container. That is roughly 60 lines of
Ansible plus a new secret, to shade a rectangle.

`POST /api/snapshots` genuinely cannot be done without a credential (anonymous is rejected by
middleware before RBAC ever runs). Snapshots stay out of this slice.

## A measured flaw in the obvious annotation, and its fix

The natural expression is `training_run_info`, which slice 1 already pushes every cycle. **Do not use
it.** Measured on the box against the slice-1 acceptance run:

```
run really started   1785847319
run really ended     1785847367     (48 s)
training_run_info series ends at    1785847660     -> 293 s past the end
training_loss series ends at        1785847355     ->  12 s before the end
```

`training_run_info` is in `metrics.LIFECYCLE`, so slice 1 deliberately exempts it from stale
marking, which is what keeps the panel join resolvable after a run finishes. The cost is that it then
persists for the full `--query.lookback-delta` (5m), and an annotation built on it draws a region
**seven times longer than a short run**. `training_loss` stops dead, because it *is* stale-marked;
the 12 s undershoot is just the 5 s flush cadence.

**The fix is one new metric**, `training_run_active`, value 1, deliberately **not** in `LIFECYCLE`:

- `training_run_info` stays exempt, so joins keep working and a finished run stays identifiable.
- `training_run_active` is stale-marked at `end()`, so the annotation region is exact.

Two concerns that were conflated, separated. Do not "simplify" this back to one metric.

## Three corrections to the energy design in `docs/training-observability.md`

All measured on the box. Section D2 is wrong in ways that would ship a permanently empty panel.

**1. The energy PromQL is wrong twice over.** Beyond the already-known metric name, `label` is not a
label on the energy metric at all. Verified:

```promql
increase(node_hwmon_energy_joule_total{label="gpu"}[1m])                          -> 0 series
increase(node_hwmon_energy_joule_total[1m])
  * on(chip,sensor) group_left(label) node_hwmon_sensor_label{label="gpu"}        -> 802.86
increase(node_hwmon_energy_joule_total{sensor="energy4"}[1m])                     -> 802.86
```

`label` lives only on `node_hwmon_sensor_label` and must be joined, exactly as the power query
already does. Every energy query in this plan uses the joined form.

**2. NVML and the firmware disagree by a stable 22.5%, so "exact" is meaningless without a source.**

```
window          NVML delta    firmware gpu counter    ratio
60 s idle        191.356 J          229.160 J         1.198
60 s loaded      635.816 J          778.997 J         1.225
60 s loaded      648.164 J          794.255 J         1.225
700 s mixed     6556.646 J         8016.155 J         1.223
```

Reproducible to three significant figures, so this is a measurement-boundary difference (GPU domain
vs rail input including regulator loss), not noise. D2 calls the NVML figure "exact"; it is exact
only relative to its own boundary. **Emit both, labelled by source.** The derived
`gpu_energy / total_energy` ratio moves from 0.30 to 0.37 purely by switching source, so a single
unlabelled number is a trap.

Useful side effect: the two counters reset independently, which patches a hole in D2's guard. D2 says
to discard a run whose NVML counter went backwards, but a driver reload that re-accumulates *past*
the start value yields a silently wrong positive delta. Cross-checking the two sources catches it:
the ratio is stable at ~1.22, so a large departure means one of them reset.

**3. The cost arithmetic is off by roughly 5x.** D2 says "a continuous 200 W box is on the order of
EUR 0.06/hour". Measured: `sys_total` idles at **13 W**, the highest value ever recorded on this box
is **42.4 W**, and a GPU-saturating run extrapolates to ~95 W. At 1.3 RON/kWh a 40-minute run costs
**between 2 and 9 bani**. D2's own parenthetical is the correct position and should be promoted:
watt-hours per run for comparing configs is the number worth having, and the currency column is
decoration.

Also: `sys_total` tracks `dc_input` to within 0.14 W and sits *below* `soc_pkg` at idle, which is
impossible for a strict superset. Treat sub-watt differences between hwmon channels as noise, and do
not present `sys_total` as a wall-socket figure.

## The idle baseline must be sampled locally, not read from Prometheus

D2 says the gauge integral's error is "well under a percent". True at run length, false at baseline
length. Comparing a 1 Hz local sampler against Prometheus's 15 s integral over identical windows:

```
700 s    +0.06%
60 s     +0.21%
60 s     +2.07%
60 s     +7.06%
```

`sys_total` has ~1.9 W of 1 Hz jitter and only 4-5 scrapes land in a 60 s window. The baseline window
is exactly the length where this bites. **Sample the baseline in-process at 1 Hz.**

Drift justifies a per-run baseline, but modestly: within a triple of windows the variation is 2-3.5%,
hour to hour it is ~11% (12.26 W to 13.67 W). Since the baseline is only 13-37% of run power, an 11%
baseline error is 1-4% of marginal energy. Worth 60 seconds; not worth agonising over.

One trap: a baseline taken immediately before launch catches the launcher's own work. A 1 Hz sampling
loop alone inflated the reading from 13.06 W to 13.86 W, a 6% self-inflicted error. Sample before
doing anything expensive.

## OOM detection, and why there are three terminal states

SIGKILL is genuinely indistinguishable from inside the victim. The kernel's OOM killer bumps
`memory.events:oom_kill` in the victim's cgroup, but on a plain SSH login the process lands in
`/user.slice/user-1000.slice/session-N.scope`, shared with the whole login session, so a non-zero
delta means "something in my session was killed", not "my child was". On a shared box with several
runs in one tmux that is real misattribution.

Verified on the box, and all of it is favourable:

```
systemd version          255            (>= 253, so login scopes get OOMPolicy=continue:
                                          a kernel OOM will not tear down the whole session)
systemd-oomd             inactive       (so userspace PSI kills, which do NOT bump oom_kill,
                                          are not in play)
systemd-run --user --scope  works
user@.service Delegate   yes            (so a private cgroup subtree is available)
memory_localevents       not set
/proc/pressure/memory    readable
```

So the delegated-cgroup approach works here. systemd 255 also means the supervisor survives a child
OOM even without a delegated unit, so the private cgroup is an attribution improvement rather than a
survival requirement. Probe both at runtime anyway rather than hardcoding them; they are config that
can flip.

**The status vocabulary, and the one that is easy to get wrong:**

- `finished` — exited 0, and the wrapper was never signalled
- `crashed` — non-zero exit, or a signal death the wrapper did not cause
- `cancelled` — **the wrapper was signalled**, whatever the child then chose to do
- `killed` — SIGKILL the wrapper did not send, cause unproven
- `oom` — `killed` plus cgroup proof (`memory.events.local` `oom_kill` delta > 0)

**`cancelled` is decided by whether the wrapper was signalled, never by how the child exited.** A
training script that traps SIGTERM, checkpoints, and exits 0 would otherwise be recorded `finished`.
That is exactly the dashboard lying about how a run ended, and it was a real bug in the research
drafts before it was caught.

Keep `killed` distinct from `oom` even with a private cgroup. It becomes rare, and rare is the reason
to keep it rather than to fold it into a neighbour.

---

## Task 0: one sparkup change

A separate 60 s scrape job for the textfile collector. The run index is immutable data; re-recording
it every 15 s for a year costs ~30 GB, against ~8 GB at 60 s. Measured: a compacted block holds
0.516 B/sample, and returns collapse past 60 s because Prometheus caps chunks at 2 hours, so chunk
overhead is constant for any interval >= 60 s.

`roles/monitoring/templates/prometheus.yml.j2`:

```diff
   - job_name: node
     static_configs:
       - targets: ["host.docker.internal:{{ node_exporter_port }}"]
+    # The textfile collector holds a permanent run index: immutable rows that
+    # do not need re-recording every 15s. Scraped separately below, at a
+    # slower interval. `collect[]` and `exclude[]` are mutually exclusive per
+    # job, hence two jobs against one target.
+    params:
+      "exclude[]": ["textfile"]
+
+  - job_name: node_textfile
+    scrape_interval: 60s
+    static_configs:
+      - targets: ["host.docker.internal:{{ node_exporter_port }}"]
+    params:
+      "collect[]": ["textfile"]
```

**Hard ceiling on the interval: `--query.lookback-delta`, default 5m.** A scrape interval above that
makes the index intermittently vanish from instant queries, which is worse than an expensive one.
60 s leaves a 5x margin. Do not raise it to 300 s to save the last 3 GB.

Note that `node_textfile_mtime_seconds` and `node_textfile_scrape_error` now exist only under
`job="node_textfile"`. Point the alerts there.

**Verify:** after `make apply`, `count(up{job="node_textfile"})` is 1, and
`rate(scrape_samples_scraped{job="node_textfile"}[5m])` is non-zero.

---

## Task 1: energy sampling

**Files:** Create `src/sparks/energy.py`, `tests/test_energy.py`

**Step 1: Write the failing tests**

```python
import pytest

from sparks.energy import EnergyReading, Sampler, watt_hours


def test_watt_hours_converts_from_joules() -> None:
    assert watt_hours(3600.0) == pytest.approx(1.0)
    assert watt_hours(0.0) == 0.0


def test_a_reading_reports_both_gpu_sources_separately() -> None:
    # NVML and the firmware counter disagree by a stable ~22.5% because they
    # measure at different boundaries. One unlabelled number is a trap.
    r = EnergyReading(total_joules=1000.0, gpu_nvml_joules=300.0,
                      gpu_firmware_joules=367.0, idle_watts=13.0, seconds=100.0)
    assert r.gpu_nvml_joules != r.gpu_firmware_joules
    assert r.marginal_joules == pytest.approx(1000.0 - 13.0 * 100.0)


def test_marginal_energy_never_goes_negative() -> None:
    # A run quieter than the baseline is measurement noise, not free energy.
    r = EnergyReading(total_joules=100.0, gpu_nvml_joules=0.0,
                      gpu_firmware_joules=0.0, idle_watts=13.0, seconds=100.0)
    assert r.marginal_joules == 0.0


def test_the_two_gpu_sources_are_cross_checked() -> None:
    # The measured ratio is ~1.22. A large departure means one counter reset,
    # which is the failure D2's backwards-delta guard cannot catch.
    ok = EnergyReading(total_joules=1.0, gpu_nvml_joules=1000.0,
                       gpu_firmware_joules=1220.0, idle_watts=0.0, seconds=1.0)
    assert ok.sources_agree
    bad = EnergyReading(total_joules=1.0, gpu_nvml_joules=1000.0,
                        gpu_firmware_joules=5000.0, idle_watts=0.0, seconds=1.0)
    assert not bad.sources_agree


def test_a_sampler_without_nvml_degrades_rather_than_raising() -> None:
    # Development happens on macOS, where there is no NVML and no hwmon.
    s = Sampler(nvml=None, hwmon=None)
    assert s.baseline_watts(seconds=0.0) == 0.0
```

**Step 2:** `uv run pytest tests/test_energy.py -v` → FAIL, `ModuleNotFoundError`

**Step 3: Implement**

`src/sparks/energy.py`. Read the hwmon sysfs files directly rather than through Prometheus: at the
60 s baseline window the 15 s scrape integral is up to 7% wrong.

```python
"""What a run cost, measured two ways because the two disagree.

NVML's `nvmlDeviceGetTotalEnergyConsumption` and the firmware's `gpu` energy
counter differ by a stable 22.5% under load, reproducible to three significant
figures. That is a measurement-boundary difference, not noise, so a single
"GPU energy" number is meaningless without saying which one it is. Both are
reported, and their ratio is the cross-check that catches a counter reset.

Whole-box energy has no counter, only the `sys_total` gauge, so it is
integrated. Sampled here at 1 Hz rather than read back from Prometheus: at a
60 s window the 15 s scrape integral was measured up to 7% wrong.
"""

import time
from dataclasses import dataclass
from pathlib import Path

HWMON = Path("/sys/class/hwmon")
SOURCE_RATIO = 1.22
"""Measured firmware/NVML ratio for GPU energy. Used only as a sanity bound."""
RATIO_TOLERANCE = 0.5


def watt_hours(joules: float) -> float:
    return joules / 3600.0


@dataclass(frozen=True)
class EnergyReading:
    total_joules: float
    gpu_nvml_joules: float
    gpu_firmware_joules: float
    idle_watts: float
    seconds: float

    @property
    def marginal_joules(self) -> float:
        """Energy attributable to the run: total minus what the box would have
        drawn anyway. Clamped at zero, because a run quieter than its own
        baseline is measurement noise rather than free energy."""
        return max(0.0, self.total_joules - self.idle_watts * self.seconds)

    @property
    def sources_agree(self) -> bool:
        """Whether the two GPU counters are in their usual relationship.

        False means one of them reset mid-run. This catches the case D2's
        backwards-delta guard misses: a driver reload that re-accumulates past
        the start value gives a wrong but positive delta."""
        if self.gpu_nvml_joules <= 0:
            return self.gpu_firmware_joules <= 0
        ratio = self.gpu_firmware_joules / self.gpu_nvml_joules
        return abs(ratio - SOURCE_RATIO) <= RATIO_TOLERANCE
```

Add a `Sampler` that locates the spbm hwmon chip by reading `name`, resolves `sys_total` and `gpu`
through the `*_label` files, and exposes `baseline_watts(seconds)` sampling `power1_input` at 1 Hz.
Every accessor must return 0.0 rather than raise when the path is absent, because development
happens on macOS and because `spbm_enabled` may be false on someone else's box.

**Step 4:** tests pass. **Step 5:** commit.

**Step 6: verify against the real box**, not just the unit tests:

```bash
make deploy
ssh "$SPARKS_HOST" '$SPARKS_VENV/bin/python -c "
from sparks.energy import Sampler
s = Sampler.detect()
print(\"idle watts over 10s:\", s.baseline_watts(10.0))
print(\"gpu nvml J:\", s.gpu_nvml_joules(), \"gpu firmware J:\", s.gpu_firmware_joules())
"'
```

Expect idle around 13 W. **A training job may be running; check first with `pgrep -af run_e0` and do
not report a contaminated number as a baseline.**

---

## Task 2: the run directory and `summary.json`

**Files:** Create `src/sparks/summary.py`, `tests/test_summary.py`

One directory per run under `$SPARKS_SHARED_DIR/runs/<run_id>/` holding `summary.json`, `stdout.log`
and the command line. `summary.json` is the source of truth the run index is rebuilt from, so it must
be complete enough to regenerate the index after losing the TSDB entirely.

Schema, with every field justified:

```python
{
  "run_id": "run-20260805-1420-e0",
  "run_name": "e0",
  "user": "vlad",                  # who to ask about it
  "git_sha": "abc1234",
  "command": ["python", "train.py"],
  "started_unix": 1785847319.0,    # time.time(), matches the dashboard axis
  "ended_unix": 1785847367.0,
  "duration_seconds": 48.02,       # from monotonic, immune to clock steps
  # Three orthogonal fields, systemd's model. Never collapse them into one int:
  # a child that genuinely calls exit(137) and a SIGKILLed child both give the
  # caller $? = 137, so only these fields can tell them apart.
  "status": "finished",            # finished | crashed | cancelled | killed | oom
  "exit_code": 0,                  # null when killed by a signal
  "signal": None,                  # signal name when killed, else null
  "escalated_to_sigkill": False,   # the child ignored its grace period
  "energy": {
    "total_joules": 1810.0,
    "marginal_joules": 1186.0,
    "gpu_nvml_joules": 543.0,
    "gpu_firmware_joules": 663.0,  # both, because they disagree by 22.5%
    "idle_watts": 13.0,
    "sources_agree": True
  },
  "final_loss": 0.412              # or absent, never null-as-zero
}
```

`duration_seconds` comes from `time.monotonic()` while `started_unix`/`ended_unix` come from
`time.time()`. The wall-clock pair positions the run on the dashboard's time axis; the monotonic
delta is the duration, and is immune to NTP stepping the clock mid-run. They will disagree slightly
and that is correct.

---

## Task 3: the process wrapper

**Files:** Create `src/sparks/process.py`, `tests/test_process.py`

The hard part, and where a mistake makes the dashboard lie.

Use `start_new_session=True` and forward deliberately. The alternative, leaving the child in the
wrapper's process group, means the tty delivers Ctrl-C to the child directly and a forwarding wrapper
delivers it twice. `timeout(1)` treats this tradeoff as unsolvable and exposes `--foreground` for it;
a new session costs the child its controlling terminal (`open("/dev/tty")` fails with ENXIO), which
for a training run is correct.

Seven details, every one of them measured, and each one is a way the dashboard lies if missed:

1. **Signal the group AND the pid.** A child that calls `setpgid` itself escapes the group.
2. **Chase every terminating signal with SIGCONT.** A child stopped by SIGTSTP never runs its SIGTERM
   handler: measured burning its whole grace period and dying to SIGKILL with its checkpoint lost.
3. **Never `sys.exit()` from the handler.** A default-disposition SIGTERM does not run `atexit`, so
   `emit.py`'s `atexit.register(self._shutdown)` backstop silently never fires. The handler sets a
   flag and forwards; the process reaches its normal return path and calls `end(status)` itself.
4. **Never block unboundedly in `wait()`.** PEP 475 auto-retries an interrupted `wait(timeout=None)`,
   so a handler's flag is never observed and any escalation deadline armed around it is dead code.
   Measured: handler at t=0.51s, `wait()` returned at t=4.02s only when the child exited on its own.
   Poll with a bounded timeout.
5. **Take `ended_at` the moment `wait()` returns**, before the group sweep and before joining the tee.
   Measured a 1.5s run recorded as **6.5s** because orphaned workers held the stdout pipe open, so
   the tee never saw EOF and `join(timeout=5)` ran its full timeout.
6. **Exit codes are masked to 8 bits.** `SystemExit(256)` produces returncode **0**: a failing child
   reported as success. Clamp anything synthesised.
7. **Sweep the group after reaping.** A launcher that exits promptly while its workers ignore SIGTERM
   is the normal case; the strays hold GPU memory and the stdout pipe.

**Classification, and the bug worth naming.** Cancellation is a property of the wrapper, not of the
child:

```python
def classify(returncode: int, interrupted_by: int | None) -> Outcome:
    if returncode >= 0:
        if interrupted_by is not None:
            # We were told to stop and the child complied. However it chose to
            # exit, this run did not run to completion. A script that traps
            # SIGTERM to checkpoint and then exits 0 is NOT `finished`.
            return Outcome("cancelled", exit_code=returncode,
                           signal_name=signal.Signals(interrupted_by).name,
                           wrapper_exit=128 + interrupted_by)
        if returncode == 0:
            return Outcome("finished", exit_code=0, wrapper_exit=0)
        return Outcome("crashed", exit_code=returncode, wrapper_exit=returncode)

    signum = -returncode
    if interrupted_by is not None:
        status = "cancelled"
    elif signum == signal.SIGKILL:
        status = "killed"          # may be OOM; only the cgroup counter decides
    else:
        status = "crashed"
    return Outcome(status, exit_code=None, signal_name=signal.Signals(signum).name,
                   wrapper_exit=128 + signum)
```

**Output buffering, measured.** A pipe makes libc and CPython switch from line-buffered to block
buffered. A child printing five lines at 0.3s intervals:

```
stdout=PIPE, default env       all five lines at t=1.54s, in one burst
stdout=PIPE, PYTHONUNBUFFERED  0.02, 0.32, 0.62, 0.92, 1.23s
```

Over tens of minutes that is a dead terminal versus a live feed. Use a pipe plus
`PYTHONUNBUFFERED=1` plus a tee thread writing to both the terminal and the log, with
`stderr=STDOUT` so interleaving is preserved. Output is not lost on SIGKILL: 100000/100000 lines
captured, because the tee drains the pipe after the child dies.

A pty is the only fix for a non-Python child, since `PYTHONUNBUFFERED` cannot help a C binary
(measured: everything at 1.91s through a pipe, streaming through a pty). Ship it as an opt-in flag,
and note the two gotchas: a pty inserts CR before every LF via ONLCR unless cleared, and EOF differs
by platform (Linux raises `OSError` EIO, macOS returns `b''`). Handle both or the pty path hangs on
one of them.

**OOM classification** reads `memory.events.local` from a per-run cgroup inside the delegated
subtree, with `ru_maxrss` from `os.wait4` and the PSI high-water mark as corroborating signals.
Degrade to `killed` rather than guessing.

**Tests**, with real child processes and no mocks. The matrix that was actually verified:

```
exit 0                              finished / 0    / None      $? = 0
exit 3                              crashed  / 3    / None      $? = 3
uncaught exception                  crashed  / 1    / None      $? = 1
genuine exit 137                    crashed  / 137  / None      $? = 137
external SIGKILL                    killed   / null / SIGKILL   $? = 137
SIGTERM, child complies with exit 0 cancelled/ 0    / SIGTERM   $? = 143
SIGTERM, child ignores it           cancelled/ null / SIGKILL   $? = 137, escalated
```

Note rows 4 and 5: both give the caller `$? = 137`, and only `summary.json` can distinguish them.
That is the reason for three orthogonal fields.

Also test: a stopped child is revived by SIGCONT and shuts down cleanly; a launcher with three
SIGTERM-ignoring workers leaves zero survivors (assert the group is empty, which is the only shape
that proves no leak); and 100k lines of output survive a SIGKILL.

---

## Task 4: `sparks run`

Extend the CLI with `run --name <name> -- <cmd>...`. Order of operations matters:

1. mint `run_id`, create the run directory
2. **sample the idle baseline before doing anything expensive** (self-contamination inflated a
   reading by 6%)
3. read both GPU energy counters
4. `begin()` on a `RunMetrics`, and emit `training_run_active` = 1
5. export `SPARKS_RUN_ID` and `SPARKS_PROMETHEUS_URL` into the child's environment
6. run the child
7. read the counters again, compute the reading
8. write `summary.json`, `end(status)`, rebuild the index

Print the deep link at launch, in live form, and again at completion pinned to the run's window.

### The parent/child split, which must be enforced in code

**The child owns its progress metrics; the parent owns lifecycle, energy and terminal state.** The
metrics that matter are produced inside the training loop, so *every* option requires the child to
cooperate: there is no design where an arbitrary unmodified command emits a loss curve. Given that,
importing a library beats inventing a wire format, and it keeps `sparks run` usable for a child in
any language, which merely gets no training curves.

Two consequences are load-bearing, and the second is the stronger argument.

**Two `RunMetrics` on one series is a data-loss bug, not a style question.** `_beat()` pushes
`training_run_info` and the heartbeat every 5s with identical labels. Two processes writing the
*same* series choose timestamps independently, which is out of order, a 400, and a rollback that
destroys the batch carrying `training_loss`. That is the exact failure slice 1's live spike measured.
Disjoint series from two processes is fine; the same series from two processes is not.

**Only the parent is guaranteed to outlive the run.** An OOM-killed child never runs `atexit`, never
flushes, and can never write its own terminal status. The parent must own `training_run_status` for
the wrapper to be able to report `crashed` or `killed` at all. That is the whole feature.

`metrics.LIFECYCLE` already draws exactly this line: five metrics for the parent, the rest for the
child. So the change to `emit.py` is one flag:

```python
def __init__(self, run_id, url, info=None, labels=None, autostart=True,
             lifecycle=True):
    """`lifecycle=False` suppresses the run's own record of itself.

    The supervisor and the training child both hold a RunMetrics for one
    run_id and MUST write disjoint series. The supervisor owns
    metrics.LIFECYCLE because it is the only process guaranteed to outlive the
    run; the child owns everything else.
    """
```

`begin()`, `end()` and `_beat()` return early when it is false, with `end()` still calling
`_shutdown()` so the child's own series are flushed and stale-marked.

The child's entry point, which no-ops cleanly outside `sparks run` so the same script still runs
standalone:

```python
def from_env(**labels: str) -> RunMetrics | None:
    run_id = os.environ.get("SPARKS_RUN_ID")
    url = os.environ.get("SPARKS_PROMETHEUS_URL")
    if not run_id or not url:
        return None
    return RunMetrics(run_id, url, labels=labels, lifecycle=False)
```

**One follow-on worth building in the same task:** because `METRICS` is a closed allowlist, the parent
can push stale markers for the child's series when the child dies abnormally. The child cannot do
this for itself precisely in the case where it matters, so without it a killed run's loss curve
flat-lines for the lookback window instead of stopping dead.

---

## Task 5: the run index

**Files:** Create `src/sparks/index.py`, `tests/test_index.py`

Rebuild one aggregated `sparks_runs.prom` from `$SPARKS_SHARED_DIR/runs/*/summary.json`.

**One file, not one per run.** Measured per-file overhead is ~73 µs locally and ~150 µs on the box's
slower cores, perfectly linear: 5000 files would cost ~0.75 s of every 15 s scrape forever, against
~20 ms for the aggregate. The single file stops being comfortable around 100k series / 10 MB, which
is 3-4x the 5000-run horizon.

**Shape:** one `sparks_run_info` gauge valued 1 carrying identity labels, plus numeric gauges keyed
only on `run_id`. Gauges, not counters: these are final measurements, and `rate()` over them is
nonsense. **Use `_joules`, not `_watt_hours`** — `promtool check metrics` warns on watt-hours, and
Prometheus naming guidance says prefer joules. Divide by 3600 in the panel.

**Only write terminal runs.** A run that appears as `running` then flips to `finished` stales the
first series and creates a second, doubling churn. `summary.json` only exists at completion, so this
is free.

```
# HELP sparks_run_info Identity of a completed training run. Always 1.
# TYPE sparks_run_info gauge
sparks_run_info{run_id="run-20260805-1420-e0",user="vlad",run_name="e0",status="finished"} 1
# HELP sparks_run_duration_seconds Wall-clock duration of the run.
# TYPE sparks_run_duration_seconds gauge
sparks_run_duration_seconds{run_id="run-20260805-1420-e0"} 48.02
# HELP sparks_run_energy_joules Total energy drawn over the run.
# TYPE sparks_run_energy_joules gauge
sparks_run_energy_joules{run_id="run-20260805-1420-e0"} 1810
```

**Format rules that are fatal if broken**, all verified against node_exporter 1.12.1:

- The file **must end with a newline**, or the whole file is dropped.
- **`# TYPE ... info` is rejected.** Only counter/gauge/summary/untyped/histogram. An info metric is
  declared `gauge`.
- TYPE appears **once per family and before its first sample**, so headers cannot be repeated per run.
- Only three escapes in label values: `\\`, `\"`, `\n`. A `\t` is a parse error.
- **Explicit timestamps make the collector skip the entire file**, so a past run cannot be backdated.

**Atomic write, with the trap:**

```python
fd, tmp = tempfile.mkstemp(dir=d, prefix=".sparks_runs.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(render())          # must end with "\n"
    os.chmod(tmp, 0o644)           # mkstemp gives 0600, which node_exporter skips
    os.replace(tmp, target)
except BaseException:
    os.unlink(tmp)
    raise
```

`mkstemp` creates mode **0600**, and renaming that into place creates exactly the unreadable file the
docs warn about. The temp name must not end in `.prom` or the collector reads it half-written. Same
directory matters for rename atomicity and to inherit the setgid group. `os.replace` is `rename(2)`
and is sufficient; fsync is not needed, since after a power loss the index is regenerated from the
summaries anyway.

Measured: a naive `open(target, "w")` lost 30 of 60 concurrent scrapes to truncation or parse errors;
the mkstemp-chmod-replace sequence lost 0 of 53.

---

## Task 6: `training_run_active`, and the annotation

**Files:** Modify `src/sparks/metrics.py`, `src/sparks/emit.py`, `dashboards/training-runs.json`

Add `training_run_active` to `METRICS` and **not** to `LIFECYCLE`, so it is stale-marked at `end()`.
Emit it from `_beat()` alongside the info metric. A test must assert it is absent from `LIFECYCLE`,
with the measured overshoot numbers in the comment, or someone will "tidy" it in.

Annotation entry for `annotations.list`, no credentials:

```json
{
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "enable": true,
  "iconColor": "green",
  "name": "Training runs",
  "target": { "expr": "training_run_active", "refId": "Anno" },
  "titleFormat": "{{run_name}}",
  "textFormat": "{{run_id}}",
  "tagKeys": "run_id,run_name"
}
```

Known limits, worth stating in the panel description rather than discovering later: region edges
quantize to the query step, so over a 7-day window a 3-minute run is a smear; two runs closer than
one step merge into one region; and title, text and tags can only come from label values, so a final
loss cannot appear in the annotation text.

---

## Task 7: the overview dashboard

**Files:** Create `dashboards/sparks-overview.json`

One table, one row per run, from instant queries joined on `run_id`. The join works precisely because
the info metric is valued 1, so multiplication passes the numeric value through untouched:

```promql
sparks_run_duration_seconds * on(run_id) group_left(user,run_name,status) sparks_run_info
```

Columns: run_id, user, run_name, status, duration, watt-hours, marginal watt-hours, cost, final loss.
Data links per row into `training-runs` with the time range pinned to that run. Stat tiles above:
runs this week, total kWh, total cost.

**This is an instant query at `now()`, deliberately.** The index is a current-state table; history
lives in the *values* (`sparks_run_start_timestamp_seconds`), not in sample timestamps. That is why
retention does not erase it.

Extend `tests/check_dashboard.py` to check both dashboards rather than one hardcoded path, and add
the `sparks_run_*` names to `METRICS`.

---

## Task 8: alerts for the silent failures

**Files:** Create `alerts/sparks.yml`

sparkup's `INSTALL_CLAUDE.md` documents that a non-world-readable `.prom` fails silently. **That is
wrong** — it sets `node_textfile_scrape_error 1`. The genuinely silent failures, measured:

- a duplicate series within one file, or across two files with matching HELP: `scrape_error 0`,
  the duplicate is dropped by the gatherer, visible only in
  `promhttp_metric_handler_errors_total{cause="gathering"}`
- a file truncated at a clean line boundary: `scrape_error 0`, rows just missing
- **an empty or headers-only file: `scrape_error 0`, a *fresh* mtime, and zero series.** Total data
  loss with every health signal green. Nothing in node_exporter reports this.

```yaml
groups:
  - name: sparks
    rules:
      - alert: SparksRunIndexEmpty
        # The only defence against an empty index. node_exporter reports a
        # fresh mtime and no error for a headers-only file.
        expr: absent(sparks_run_info)
        for: 15m
      - alert: SparksTextfileError
        expr: node_textfile_scrape_error{job="node_textfile"} > 0
        for: 15m
      - alert: SparksDuplicateSeries
        expr: rate(promhttp_metric_handler_errors_total{cause="gathering"}[15m]) > 0
```

Gate the writer with `promtool check metrics < file`, which exits 3 on `metric not unique` and so
catches the duplicate case the collector will not.

---

## Task 9: deploy and verify on the box

`make deploy`, then a real `sparks run` around a short command, and confirm:

- `summary.json` exists and its `duration_seconds` matches the wall-clock pair to within a second
- `sparks_runs.prom` is 0644 and `promtool check metrics` passes it
- `count(sparks_run_info)` is non-zero under `job="node_textfile"`
- the annotation region on `training-runs` matches the run's real span rather than overshooting by
  five minutes, which is the whole point of Task 6
- `prometheus_api_remote_write_invalid_labels_samples_total` is still 0

Then run the status matrix against the real box, not just the unit tests:

```bash
sparks run --name t1 -- true                      # finished / 0
sparks run --name t2 -- sh -c 'exit 3'            # crashed  / 3
sparks run --name t3 -- sh -c 'sleep 60' &        # then kill -9 the child
sparks run --name t4 -- sh -c 'trap "exit 0" TERM; sleep 60' &   # then kill the WRAPPER
```

`t4` is the one that matters: the child traps SIGTERM and exits 0, and the status must be
**`cancelled`**, not `finished`. If it reads `finished`, the classification is inverted and the
dashboard is lying about how runs end.

Confirm too that `training_loss` for a killed run stops dead rather than flat-lining, which proves
the parent stale-marked the child's series on its behalf.

---

## Open questions this plan does not settle

- **Does the index grow forever?** 5000 runs is 30k series, fine on this box against ~1800 today. But
  growth is unbounded in principle, and every ecosystem that shipped per-run metrics without a cap
  (Jenkins most visibly) retrofitted a TTL after OOMs. Decide consciously whether the writer caps the
  index or you accept linear growth.
- **`admin:admin` is live on the box's Grafana and grants server admin to anyone on the LAN.** Out of
  scope here, and it is a sparkup change, but it should not stay open.
- The cost column is decoration at these magnitudes. Ship it because it was asked for, but the
  comparison that earns its place is watt-hours per run between configs.
