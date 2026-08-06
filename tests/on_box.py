"""The checks that need the real box, in one runnable pass.

Three things cannot be verified from a laptop, and all three were left open
when the multi-user-safety plan shipped:

  permissions   the one-time `chmod -R 2775` migration. Measured: a second user
                cannot chmod the first's 2700 tree and cannot create anything
                inside it, so new code cannot self-heal this for them. The tree
                has to be corrected once by its owner or root.
  acceptance    the plan's definition of done for tier 1, which is written in
                terms of two accounts, three umasks and a real filesystem.
  calibration   BUSY_GPU_WATTS, MIN_COUNTER_WINDOW_SECONDS and RATIO_TOLERANCE
                are calibrated to one box and were guessed for every other one.
                Each is measured here rather than argued about.

Every check prints PASS, FAIL or SKIP and says what it observed, and the script
exits non-zero if anything failed, so it can be a gate rather than a report.
Nothing here writes outside `--shared-dir`, and `--fix-permissions` is the only
flag that changes anything it did not create.

    uv run python tests/on_box.py --shared-dir /srv/bbm
    uv run python tests/on_box.py --shared-dir /srv/bbm --fix-permissions
    uv run python tests/on_box.py --shared-dir /srv/bbm --other-user alice \
        --url http://127.0.0.1:9090
"""

import argparse
import contextlib
import itertools
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sparks import box, index, launcher, shared, summary
from sparks.client import cli
from sparks.energy import (
    MIN_COUNTER_WINDOW_SECONDS,
    RATIO_TOLERANCE,
    SOURCE_RATIO,
    Sampler,
)
from sparks.run import current_user

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Report:
    results: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, outcome: str, check: str, detail: str = "") -> None:
        self.results.append((outcome, check, detail))
        line = f"  {outcome:4}  {check}"
        print(f"{line}\n        {detail}" if detail else line, flush=True)

    def ok(self, check: str, detail: str = "") -> None:
        self.record(PASS, check, detail)

    def bad(self, check: str, detail: str = "") -> None:
        self.record(FAIL, check, detail)

    def skip(self, check: str, detail: str = "") -> None:
        self.record(SKIP, check, detail)

    def expect(
        self, condition: bool, check: str, detail: str = "", passed: str = ""
    ) -> bool:
        """`detail` explains a failure; `passed` is shown instead when it holds,
        so a green line does not print the empty list of things that went
        wrong."""
        if condition:
            self.ok(check, passed)
        else:
            self.bad(check, detail)
        return condition

    def summarise(self) -> int:
        counts = {
            outcome: sum(1 for o, _, _ in self.results if o == outcome)
            for outcome in (PASS, FAIL, SKIP)
        }
        print(f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
        if counts[FAIL]:
            print("\nfailed:")
            for outcome, check, detail in self.results:
                if outcome == FAIL:
                    print(f"  {check}: {detail}")
        return 1 if counts[FAIL] else 0


def heading(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 68 - len(title)), flush=True)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def explain_mode(path: Path) -> str:
    """Why a directory is not 2775, in the terms that decide what to do next.

    The common non-bug is the setgid bit alone: POSIX has chmod clear S_ISGID
    when the caller is unprivileged and the directory's group is not one of its
    own, so a dry run against /tmp (root:wheel on macOS) reports 0775 and looks
    like a failure. On the real shared tree, whose group every user is in, it
    sticks -- and if it does not, that is the defect this tier is about.
    """
    actual = mode_of(path)
    wanted = shared.DIR_MODE
    only_setgid_missing = actual & 0o777 == wanted & 0o777 and not actual & stat.S_ISGID
    if only_setgid_missing:
        group = path.stat().st_gid
        mine = sorted({*os.getgroups(), os.getegid()})
        if group not in mine:
            return (
                f"{path} is {actual:04o}: the permission bits are right and only "
                f"setgid is missing, which chmod clears when the directory's group "
                f"({group}) is not one of yours ({mine}). Expected outside the "
                f"shared group, eg under /tmp; on the real tree this must hold."
            )
    return f"{path} is {actual:04o}, wanted {wanted:04o}"


@contextmanager
def umask(value: int) -> Iterator[None]:
    """os.umask is process-global and returns the old value rather than scoping,
    which is exactly why the launcher must never call it. A verification script
    is the one place it is legitimate, and it is restored on every path."""
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------


def check_contract(report: Report, shared_dir: Path) -> None:
    """What sparkup's `sparks` role promised, checked against the box and against
    the shared tree this script was pointed at.

    The last of those matters most. Two sources of truth for one path is how a
    run ends up recorded where nobody reads it, and this script taking
    --shared-dir is itself a second source.
    """
    heading("contract: what sparkup declared this box provides")
    try:
        contract = box.load()
    except box.Malformed as e:
        report.bad("the contract parses", str(e))
        return
    if contract is None:
        report.bad(
            "the box declares itself provisioned",
            f"{box.config_path()} is absent, so `sparks-run` refuses with exit "
            f"{cli.EX_CONFIG}. Converge sparkup with the sparks role.",
        )
        return
    report.ok("the box declares itself provisioned", str(box.config_path()))

    complaints = box.preflight(contract)
    report.expect(
        not complaints,
        "everything the contract promises is there",
        "; ".join(complaints),
        passed=f"{contract.runs_dir} and {contract.textfile_dir}",
    )
    report.expect(
        contract.shared_dir == shared_dir,
        "the contract and --shared-dir agree",
        f"contract says {contract.shared_dir}, this run was given {shared_dir}. "
        f"One of them is wrong, and the contract is what real runs will use.",
        passed=str(shared_dir),
    )
    # Not fatal: the index is published through the textfile collector, and a
    # box with no Prometheus still records complete runs on disk.
    report.expect(
        bool(contract.prometheus_url),
        "the contract names a Prometheus",
        "prometheus_url is empty, so runs record to disk but publish nothing",
        passed=contract.prometheus_url,
    )


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------


def check_permissions(report: Report, shared_dir: Path, fix: bool) -> None:
    heading("permissions: the migration new code cannot do for the other user")
    runs = shared_dir / "runs"
    if not runs.is_dir():
        report.skip("shared tree exists", f"{runs} is not a directory yet")
        return

    wrong_dirs = [runs] if mode_of(runs) != shared.DIR_MODE else []
    wrong_files: list[Path] = []
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        if mode_of(run_dir) != shared.DIR_MODE:
            wrong_dirs.append(run_dir)
        for name in (summary.FILENAME, "output.log"):
            f = run_dir / name
            # 0664 is the target; anything the group cannot read or write is
            # what strands the other user, so that is what is reported.
            if f.is_file() and mode_of(f) & 0o060 != 0o060:
                wrong_files.append(f)

    if fix:
        refused = _repair(wrong_dirs, wrong_files)
        report.expect(
            not refused,
            "every wrong mode could be repaired",
            "chmod on another user's file is EPERM; root or its owner must run "
            f"this: {[str(p) for p in refused[:5]]}"
            if refused
            else "",
        )
        wrong_dirs = [d for d in wrong_dirs if mode_of(d) != shared.DIR_MODE]
        wrong_files = [f for f in wrong_files if mode_of(f) & 0o060 != 0o060]

    report.expect(
        not wrong_dirs,
        f"every directory under {runs} is 2775",
        "not 2775, so the other user cannot list or create: "
        + "; ".join(explain_mode(d) for d in wrong_dirs[:5]),
    )
    report.expect(
        not wrong_files,
        "every summary.json and output.log is group read-write",
        "the other user cannot read these: "
        + ", ".join(f"{f} is {mode_of(f):04o}" for f in wrong_files[:5]),
    )
    if wrong_dirs and not fix:
        print(f"\n        to repair: chmod -R 2775 {runs}   (as its owner or root)")


def _repair(dirs: list[Path], files: list[Path]) -> list[Path]:
    refused = []
    for path in dirs:
        try:
            os.chmod(path, shared.DIR_MODE)
        except OSError:
            refused.append(path)
    for path in files:
        try:
            os.chmod(path, shared.FILE_MODE)
        except OSError:
            refused.append(path)
    return refused


# --------------------------------------------------------------------------
# acceptance: the plan's definition of done for tier 1
# --------------------------------------------------------------------------


def check_acceptance(
    report: Report, shared_dir: Path, url: str | None, other_user: str | None
) -> None:
    heading("acceptance: tier 1's definition of done")
    _umask_matrix(report, shared_dir)
    _same_second_collision(report, shared_dir)
    _hostile_name(report, shared_dir)
    _exit_code(report, shared_dir)
    _unwritable_run_dir(report, shared_dir, url)
    _second_account(report, shared_dir, other_user)


def _umask_matrix(report: Report, shared_dir: Path) -> None:
    """Three runs at three umasks. The failure this catches is total: at umask
    077 the other user cannot listdir runs/, so their load_all sees zero
    summaries and the rebuild writes an empty index, wiping shared history."""
    made = []
    for value in (0o077, 0o022, 0o027):
        with umask(value):
            result = launcher.launch(
                ["true"],
                name=f"onbox-umask-{value:03o}",
                shared_dir=shared_dir,
                url=None,
                baseline_seconds=0.0,
            )
        made.append((value, result.run_dir))

    bad_dirs = [(v, d) for v, d in made if mode_of(d) != shared.DIR_MODE]
    report.expect(
        not bad_dirs,
        "a run directory is 2775 at every umask",
        _same_cause([d for _, d in bad_dirs], [f"{v:03o}" for v, _ in bad_dirs]),
        "2775 at umask 077, 022 and 027",
    )

    logs = [(v, d / "output.log") for v, d in made]
    bad_logs = [(v, f) for v, f in logs if f.is_file() and mode_of(f) & 0o060 != 0o060]
    report.expect(
        not bad_logs,
        "output.log is group read-write at every umask",
        ", ".join(f"umask {v:03o} gave {mode_of(f):04o}" for v, f in bad_logs),
        "0664 at umask 077, 022 and 027",
    )

    loaded = {s.run_id for s in index.load_all(shared_dir / "runs")}
    missing = [d.name for _, d in made if d.name not in loaded]
    report.expect(
        not missing,
        "load_all sees all three runs",
        f"missing from the index: {missing}",
        f"{len(loaded)} runs readable",
    )


def _same_cause(paths: list[Path], labels: list[str]) -> str:
    """One explanation when every offender has the same one, which is the usual
    case: three umasks hitting one filesystem fail three times identically, and
    printing that verbatim three times buries the sentence that matters."""
    if not paths:
        return ""
    reasons = {explain_mode(p).split(": ", 1)[-1] for p in paths}
    if len(reasons) == 1:
        return f"at umask {', '.join(labels)}, all for one reason -- {reasons.pop()}"
    return "; ".join(
        f"umask {v}: {explain_mode(p)}" for v, p in zip(labels, paths, strict=True)
    )


def _same_second_collision(report: Report, shared_dir: Path) -> None:
    """The only hard uniqueness guarantee is mkdir raising EEXIST. Two reserves
    in one second must produce two directories, not one shared one."""
    runs = shared_dir / "runs"
    first_id, first = shared.reserve_dir(runs, "onbox-collide", "tester")
    second_id, second = shared.reserve_dir(runs, "onbox-collide", "tester")
    report.expect(
        first != second and first_id != second_id,
        "two reserves in one second get two directories",
        f"both resolved to {first}" if first == second else f"{first_id}, {second_id}",
    )


def _hostile_name(report: Report, shared_dir: Path) -> None:
    """A non-UTF-8 --name must neither crash the wrapper nor freeze the index.
    Proven failure mode: json.dumps escapes the lone surrogate so the write
    succeeds, then every later rebuild raises UnicodeEncodeError and is
    swallowed as a warning, so the colleague's runs stop appearing forever."""
    try:
        result = launcher.launch(
            ["true"],
            name="onbox-\udcffname",
            shared_dir=shared_dir,
            url=None,
            baseline_seconds=0.0,
        )
    except Exception as e:
        report.bad("a non-UTF-8 --name does not crash the wrapper", repr(e))
        return
    report.ok("a non-UTF-8 --name does not crash the wrapper", result.run_id)

    target = shared_dir / "index" / index.FILENAME
    try:
        rows = index.rebuild(shared_dir / "runs", target)
    except Exception as e:
        report.bad("the index still rebuilds after a hostile name", repr(e))
        return
    report.expect(
        rows > 0 and target.is_file(),
        "the index still rebuilds after a hostile name",
        f"{rows} rows in {target}",
    )
    _promtool(report, target)


def _promtool(report: Report, target: Path) -> None:
    # Running this file directly puts tests/ on sys.path, not the repo root, so
    # the sibling module is not importable as tests.promtool without help.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from tests import promtool
    except ImportError as e:
        report.skip("promtool accepts the rendered index", f"cannot import: {e}")
        return
    if not promtool.usable():
        report.skip("promtool accepts the rendered index", promtool.REASON)
        return
    done = promtool.check_metrics(target.read_text())
    report.expect(
        done.returncode == 0,
        "promtool accepts the rendered index",
        (done.stdout + done.stderr).strip(),
    )


def _exit_code(report: Report, shared_dir: Path) -> None:
    """A queue or a shell && reads $?. The whole suite once passed while the
    wrapper returned 0 for a crashed run."""
    from sparks import run_main

    code = run_main.main(
        [
            "--url",
            "",
            "--shared-dir",
            str(shared_dir),
            "--baseline-seconds",
            "0",
            "--",
            "sh",
            "-c",
            "exit 3",
        ]
    )
    report.expect(code == 3, "a crashed child makes the CLI exit 3", f"exited {code}")


def _unwritable_run_dir(report: Report, shared_dir: Path, url: str | None) -> None:
    """A failure while saving must not lose the record. The child chmods its own
    run directory read-only as its last act, which is what a full disk or a
    quota does to the save that follows. launch() must still return the child's
    status rather than raising, and with a url the run must still land a
    terminal status rather than sitting on the dashboard forever."""
    runs = shared_dir / "runs"
    script = f'chmod 500 "{runs}/$SPARKS_RUN_ID"; exit 0'
    run_dir = None
    try:
        result = launcher.launch(
            ["sh", "-c", script],
            name="onbox-readonly",
            shared_dir=shared_dir,
            url=url,
            baseline_seconds=0.0,
        )
        run_dir = runs / result.run_id
        report.expect(
            result.status == "finished",
            "a PermissionError while saving does not lose the status",
            f"status was {result.status!r}, exit {result.wrapper_exit}",
        )
    except Exception as e:
        report.bad("a PermissionError while saving does not lose the status", repr(e))
    finally:
        # Hand the directory back, or the next run of this script inherits a
        # tree it deliberately broke.
        if run_dir is not None and run_dir.is_dir():
            with contextlib.suppress(OSError):
                os.chmod(run_dir, shared.DIR_MODE)


def _second_account(report: Report, shared_dir: Path, other_user: str | None) -> None:
    """The motivating case: two real accounts in one group on one tree."""
    if other_user is None:
        report.skip(
            "a second account can run and see the shared history",
            "pass --other-user to test the case this whole tier exists for",
        )
        return
    before = {s.run_id for s in index.load_all(shared_dir / "runs")}
    done = subprocess.run(
        [
            "sudo",
            "-n",
            "-u",
            other_user,
            sys.executable,
            "-m",
            "sparks.run_main",
            "--url",
            "",
            "--shared-dir",
            str(shared_dir),
            "--baseline-seconds",
            "0",
            "--name",
            "onbox-other",
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        report.bad(
            f"{other_user} can complete a run in the shared tree",
            (done.stdout + done.stderr).strip()[:400],
        )
        return
    report.ok(f"{other_user} can complete a run in the shared tree")

    after = {s.run_id for s in index.load_all(shared_dir / "runs")}
    new = after - before
    report.expect(
        bool(new) and before <= after,
        f"{current_user()} still sees every run, including {other_user}'s",
        f"gained {sorted(new)}, lost {sorted(before - after)}",
    )


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


def check_calibration(report: Report, window: float) -> None:
    heading("calibration: the constants that were guessed for this box")
    sampler = Sampler.detect()
    if sampler.hwmon is None:
        report.skip("hwmon chip present", "no spbm chip; nothing here can be measured")
        return
    report.ok("hwmon chip present", str(sampler.hwmon))

    _idle_gpu_rail(report, sampler, window)
    _counter_tick(report, sampler)
    _source_ratio(report, sampler, window)


def _idle_gpu_rail(report: Report, sampler: Sampler, window: float) -> None:
    """BUSY_GPU_WATTS is 2.6x the idle rail on the box it was calibrated on.
    Measuring idle here is what turns it from a guess into a number."""
    if sampler.gpu_energy is None:
        report.skip("GPU rail idle draw", "no gpu energy counter on this chip")
        return
    base = sampler.baseline(window)
    if base.gpu_watts <= 0:
        report.bad(
            "GPU rail idle draw",
            "read 0 W, so either the counter did not advance or an endpoint "
            "could not be read; BUSY_GPU_WATTS cannot be calibrated from this",
        )
        return
    report.ok(
        "GPU rail idle draw",
        f"{base.gpu_watts:.2f} W idle, whole box {base.idle_watts:.2f} W. "
        f"Suggested BUSY_GPU_WATTS = {base.gpu_watts * 2.6:.1f} "
        f"(2.6x idle, the ratio this box was calibrated at). "
        "Run this while the box is quiet, or it measures a neighbour.",
    )


def _counter_tick(report: Report, sampler: Sampler) -> None:
    """MIN_COUNTER_WINDOW_SECONDS is set by the slowest counter's tick, and the
    plan's instruction is exactly this: read at 10 Hz for 20 s and time the
    steps. A window shorter than the tick catches one update from one source
    and two from the other, which reads as the sources disagreeing."""
    if sampler.gpu_energy is None:
        report.skip("counter tick granularity", "no gpu energy counter on this chip")
        return
    changes: list[float] = []
    previous = sampler.gpu_firmware_joules()
    started = time.monotonic()
    while time.monotonic() - started < 20.0:
        time.sleep(0.1)
        value = sampler.gpu_firmware_joules()
        if value is not None and previous is not None and value != previous:
            changes.append(time.monotonic())
            previous = value
        elif previous is None:
            previous = value
    if len(changes) < 2:
        report.bad(
            "counter tick granularity",
            f"the counter advanced {len(changes)} times in 20 s, which is too "
            "few to time; MIN_COUNTER_WINDOW_SECONDS cannot be derived",
        )
        return
    gaps = [b - a for a, b in itertools.pairwise(changes)]
    worst = max(gaps)
    report.expect(
        worst * 2 <= MIN_COUNTER_WINDOW_SECONDS,
        "MIN_COUNTER_WINDOW_SECONDS clears the slowest tick",
        f"ticked {len(changes)} times, worst gap {worst:.3f} s, mean "
        f"{sum(gaps) / len(gaps):.3f} s. Suggested minimum = {worst * 2:.1f} s "
        f"(2x the worst gap); the constant is {MIN_COUNTER_WINDOW_SECONDS} s.",
    )


def _source_ratio(report: Report, sampler: Sampler, window: float) -> None:
    """RATIO_TOLERANCE is relative because the firmware/NVML ratio moved only
    2.1% across every regime observed. It is only meaningful under load: at
    idle both deltas are near zero and their ratio is noise."""
    if sampler.nvml is None:
        report.skip("firmware/NVML ratio", "no NVML counter; the driver lacks it")
        return
    firmware0, nvml0 = sampler.gpu_firmware_joules(), sampler.gpu_nvml_joules()
    time.sleep(window)
    firmware1, nvml1 = sampler.gpu_firmware_joules(), sampler.gpu_nvml_joules()
    if None in (firmware0, nvml0, firmware1, nvml1):
        report.bad("firmware/NVML ratio", "an endpoint could not be read")
        return
    assert firmware0 is not None and nvml0 is not None
    assert firmware1 is not None and nvml1 is not None
    firmware, nvml = firmware1 - firmware0, nvml1 - nvml0
    if nvml <= 0 or firmware <= 0:
        report.skip(
            "firmware/NVML ratio",
            f"only {firmware:.2f} J firmware and {nvml:.2f} J NVML over "
            f"{window:g} s; re-run under a GPU-saturating job, because at idle "
            "this ratio is noise",
        )
        return
    ratio = firmware / nvml
    drift = abs(ratio / SOURCE_RATIO - 1.0)
    report.expect(
        drift <= RATIO_TOLERANCE,
        "the firmware/NVML ratio is within RATIO_TOLERANCE",
        f"ratio {ratio:.3f} against SOURCE_RATIO {SOURCE_RATIO}, {drift:.1%} off, "
        f"tolerance {RATIO_TOLERANCE:.0%}. Measure under load; if this box sits "
        "at a different ratio, SOURCE_RATIO is what to change.",
    )


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="on_box",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--shared-dir",
        type=Path,
        required=True,
        help="the real shared tree, eg /srv/bbm. Runs are created under it.",
    )
    parser.add_argument(
        "--section",
        action="append",
        choices=["contract", "permissions", "acceptance", "calibration"],
        help="repeatable; default is all four",
    )
    parser.add_argument(
        "--fix-permissions",
        action="store_true",
        help="chmod the shared tree to 2775/0664 where this account is allowed to",
    )
    parser.add_argument(
        "--other-user",
        help="a second account to run as via sudo -n -u, for the cross-user check",
    )
    parser.add_argument(
        "--url",
        help="Prometheus, so the record-not-lost check can confirm a status landed",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=10.0,
        help="seconds per energy measurement (default 10)",
    )
    args = parser.parse_args(argv)

    sections = args.section or ["contract", "permissions", "acceptance", "calibration"]
    report = Report()
    print(f"sparks on-box verification, as {current_user()}, on {os.uname().nodename}")

    # First: everything below assumes the shared tree is the one real runs use,
    # and this is what establishes that.
    if "contract" in sections:
        check_contract(report, args.shared_dir)
    if "permissions" in sections:
        check_permissions(report, args.shared_dir, args.fix_permissions)
    if "acceptance" in sections:
        check_acceptance(report, args.shared_dir, args.url, args.other_user)
    if "calibration" in sections:
        check_calibration(report, args.window)
    return report.summarise()


if __name__ == "__main__":
    raise SystemExit(main())
