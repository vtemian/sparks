# Quality Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port sisif's quality gates to sparks: the expanded ruff rule set with sisif's complexity caps (C901≤5, branches≤4, returns≤4, statements≤15), refactor every violating function to comply, rename five exceptions to `*Error`, add pre-commit hooks, a lint+test CI workflow, and a CLAUDE.md with the mandatory code-quality rules.

**Architecture:** Land behaviour-preserving refactors first, each commit green under the *current* gate (`make check`). Flip the ruff config last, in one commit that goes green immediately. Pre-commit, CI, and CLAUDE.md follow. Verification per refactor task uses explicit `ruff check` invocations with the target caps passed on the command line, since the config is not in `pyproject.toml` until the flip.

**Tech Stack:** Python 3.12, uv, ruff 0.16.1, mypy strict (already on), pytest, pre-commit (via `uvx`), GitHub Actions.

---

## Decision log (locked for this plan)

1. **Sisif's caps, refactor now, no grandfathering.** Vlad chose "Adopt + refactor now" over noqa-grandfathering and over ruff-default thresholds.
2. **Renames:** `Malformed`→`MalformedError`, `NotProvisioned`→`NotProvisionedError`, `PullFailed`→`PullFailedError`, `InvalidLabel`→`InvalidLabelError`, and `_Interrupted`→**`_AbortError`** (NOT `_InterruptedError`: one underscore away from the `InterruptedError` builtin in an EINTR-adjacent module; the exception signals an operator abort, and the caller records `cancelled`).
3. **Line length stays 88.** The reformat diff is 8 cosmetic files from an earlier narrower width, not a width change. Say so in the commit message.
4. **Global ignores are principles, not debt.** Sisif carries ignores marked "to be fixed gradually" (B904, S113); sparks takes none of those. Every ignore in the new config must state its reason inline.
5. **`PLR0917` joins `PLR0913` in the ignores.** They are the same "wide signatures are deliberate" call; ignoring only PLR0913 leaves the ignore inert because PLR0917 is the stricter twin (8 src hits).
6. **`S603`/`S607` ignored globally, with a two-claim comment.** Verified: every subprocess call in the repo is list-argv, zero `shell=True`. The honest phrasing is "argv is always a list and shell=True never appears" (S603) plus "ssh/git/rsync resolve via PATH on purpose" (S607) — NOT "no shell is ever involved": `ssh_argv` (remote.py:305) hands a `shlex.join`-quoted command string to the *remote* shell by design.
7. **`TRY301` ignored globally.** The raise-then-translate idiom (raise a domain error mid-`try`, annotate in the `except` below) is deliberate in `build`/`push`/`pull`.
8. **BLE001 sites get `# noqa: BLE001`, keeping their prose comments.** All 13 src sites verified deliberate (telemetry-never-kills-a-run, queue-never-stops, callbacks of unknowable type). `tests/on_box.py` gets a per-file ignore instead — the catch-all-to-FAIL-row IS its design.
9. **`docs/superpowers/plans/*.md` and `docs/superpowers/specs/*.md` are historical records — the renames do NOT touch them.** Only living docs (`INSTALL_CLAUDE.md`) get updated.
10. **CI is one job running `make check`** — the same aggregate developers run locally. No separate pre-commit job duplicating what `make lint` already does.
11. **`dock.remove_quietly`'s `timeout` parameter is deleted** (real bug found: declared, never read, both call sites pass `CLEANUP_TIMEOUT_SECONDS` which is silently discarded). Smallest honest fix is removal, not implementation nobody asked for.
12. **Commit style:** plain imperative sentences matching repo history ("Stop shipping the Docker CLI in the fire image."), occasionally `fix:` for bugfixes. No AI attribution of any kind.

## File map

| Path | Change |
|---|---|
| `pyproject.toml` | Expanded `[tool.ruff.lint]`: select, ignore, caps, per-file-ignores (Task 14) |
| `.pre-commit-config.yaml` | **Create.** ruff + ruff-format hooks (Task 15) |
| `.github/workflows/checks.yml` | **Create.** `make check` on PR + push to main (Task 16) |
| `CLAUDE.md` | **Create.** Mandatory quality rules adapted from sisif (Task 17) |
| `INSTALL_CLAUDE.md` | Update BLE001 trap (Task 14), rename mentions (Task 2) |
| `src/sparks/box.py` | Renames (Task 2) |
| `src/sparks/series.py`, `src/sparks/fire/runner.py` | Renames (Task 2) |
| `src/sparks/fire/launch.py` | Rename `_Interrupted`→`_AbortError` (Task 2), BLE noqa (Task 4), decomposition (Task 13) |
| `src/sparks/fire/contain.py` | TC imports, `_signum` (Task 3), `_Abort` dataclass + `main` split (Task 5) |
| `src/sparks/fire/control.py` | duration constants (Task 3), `resolve` split (Task 6) |
| `src/sparks/fire/ctl.py` | `build_parser` split (Task 6) |
| `src/sparks/client/cli.py` | `build_parser` split (Task 7) |
| `src/sparks/client/remote.py` | BLE noqa (Task 4), `build`/`push` (Task 7), `fetch_registry_url`/`submit_remote` (Task 8) |
| `src/sparks/energy.py` | BLE noqa (Task 4), `marginal_joules`/`_read_micro` (Task 9) |
| `src/sparks/index.py` | `render_queue` split (Task 10) |
| `src/sparks/shared.py` | `exclusive` split (Task 10) |
| `src/sparks/fire/engine.py` | `pull` split (Task 11) |
| `src/sparks/fire/process.py` | `classify`/`oom_kills`/`__init__`/`run` (Task 12) |
| `src/sparks/emit.py` | BLE noqa (Task 4), comment rename (Task 2) |
| `src/sparks/run.py` | BLE noqa (Task 4) |
| `src/sparks/spool.py` | `# noqa: ANN401` on `advance` (Task 3) |
| `src/sparks/dock.py` | Delete dead `timeout` param (Task 3) |
| `tests/…` | Rename call sites, `CheckError`, small fixes, abort-flag updates |

## Global constraints (verified by research; violating any of these is a defect)

- **Every refactor is behaviour-preserving.** No error-message wording changes (several are pinned by `pytest.raises(match=...)`), no reordering of externally visible calls, no Popen kwarg changes.
- **Monkeypatch surface:** tests patch `launcher_mod._supervisor_metrics`, `client.fetch_registry_url`, `capture`, `local_user`, `provenance`, `sparks.dock.client` **as module attributes**. Extracted helpers must call these through module globals, stay module-level themselves, and never import them `from X import y` into another namespace.
- **`launch()`:** `metrics.end(status)` and `_rebuild(shared_dir)` stay lexically in `launch`'s `finally`. `status = completed.outcome.status` stays outside the `try` that precedes that `finally`. `_interruptible()` keeps its exact scope (opens before `sampler.baseline()`, closed before `Supervisor` is constructed); `_AbortError` propagates uncaught through any new helper. No null-object for metrics — a constructed-but-stubbed `RunMetrics` would recreate the second-writer bug (`INSTALL_CLAUDE.md`, single-writer invariant).
- **`Supervisor.run()`:** the Detail-5 freeze (`ended_wall, ended_mono, interrupted_by, escalated, oom_after`) is captured between `_wait_for_child()` and `_sweep()`, and passed to helpers **as parameters — helpers must not re-read `self`**. `log` opens before the `try`, closes unconditionally in the `finally`. `_install()` before `Popen`, inside the try guarded by `_restore()`. The conditional `reader.close()` ordering (`_restore()` → conditional close → `log.close()`) is load-bearing (test at `tests/test_process.py:348` hangs 20s without it). `self.pgid` survives `run()` returning. Instance attributes read by signal handlers (`child`, `pgid`, `interrupted_by`, `escalated`, `_deadline`) stay instance attributes.
- **`contain.main()`:** the two abort checkpoints stay in `main` (pushing checkpoint 1 into a helper makes `logs.assert_not_called()` pass for the wrong reason). `stop(timeout=int(process.GRACE_SECONDS))` keeps the `int()` cast (asserted as literal `30`).
- **`process.classify`:** the `returncode >= 0` boundary stays `>=`; `clamp_exit` on every synthesised exit; negation of `returncode` happens exactly once.
- **`index.py`:** helper signatures keep string-quoted `"spool.Entry"` annotations — `spool` is imported under `TYPE_CHECKING` only, and the one-way dependency is documented policy. Helpers return lists the caller concatenates (family block order is the node_exporter contract).
- **`shared.exclusive`:** `os.close(fd)` stays in `exclusive`'s `finally` — moving it into a helper releases the lock before the `yield` and the existing test would NOT catch it. `raise TimeoutError(...) from None` keeps `from None`.
- **`remote.py`:** `build` echoes with `print(..., end="")` (chunks carry newlines); `push` prints one status per line — do not merge the echo helpers. `split_tag` must keep the port-colon behaviour (`spark.local:5000/u/n` → tag `latest`). `--context` default `Path.cwd()` stays evaluated at parser-construction time.
- **After each task:** `make check` green (old gate until Task 14, new gate after). Full suite: 37 test files, no `live` marker without the harness.

---

### Task 0: Branch

- [ ] `cd /Users/whitemonk/projects/ai/sparks && git checkout -b quality-gates`
- [ ] `git add docs/superpowers/plans/2026-08-07-quality-gates.md && git commit -m "Plan the quality-gate port from sisif."`

### Task 1: Reformat drift (own commit, no lint changes)

- [ ] `uv run ruff format src tests` — expect exactly 8 files changed (2 docstring collapses in `__init__.py`s, 5 line-unwraps, 1 trailing blank line).
- [ ] `make check` — green.
- [ ] Commit: `git add -u && git commit -m "Reformat the files an earlier narrower width left behind."`

### Task 2: Exception renames

Sites are exhaustively mapped; historical plan docs are excluded on purpose.

- [ ] `Malformed`→`MalformedError`: `src/sparks/box.py` (34 def, 41/62 docstrings, 80/84/87 raises), `src/sparks/fire/ctl.py:148`, `src/sparks/fire/cli.py:120`, `src/sparks/fire/supervise.py:201`, `tests/test_box.py:43,89,97`, `tests/on_box.py:158`, `INSTALL_CLAUDE.md:189` prose.
- [ ] `NotProvisioned`→`NotProvisionedError`: `src/sparks/box.py:30,128`, `src/sparks/fire/ctl.py:82,148`, `src/sparks/fire/supervise.py:65,69,201`, `src/sparks/fire/cli.py:25,120`, `tests/test_box.py:195`.
- [ ] `_Interrupted`→`_AbortError`: `src/sparks/fire/launch.py:280,303,119` only.
- [ ] `PullFailed`→`PullFailedError`: `src/sparks/fire/runner.py:70,61(docstring),191`, `src/sparks/fire/engine.py:36(import),152,159,162,165`, `tests/test_engine.py:17,237`, `tests/test_runner.py:75`.
- [ ] `InvalidLabel`→`InvalidLabelError`: `src/sparks/series.py:16,29,32,34`, `src/sparks/emit.py:86(comment)`, `tests/test_series.py:3,22,27,32`, `tests/test_emit.py:8,151,153`, `INSTALL_CLAUDE.md:267` prose.
- [ ] Do NOT touch message strings ("pull failed: …"), lowercase prose ("not provisioned"), or `docs/superpowers/**`.
- [ ] Verify: `grep -rnE '\b(Malformed|NotProvisioned|PullFailed|InvalidLabel|_Interrupted)\b' src tests INSTALL_CLAUDE.md` → zero identifier hits.
- [ ] `make check && uv run mypy` — green. Commit: `"Give exceptions their Error suffix."`

### Task 3: Small mechanical fixes

- [ ] Autofixes: `uv run ruff check --select TC006,PLR0402 --fix src tests` (3 cast-quoting sites; `import sparks.dock as dock` → `from sparks import dock` in `contain.py:22`, `tests/test_dock.py:6`, `tests/test_engine.py:14`).
- [ ] `src/sparks/fire/contain.py`: move `Callable`, `Sequence` (line 14), `FrameType` (16), `Container` (20) into an `if TYPE_CHECKING:` block (file already has `from __future__ import annotations`; the annotated dicts at 105-106 are annotations, so this is safe). Add `from typing import TYPE_CHECKING` to the existing typing import.
- [ ] `src/sparks/fire/contain.py:110`: `def handler(signum: …` → `_signum` (registered for both signals, never discriminates).
- [ ] `src/sparks/fire/launch.py:154,235`: `LOG.error("sparks: could not run %s: %s", command, e)` → `LOG.exception("sparks: could not run %s", command)`; same shape at 235 for "could not record %s". The traceback is strictly more information.
- [ ] `src/sparks/fire/control.py:178-185` (`_duration`): name the literals — module constants `_MINUTE, _HOUR, _DAY = 60, 3600, 86400` above the function; comparisons use them. 4 returns = at cap, passes.
- [ ] `src/sparks/spool.py:340`: `def advance(path: Path, **changes: Any) -> State:  # noqa: ANN401` — `**changes` forwards into `dataclasses.replace()` whose field types are heterogeneous; `object` breaks mypy strict.
- [ ] `src/sparks/dock.py`: delete the `timeout: float = 60.0` parameter from `remove_quietly` (declared, never read); update both call sites `src/sparks/fire/engine.py:116,314` to drop `timeout=CLEANUP_TIMEOUT_SECONDS`; delete `CLEANUP_TIMEOUT_SECONDS` if nothing else reads it (`grep -rn CLEANUP_TIMEOUT_SECONDS src tests`).
- [ ] `tests/test_process.py:87`: add `check=False` to the `ps` probe (latent bug: `ps` exits non-zero when the pid is gone and the helper wants `False`, not an exception).
- [ ] `tests/test_client.py:360`: rename `test_ssh_argv_honours_SPARKS_REMOTE` → `test_ssh_argv_honours_sparks_remote_bin_env`.
- [ ] `tests/check_dashboard.py:134`: `out.append` loop → `out.extend(t["expr"] for t in targets if "expr" in t)` (keep the surrounding comment).
- [ ] `tests/check_dashboard.py:76`: `CheckFailed` → `CheckError` (refs: check_dashboard.py 84,163,168,173,182; tests/test_check_dashboard.py 5,23,49).
- [ ] `make check && uv run mypy` — green. Commit: `"fix: small lint debts — dead timeout param, unchecked ps probe, typing-only imports."` (split into two commits if the dock.py change deserves its own: `"fix: drop the timeout remove_quietly never honoured."`)

### Task 4: BLE001 noqa pass (13 src sites, comments preserved)

Append `# noqa: BLE001` to the `except Exception…:` lines only; every prose comment stays byte-identical:

- [ ] `src/sparks/emit.py:238` (pump guard), `:247`, `:286` (flush/stale "telemetry never kills a run").
- [ ] `src/sparks/energy.py:230` — careful: its comment is split across lines 230-231; the noqa goes after the code, before the existing trailing comment cannot merge — restructure as `except Exception:  # noqa: BLE001 — NVML's own error type, and a reload…` keeping the sentence intact across both lines.
- [ ] `src/sparks/energy.py:374`.
- [ ] `src/sparks/fire/launch.py:94,148,232,276`.
- [ ] `src/sparks/run.py:56`.
- [ ] `src/sparks/fire/runner.py:98`.
- [ ] `src/sparks/client/remote.py:435` — the one undocumented site: add run.py:56's justification (`# deliberately broad: a missing account is not a failure`) plus the noqa.
- [ ] `src/sparks/summary.py:201` (`except BaseException:` cleanup-then-reraise): verify whether BLE001 fires on it (`uv run ruff check --select BLE src/sparks/summary.py --no-cache`); if it does, noqa with a comment noting it re-raises unconditionally — a cleanup idiom, not a swallow.
- [ ] Verify: `uv run ruff check --select BLE001 src` → clean. `make check` green. Commit: `"Mark the deliberate broad excepts for the BLE gate."`

### Task 5: `contain.py` — `_Abort` state + `main` decomposition

Clears PLW0603 ×3 and C901/PLR0912/PLR0915 on `main`, and makes `main` re-entrant.

- [ ] Replace module globals `_abort_requested` (line 25) and the holder-list pattern with a small mutable dataclass instantiated per `main()` call:
```python
@dataclass
class _Abort:
    """Set by the signal handler; read at the two checkpoints in main()."""
    requested: bool = False
    container: Container | None = None
```
  `_install_signal_handlers(abort: _Abort)`: handler sets `abort.requested = True` and stops `abort.container` when present. `_request_abort` (test hook) keeps its handler-registry lookup, falling back to setting the passed/module-reachable state — preserve its documented contract; the handler registries (`_previous_signal_handlers`, `_signal_handlers`) stay module-level (they are process-wide by nature).
- [ ] Update the two behaviour pins in the same change: `tests/test_contain.py:161` (`contain._abort_requested = True` → set through the new state; the `containers.run.side_effect` closure needs access to the `_Abort` instance — pass it via the module-level test hook or expose `main`'s abort through `_request_abort`), and `:217` (`_request_abort` path — should keep working unmodified if the registry fallback is preserved).
- [ ] Extract from `main` (132-185): `_create(client, args) -> Container` (143-160, run_kwargs call + `container.id is None` RuntimeError guard; caller writes the cidfile; the `container: Container | None = None` pre-assignment stays in `main` before the `try`); `_stop_and_wait(container) -> int` (166-169); `_stream(container) -> None` (171-175); `_cleanup(container) -> None` (183-185: `_restore_signal_handlers()` + suppressed remove). Both abort checkpoints stay in `main`.
- [ ] Verify: `uv run ruff check --select C901,PLR0912,PLR0915,PLW0603 --config 'lint.mccabe.max-complexity=5' --config 'lint.pylint.max-branches=4' --config 'lint.pylint.max-returns=4' --config 'lint.pylint.max-statements=15' src/sparks/fire/contain.py --no-cache` → clean.
- [ ] `uv run pytest tests/test_contain.py -v` then `make check`. Commit: `"Hold contain's abort state in an object instead of module globals."`

### Task 6: Pattern-setters — `control.resolve` + `ctl.build_parser`

- [ ] `src/sparks/fire/control.py:82-109` `resolve`: extract `_disambiguate(matches: list[spool.Entry], needle: str) -> spool.Entry` (lines 100-107: live filter, single-live return, formatted raise) and optionally `_matches(found, needle)` for the two comprehensions. `resolve`: empty guard → exact tier → fuzzy tier → no-match raise → `return matches[0] if len(matches) == 1 else _disambiguate(...)`. Exact tier stays BEFORE fuzzy. Error strings verbatim (`"matches several"`, `"no job matches"`, `"no jobs"` are regex-pinned).
- [ ] `src/sparks/fire/ctl.py:95-137` `build_parser`: extract `_add_queue(sub, shared)`, `_add_control_verbs(sub, shared)` (the existing 4-verb loop), `_add_rpc_verbs(sub, shared)` (the three `help=argparse.SUPPRESS` ones — reserve/commit/contract). Pass `sub`/`shared` explicitly; never rebuild `shared`. mypy strict needs the subparsers type: module alias `Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]`.
- [ ] Verify caps on both files (same ruff invocation as Task 5, both paths) → clean. `uv run pytest tests/test_client.py tests/test_fire_ctl.py tests/test_fire_cli.py -v && make check`. Commit: `"Flatten resolve and the fire argv builder."`

### Task 7: Client parser + `build`/`push`

- [ ] `src/sparks/client/cli.py:61-123` `build_parser`: extract `_submit_parser(sub, host)`, `_queue_parser(sub, host)`, `_job_parsers(sub, host)` (the existing loop). Same `Subparsers` alias trick. `--context` default `Path.cwd()` stays inside the helper. `description=__doc__` stays on the root parser.
- [ ] `src/sparks/client/remote.py`:
  - Delete the dead `except ClientError: raise` arms in `build` (93-94) and `push` — `ClientError` is unrelated to `dock.DockerException`, so the arm re-raises what already propagates. (Same dead shape exists in `engine.pull`; handled in Task 11.)
  - `_failed(chunk: dict[str, Any]) -> bool` — true on `error`/`errorDetail`, truthiness semantics (unifies build's key-presence and push's truthiness; no test distinguishes them).
  - `_echo_build(chunks, tag) -> None` — prints `chunk["stream"]` with `end=""` (load-bearing), raises `ClientError(f"docker build failed for {tag}")` on `_failed`.
  - `split_tag(tag: str) -> tuple[str, str]` — repository + image tag, `latest` when the last path segment (`tag.rsplit("/", 1)[-1]`) carries no `:`. Sits beside `tag_for`. Port-colon behaviour pinned by `tests/test_client.py:457`.
  - `PUSH_HINT` module constant with the existing two-line message (must keep the phrase `insecure-registries`), constant-docstring in house style.
  - `_echo_push(chunks) -> None` — prints `status` per line, raises `ClientError(PUSH_HINT)` on `_failed`.
- [ ] Add a direct test for `split_tag` in `tests/test_client.py` (house style sentence-name, e.g. `test_split_tag_defaults_latest_without_splitting_the_port_colon`) covering `spark.local:5000/u/n` → `("spark.local:5000/u/n", "latest")` and `…/n:abc` → split. Write it first, watch it fail (function doesn't exist), implement, watch it pass.
- [ ] Verify caps on both files → clean. `uv run pytest tests/test_cli.py tests/test_client.py -v && make check`. Commit: `"Flatten the client parser and the build and push streams."`

### Task 8: `fetch_registry_url` + `submit_remote`

- [ ] `fetch_registry_url` (126-149): split on the existing seam — `_box_config(host) -> bytes` (the ssh/cat with FileNotFoundError/Timeout/returncode handling; the `(done.stderr or done.stdout)` fallback verbatim; `capture_output=True`, `check=False`, `timeout=SSH_TIMEOUT_SECONDS` stay one `subprocess.run` call — argv assertions depend on it) and `_registry_url(raw: bytes, where: str) -> str` (TOML parse + non-empty-string check). `fetch_registry_url` = 2 statements, keeps its docstring. Callers keep reaching it as a module global (`tests/test_client.py:504` monkeypatches the attribute).
- [ ] `submit_remote` (343-397): extract `ship_to(data: Path, host: str, reserved: str) -> None` (the rsync block 381-395, exactly one `subprocess.run` between the two `capture` calls; do NOT merge with `ship` — messages differ and `ship` mkdirs), `_registry_for(host, image, registry_url) -> str | None` (the ask-only-when-building conditional), `_reserve(host, name, who) -> str` (reserve + empty-answer raise). Body keeps the docstring's five steps readable in order: build/push (via `_resolve_tag`) → reserve → ship_to → commit.
- [ ] Verify caps on `remote.py` → clean file-wide. `uv run pytest tests/test_client.py -v && make check`. Commit: `"Split submit_remote along the five steps its docstring names."`

### Task 9: `energy.py` returns

- [ ] `_read_micro` (341-361): fold the two absence guards — `raw = _read(path) if path is not None else None` then `if raw is None: return None`. 4 returns = at cap. Every comment survives; sentinel check stays BEFORE the divide (`test_a_five_kilowatt_draw…` is the tripwire).
- [ ] `marginal_joules` (140-161): extract `_baseline_joules(self) -> float | None` returning `idle_watts * seconds`, or `None` when `idle_watts <= 0` (no baseline) or `gpu_idle_watts > BUSY_GPU_WATTS` (someone else's job) — each refusal keeps its trailing WHY-comment. `marginal_joules` drops to 4 returns. Frozen dataclass: method name must not collide with a field (`_baseline_joules` is clear). Do not confuse with the *stored* `summary.Energy.marginal_joules` field.
- [ ] Verify: `uv run ruff check --select PLR0911 --config 'lint.pylint.max-returns=4' src/sparks/energy.py --no-cache` → clean. `uv run pytest tests/test_energy.py -v && make check`. Commit: `"Bring the energy refusals under the return cap."`

### Task 10: `index.render_queue` + `shared.exclusive`

- [ ] `render_queue` (209-263): extract `_job_rows(jobs) -> list[str]`, `_depth_rows(jobs) -> list[str]`, `_stamp_rows(jobs) -> list[str]` — signatures use string-quoted `"spool.Entry"` (TYPE_CHECKING-only import is policy). Helpers RETURN lists; `render_queue` concatenates (block order is the node_exporter contract). Heartbeat stays unconditional. Do not factor a shared helper with `render` — `tests/test_index.py:105` pins `render`'s output byte-for-byte and `render` is under the cap.
- [ ] `exclusive` (93-119): extract `_grab(fd: int, directory: Path, timeout: float) -> None` (the poll loop; 50ms sleep; `raise TimeoutError(f"{directory} was locked for over {timeout:g}s") from None`). `os.close(fd)` STAYS in `exclusive`'s `finally`. Three-argument docstring stays on `exclusive`; only the poll rationale moves down.
- [ ] Verify caps on both files → clean. `uv run pytest tests/test_queue_index.py tests/test_index.py tests/test_shared.py -v && make check`. Commit: `"Split the queue renderer by metric family and the lock by its poll."`

### Task 11: `engine.pull`

- [ ] Extract `_bounded(stream: Iterable[dict[str, Any]], deadline: float) -> Iterator[dict[str, Any]]` — yields chunks, raises `PullFailedError` (post-rename) on deadline; and `_pull_line(chunk, log_path) -> str` — returns the log line, raises `PullFailedError` naming `log_path.name` on `error`/`errorDetail`. Message strings verbatim (`match="pull"` + user-facing via runner).
- [ ] `log_path.open("wb")` stays where it is (`assert log.exists()` depends on creation before the exception escapes). `except PullFailedError: raise` stays FIRST and is NOT dead here (unlike remote.py — `dock.DockerException` could otherwise re-wrap during iteration)… verify this claim: if `PullFailedError` is unrelated to `DockerException` the arm IS dead — check the class hierarchy (`PullFailedError(Exception)`, `dock.DockerException` is docker's) and if dead, delete it and note that lazily-raised `DockerException` from the generator still lands in the outer arm because the generator is consumed inside the `try`.
- [ ] Verify caps on `engine.py` → clean. `uv run pytest tests/test_engine.py tests/test_runner.py -v && make check`. Commit: `"Bound the pull stream in one place."`

### Task 12: `process.py` — `classify`, `oom_kills`, `__init__`, `run`

Highest-risk file. One commit per function is fine; full suite after each.

- [ ] `classify` (107-143): extract `_status_for_signal(signum: int, interrupted_by: int | None) -> str` (lines 132-137). Minimal churn; `classify` → 4 branches, complexity 5. `>=` boundary, single negation, `clamp_exit` everywhere — unchanged.
- [ ] `oom_kills` (146-164): extract `_events_text(cgroup: Path | None) -> str` (returns `""` when absent/OSError — behaviourally identical because the loop then falls through to the kept `return 0`) and `_int_or_zero(value: str) -> int`. `partition(" ")` stays (cgroup v2 format; a line with no space must fall through, not raise). Never-raises contract: `OSError` swallowed un-narrowed.
- [ ] `Supervisor.__init__` (174-204): extract `_blank_state(self) -> None` holding the six state initialisers (199-204), called last in `__init__`. Zero signature change — the only option touching no test. All six must be set before `run()` (signal handler reads them).
- [ ] `Supervisor.run` (208-297): extract per the seam that respects the Detail-5 freeze:
  - `_start(self) -> io.BufferedReader` (226-250: `_install()`, `Popen` with kwargs untouched, `_capture_pgid()`, the catch-up branch, BufferedReader wrap).
  - `_start_tee(self, reader, log) -> threading.Thread` (251-254).
  - a frozen `_Reaped` dataclass `(returncode, ended_wall, ended_mono, interrupted_by, escalated, oom_after)` — the freeze at 263-265 becomes one named concept.
  - `_drain(self, tee) -> None` (267-275: `_sweep()` first, bounded join, alive-warning). Called AFTER the freeze.
  - `_close(self, reader, tee, log) -> None` (283-285: the conditional-close ordering). `finally` = `self._restore(); self._close(...)`.
  - `_finish(self, reaped: _Reaped) -> Outcome` (287-291: `classify` + `replace` + oom promotion) — reads ONLY the frozen parameter, never `self`.
- [ ] Verify caps on `process.py` → clean. `uv run pytest tests/test_process.py -v` (real forks, signals the test runner — run it alone first) then `make check`. Commits: `"Split classify on the sign of the returncode."`, `"Read the oom counter through one guard."`, `"Blank the supervisor state in one place."`, `"Carve run along the freeze it must not move."`

### Task 13: `launch.py` decomposition (the big one)

- [ ] Extract, all module-level in `launch.py`:
  - `_Record` frozen dataclass (`user, name, sha, command`) + `_cleaned(command, name, user, sha) -> _Record` (81-84). Then shrink `_record_failed_launch`'s 10-parameter signature by passing `_Record`.
  - `_announce(on_reserved, run_id, run_dir) -> None` (89-95, absorbs the `is not None` and the swallow-and-warn with its comment and noqa).
  - `_Window` frozen dataclass (field names identical to the `EnergyReading` field names, so a mis-pairing is a name error not a silent swap) + `_open_window(sampler, baseline_seconds) -> _Window` (105-118, `_interruptible()` wrapped INSIDE this helper, exiting on return — preserves `test_the_handlers_do_not_outlive_the_baseline`). `sampler is None → Sampler.detect()` moves inside. NO `except Exception` in it — `_AbortError` must propagate.
  - `_close_window(window, sampler, completed, baseline_seconds) -> summary.Energy` (177-193, 208-218, 222-231: end reads, `max()` duration, three `energy.delta` calls, `EnergyReading`, SOURCES_DISAGREE warning).
  - `_record(run_dir, run_id, rec, completed, energy) -> None` (195-220: build + `summary.save`).
  - `_cancelled(...)` and `_crashed(...)` folding the two early-return failure arms (120-136, 154-159).
  - `_child_env(run_id, url) -> dict[str, str]` (the `if url` conditional).
  - `_begin(metrics) -> None` and `_end(metrics, status) -> None` free functions doing the `is not None` test — moves 3 branches out of `launch` with zero semantic change and keeps the `_supervisor_metrics` monkeypatch target intact. NO null-object.
- [ ] `launch` body after: sanitise → reserve → announce → try/except `_AbortError` around `_open_window` → metrics + env → try/except around `Supervisor(...).run()` → try/except/finally tail where the `finally` still lexically contains `_end(metrics, status)` and `_rebuild(shared_dir)`, with `status` assigned before the `try`.
- [ ] Verify caps: full ruff invocation on `launch.py` → clean. `uv run pytest tests/test_launcher.py -v` (16 direct launch() calls, no mocks) then the full `make check`.
- [ ] **Live gate:** `make live` (needs Docker running; brings up a real Prometheus). `tests/test_live.py::test_a_crashed_run_still_lands_its_whole_record` is the only test that can catch a reintroduced second writer. If Docker is unavailable, STOP and flag it — do not merge this task unverified.
- [ ] Commit: `"Flatten launch without moving what its finally guards."`

### Task 14: Flip the gate — pyproject.toml + INSTALL_CLAUDE.md trap

- [ ] Replace `[tool.ruff.lint]` in `pyproject.toml`:

```toml
[tool.ruff.lint]
select = [
    "F", "E", "W", "I", "B", "C4", "UP", "SIM", "S", "T20",
    "N", "ANN", "A", "ARG", "ERA", "PLR", "PLW", "PLE", "PERF",
    "RUF", "TRY", "BLE", "EM", "C90", "RET", "TC", "PIE",
]
ignore = [
    "TRY003",            # long messages at the raise are the house style
    "EM101", "EM102",    # and they are raw or f-strings, deliberately
    "TRY301",            # raise-then-translate inside try is the stream idiom
    "PLR0913", "PLR0917",# wide signatures are deliberate; both twins or the ignore is inert
    "S603", "S607",      # subprocess is this tool's job: argv is always a list and
                         # shell=True never appears; ssh/git/rsync resolve via PATH on
                         # purpose. ssh_argv still hands a shlex-quoted string to the
                         # REMOTE shell by design -- that is not what S603 measures.
]

[tool.ruff.lint.mccabe]
max-complexity = 5

[tool.ruff.lint.pylint]
max-branches = 4
max-returns = 4
max-statements = 15

[tool.ruff.lint.per-file-ignores]
# Test modules: assertions, fixture arity, magic numbers, long arranges.
"tests/**" = [
    "ANN",      # fixtures and helpers read fine untyped
    "ARG",      # pytest fixtures are requested by name, not used by name
    "PLR2004",  # magic values ARE the assertion
    "PLR0915",  # a long arrange/act/assert is one thought
    "S101",     # assert
    "S105", "S106",
    "S108",     # "/tmp/..." literals are argparse fixtures; nothing is created
]
# Makefile-invoked CLI scripts that live in tests/, not test modules.
"tests/check_dashboard.py" = ["C901", "PLR0912", "T201"]
"tests/on_box.py"          = ["BLE001", "C901", "PLR0912", "T201"]
# The CLI surface prints; that is its output contract.
"src/sparks/client/cli.py"     = ["T201"]
"src/sparks/client/remote.py"  = ["T201"]
"src/sparks/fire/cli.py"       = ["T201"]
"src/sparks/fire/ctl.py"       = ["T201"]
"src/sparks/fire/supervise.py" = ["T201"]
```

- [ ] `uv run ruff check src tests` → expect clean; fix any stragglers the refactors missed (there will be a few — e.g. new B904/RET/PIE hits the old select never measured). Do not add ignores to silence stragglers; fix them.
- [ ] `INSTALL_CLAUDE.md:123-124`: invert the trap — BLE is now selected, `# noqa: BLE001` is required on the deliberate broad excepts, and RUF100 flags an *unneeded* one. Both lines change together.
- [ ] `make check && uv run mypy` green. Commit: `"Turn on the sisif rule set with its complexity caps."`

### Task 15: Pre-commit hooks

- [ ] Create `.pre-commit-config.yaml`:

```yaml
default_stages: [pre-commit]

repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.1
    hooks:
      - id: ruff
      - id: ruff-format
```

- [ ] `uvx pre-commit run --all-files` → both hooks pass. `uvx pre-commit install` to activate locally.
- [ ] Commit: `"Run ruff from pre-commit."`

### Task 16: CI checks workflow

- [ ] Create `.github/workflows/checks.yml`:

```yaml
# The same gate a developer runs locally: make check is lint + typecheck +
# tests + the dashboard allowlist. One job, so CI can never drift from the
# local command.

name: checks

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv sync
      - run: make check
```

  (`setup-uv` reads `.python-version` for the interpreter; `make check` = lint + typecheck + test + dashboard, all Docker-free. The `live` marker is excluded by `make test` already.)
- [ ] Push the branch, confirm the workflow runs green on the PR.
- [ ] Commit: `"Gate PRs on make check."`

### Task 17: CLAUDE.md

- [ ] Create `/Users/whitemonk/projects/ai/sparks/CLAUDE.md`:

```markdown
# CLAUDE.md

Standing rules for working in sparks. Repo facts and traps live in
INSTALL_CLAUDE.md — read that too, first.

## Before every commit

- `make check` must pass: lint + typecheck + tests + dashboard allowlist.
- Work on a branch, never directly on main. Push after committing.
- Anything touching the emitter's threading, shutdown, or the launch/supervise
  seam also needs `make live` (real Prometheus) before merge. No unit test can
  catch a second writer; INSTALL_CLAUDE.md explains why.

## Code quality standards (mandatory, enforced by ruff)

The gate is real: complexity ≤5, branches ≤4, returns ≤4, statements ≤15 per
function. Do not noqa your way past it; decompose.

### No nesting
Flatten control flow with guard clauses and early returns. One level of
indentation inside a function body.

### Names are contracts
A function does what its name says, all of it, and nothing else. `fetch_*`
returns or raises; `find_*` may return None; `*_argv` builds argv and never
runs it. No Manager/Service/Helper/Data names. Private helpers are `_` plus
one or two terse domain words.

### Fail fast, fail visible
Catch specific exceptions. The only broad excepts are the marked, deliberate
ones (telemetry never kills a run; a bad job never stops the queue) — each
carries `# noqa: BLE001` and a comment saying why. Never add one without both.

### Comments say why, never what
The house style is prose that argues: the measured number, the rejected
alternative, the incident that produced the rule. If a comment restates the
code, delete it. Module constants carry their own docstrings.

### Classes only where state is the point
Dataclasses for structured data, exceptions, protocols, and stateful
lifecycle objects (Supervisor, RunMetrics, Buffer). Business logic is plain
functions. No wrapper classes around what a function can do.

### Explicit over implicit
No hidden defaults, no fallbacks that guess. This repo refuses to start
rather than guess a path (exit 78); keep that spirit: a wrong guess that
looks like success is the worst outcome.

## Testing

- Tests are named as sentences about behaviour, and they test behaviour:
  real forks, real signals, real files. No mocks of our own code.
- Mock only true externals (the Docker SDK client, subprocess argv capture) —
  and assert on what our code sent them.
- A test that re-implements the logic it checks is vacuous; assert on what
  the code actually produced (see the `_stale_batch` story in
  INSTALL_CLAUDE.md).
- Never delete a failing test. Never relax `[tool.mypy] strict`.
- Error paths are behaviour: pin the message the user sees
  (`pytest.raises(match=...)`).

## Commits

Plain imperative sentences in the repo's voice; `fix:` prefix for bugfixes.
Small and frequent. Never `git add -A` without `git status` first.
```

- [ ] Commit: `"Write down the rules the gate enforces."`

### Task 18: Final verification

- [ ] `make check` — green, full output pristine.
- [ ] `uv run pytest -m "not live" -q` — all pass, no warnings that weren't there before.
- [ ] `uvx pre-commit run --all-files` — green.
- [ ] `uv run ruff check src tests --statistics` — zero violations.
- [ ] `git log --oneline main..HEAD` — every commit message in house voice, no attribution.
- [ ] If Docker is available: `make live` one final time (the launch and process refactors are the reason).
- [ ] Report: branch name, commit list, and the two behaviour changes worth calling out to Vlad (`remove_quietly` losing its dead `timeout`, `_failed` unifying build/push error detection on truthiness).
```
