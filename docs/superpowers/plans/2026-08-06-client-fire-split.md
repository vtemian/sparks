# Client / Fire package split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repo match a client/server model: `sparks` (laptop client) and `fire` (box server), with job supervision private inside `sparks.fire` — no public `sparks-run` / `sparks-runner` / `*_main.py`.

**Architecture:** Two packages under `src/sparks/`: `client/` and `fire/`. Shared libraries stay at `sparks.*` (box, spool, emit, …). The queue daemon is `fire`; when it starts a job it spawns `python -m sparks.fire.supervise …` (private module, not a console script) so one bad job cannot kill the daemon, without presenting a third product on PATH.

**Tech Stack:** Python 3.12, hatchling, existing pytest/ruff/mypy, Docker image ENTRYPOINT, sparkup queue compose (flag-only command).

## Global Constraints

- Product surface is exactly two console scripts: `sparks` and `fire`.
- No `sparks-run`, `sparks-runner`, `run_main.py`, or `runner_main.py` after this lands.
- Supervision of a training command stays **outside** the training container; it becomes `sparks.fire.supervise` (library/`-m`), called by `sparks.fire.engine`.
- Import paths update everywhere (src + tests); do not leave shim modules at the old paths unless a one-release deprecation is explicitly added (prefer hard cut on this branch).
- sparkup compose comments/ENTRYPOINT assumption must say `fire`; flag-only `command:` stays valid.
- TDD where behavior changes (engine argv); mechanical moves can update tests in the same task after a failing import baseline.
- Do not commit secrets; do not force-push `main`.

## Target tree

```text
src/sparks/
  __init__.py
  box.py spool.py shared.py run.py          # shared contract / ids
  emit.py buffer.py series.py metrics.py    # metrics (training + supervisor)
  index.py summary.py energy.py             # durable record / energy (used by fire)
  client/
    __init__.py
    cli.py          # sparks = sparks.client.cli:main
    remote.py       # today's client.py (ssh, build, push, data)
  fire/
    __init__.py
    cli.py          # fire = sparks.fire.cli:main  (daemon only)
    runner.py
    engine.py
    launch.py       # today's launcher.py
    process.py
    supervise.py    # today's run_main.py — python -m sparks.fire.supervise
```

**Deleted:** `cli.py`, `client.py`, `run_main.py`, `runner_main.py`, and top-level `runner.py` / `engine.py` / `launcher.py` / `process.py` (moved into `fire/`).

## File map

| Path | Role after |
|---|---|
| `src/sparks/client/cli.py` | Laptop argparse |
| `src/sparks/client/remote.py` | Queue client ops |
| `src/sparks/fire/cli.py` | Daemon argparse (flags only, no subcommand) |
| `src/sparks/fire/supervise.py` | One-job supervisor CLI for `-m` only |
| `src/sparks/fire/engine.py` | Docker; argv starts with `sys.executable, "-m", "sparks.fire.supervise", …` |
| `pyproject.toml` | `sparks` + `fire` scripts only |
| `Dockerfile` | `ENTRYPOINT ["fire"]` |
| sparkup `roles/queue/templates/compose.yml.j2` | Comment: ENTRYPOINT is `fire` |
| sparkup `roles/queue/README.md` | Wording |
| `README.md`, `INSTALL_CLAUDE.md` | Two-product docs |

---

### Task 1: Create `client/` package (move without behavior change)

**Files:**
- Create: `src/sparks/client/__init__.py` (empty or one-line package doc)
- Create: `src/sparks/client/cli.py` (from `src/sparks/cli.py`)
- Create: `src/sparks/client/remote.py` (from `src/sparks/client.py`)
- Delete: `src/sparks/cli.py`, `src/sparks/client.py`
- Modify: `pyproject.toml` — temporary scripts still pointing at new paths for client only; fire still old until Task 2–3
- Modify: all imports `from sparks import client` → `from sparks.client import remote as client` **or** `from sparks.client import remote` and update call sites to `remote.`

**Preferred import style (lock this):**
- `from sparks.client import remote`
- `from sparks.client.cli import main` / tests import `sparks.client.cli`

Inside `client/cli.py`, change `from sparks import … client` to `from sparks.client import remote` and use `remote.` instead of `client.`.

- [ ] **Step 1: Failing baseline — note current tests pass; add a canary**

```python
# tests/test_package_layout.py (new)
def test_client_lives_under_sparks_client() -> None:
    from sparks.client import cli, remote

    assert callable(cli.main)
    assert hasattr(remote, "submit_remote")
```

Run: `uv run pytest tests/test_package_layout.py -v`  
Expected: FAIL (no `sparks.client` package).

- [ ] **Step 2: Move files with git mv**

```bash
mkdir -p src/sparks/client
git mv src/sparks/cli.py src/sparks/client/cli.py
git mv src/sparks/client.py src/sparks/client/remote.py
printf '"""Laptop client: submit work to the box queue.\n"""\n' > src/sparks/client/__init__.py
git add src/sparks/client/__init__.py
```

- [ ] **Step 3: Fix imports in `client/cli.py` and tests**

In `client/cli.py`:
```python
from sparks.client import remote
# replace client.X → remote.X throughout
```

In `tests/test_cli.py`:
```python
from sparks.client import cli
from sparks.client.cli import build_parser  # if exported
```

In `tests/test_client.py`:
```python
from sparks.client import remote as client  # keep local name `client` in tests to minimize churn
```

Or rename test attribute uses to `remote` — either is fine; pick one and be consistent.

- [ ] **Step 4: Point pyproject client script**

```toml
[project.scripts]
sparks = "sparks.client.cli:main"
sparks-runner = "sparks.runner_main:main"   # still old until Task 3
sparks-run = "sparks.run_main:main"         # still old until Task 2
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_package_layout.py tests/test_cli.py tests/test_client.py -q
uv run mypy src/sparks/client
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A src/sparks/client src/sparks/cli.py src/sparks/client.py pyproject.toml tests
git commit -m "refactor: move laptop client into sparks.client"
```

---

### Task 2: Create `fire/` package and private `supervise` module

**Files:**
- Create: `src/sparks/fire/__init__.py`
- Create: `src/sparks/fire/cli.py` (from `runner_main.py`)
- Create: `src/sparks/fire/supervise.py` (from `run_main.py`)
- Create: `src/sparks/fire/runner.py`, `engine.py`, `launch.py`, `process.py` (git mv from top-level)
- Delete: old top-level modules listed above + `runner_main.py` + `run_main.py`
- Modify: internal imports to `sparks.fire.*`
- Modify: `pyproject.toml` scripts to final form
- Modify: `Dockerfile` ENTRYPOINT

**Interfaces:**
- Produces: `fire = "sparks.fire.cli:main"`
- Produces: `python -m sparks.fire.supervise` works (`if __name__ == "__main__"` in supervise.py)
- Removes: `sparks-run`, `sparks-runner` scripts

- [ ] **Step 1: Extend layout test (fails first)**

```python
def test_fire_daemon_and_supervise_modules_exist() -> None:
    from sparks.fire import cli, supervise

    assert callable(cli.main)
    assert callable(supervise.main)


def test_only_sparks_and_fire_console_scripts() -> None:
    import importlib.metadata

    scripts = importlib.metadata.entry_points().select(group="console_scripts")
    names = {ep.name for ep in scripts if ep.value.startswith("sparks.")}
    assert "sparks" in names
    assert "fire" in names
    assert "sparks-run" not in names
    assert "sparks-runner" not in names
```

(Adjust entry-point discovery for the installed editable env; after `uv sync`.)

- [ ] **Step 2: git mv into fire/**

```bash
mkdir -p src/sparks/fire
git mv src/sparks/runner_main.py src/sparks/fire/cli.py
git mv src/sparks/run_main.py src/sparks/fire/supervise.py
git mv src/sparks/runner.py src/sparks/fire/runner.py
git mv src/sparks/engine.py src/sparks/fire/engine.py
git mv src/sparks/launcher.py src/sparks/fire/launch.py
git mv src/sparks/process.py src/sparks/fire/process.py
printf '"""Box server: the queue daemon (fire) and private job supervision.\n"""\n' > src/sparks/fire/__init__.py
```

- [ ] **Step 3: Rewrite imports inside fire/**

| Old | New |
|---|---|
| `from sparks import runner` | `from sparks.fire import runner` |
| `from sparks import engine` | `from sparks.fire import engine` |
| `from sparks import launcher` | `from sparks.fire import launch as launcher` or `from sparks.fire.launch import …` |
| `from sparks.process import …` | `from sparks.fire.process import …` |
| `prog="sparks-runner"` | `prog="fire"` |
| `prog="sparks-run"` | `prog="sparks.fire.supervise"` (for argparse help only) |

In `supervise.py`, keep `main()` and:

```python
if __name__ == "__main__":
    sys.exit(main())
```

In `fire/cli.py`, docstring: queue container entry point; compose passes flags only.

- [ ] **Step 4: Engine nests supervise via `-m`, not sparks-run**

In `fire/engine.py`, replace `sparks_bin: str = "sparks-run"` with constructing argv as:

```python
import sys

def supervise_argv(self, entry, image, cidfile, run_id_file, uid, gid) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sparks.fire.supervise",
        "--url",
        self.url,
        "--name",
        entry.job.name,
        "--shared-dir",
        str(self.shared_dir),
        "--git-sha",
        entry.job.git_sha,
        "--run-id-file",
        str(run_id_file),
        "--",
        *self.container_argv(entry, image, cidfile, uid, gid),
    ]
```

Wire `start()` / `argv()` to use this. Remove `sparks_bin` constructor knobs **or** rename to `supervise_module = "sparks.fire.supervise"` for tests.

- [ ] **Step 5: Failing then passing engine test**

```python
def test_job_is_supervised_via_python_minus_m_fire_supervise(...) -> None:
    argv = docker.supervise_argv(...)  # or whatever public method builds the outer argv
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "sparks.fire.supervise"]
    assert "sparks-run" not in argv
```

Update `tests/test_engine.py` expectations that looked for `"sparks-run"`.

- [ ] **Step 6: Final pyproject + Dockerfile**

```toml
[project.scripts]
sparks = "sparks.client.cli:main"
fire = "sparks.fire.cli:main"
```

```dockerfile
ENTRYPOINT ["fire"]
CMD []
```

```bash
uv sync
```

- [ ] **Step 7: Retarget tests**

| Old test module | New |
|---|---|
| `tests/test_run_main.py` | `tests/test_fire_supervise.py` importing `sparks.fire.supervise` |
| `tests/test_runner_main.py` | `tests/test_fire_cli.py` importing `sparks.fire.cli` |
| `tests/test_launcher.py` | import `sparks.fire.launch` |
| `tests/test_runner.py` / `test_engine.py` | `sparks.fire.runner` / `sparks.fire.engine` |
| `tests/on_box.py` | use `sparks.fire.supervise` / `-m sparks.fire.supervise` if needed |

- [ ] **Step 8: Full quality gate**

```bash
uv run ruff check src tests
uv run mypy
uv run pytest -m "not live" -q
```

Expected: PASS (same or higher count than before the move).

- [ ] **Step 9: Commit**

```bash
git commit -m "refactor: sparks.fire server; supervise jobs via python -m

Laptop remains sparks (client). The box runs fire. Job supervision is a
private module, not a third console script.
"
```

---

### Task 3: Docs + sparkup compose wording

**Files:**
- Modify: `README.md`, `INSTALL_CLAUDE.md`
- Modify: `/Users/whitemonk/projects/ai/sparkup/roles/queue/templates/compose.yml.j2` (ENTRYPOINT comment)
- Modify: `/Users/whitemonk/projects/ai/sparkup/roles/queue/README.md`
- Grep both repos for `sparks-run`, `sparks-runner`, `run_main`, `runner_main`

- [ ] **Step 1: README product surface**

```md
## Client (laptop)

export SPARKS_HOST=vlad@spark.local
sparks submit --data ./corpus --name e0 -- python train.py --data /data
sparks queue

## Server (box)

The queue container runs `fire` (image ENTRYPOINT). It pulls the job image,
mounts `--data` at `/data`, and supervises the run. You do not install or invoke
a separate wrap binary.
```

- [ ] **Step 2: INSTALL_CLAUDE table**

Replace the three-entry-point table with:

| Install | Binary | Role |
|---|---|---|
| Laptop venv | `sparks` | client |
| Queue image | `fire` | server |

Note: supervision is `python -m sparks.fire.supervise` inside the image, not on PATH as a product.

- [ ] **Step 3: sparkup comment**

```jinja
# ENTRYPOINT is fire; flags only — no leading subcommand.
```

- [ ] **Step 4: Grep clean**

```bash
rg -n 'sparks-run|sparks-runner|run_main|runner_main|sparks-worker' \
  /Users/whitemonk/projects/ai/sparks /Users/whitemonk/projects/ai/sparkup/roles/queue \
  --glob '!docs/superpowers/**'
```

Expected: only historical plan docs, if any.

- [ ] **Step 5: Commits**

sparks:
```bash
git commit -m "docs: client/server is sparks and fire"
```

sparkup (branch from main or small fix branch):
```bash
git commit -m "docs(queue): ENTRYPOINT is fire"
```

---

### Task 4: Smoke checklist (manual / optional on box)

- [ ] **Step 1:** `uv run which sparks fire` — both on PATH after sync; `which sparks-run` empty  
- [ ] **Step 2:** Rebuild/push queue image (GHCR) or `make deploy` equivalent so the box runs an image with `ENTRYPOINT ["fire"]`  
- [ ] **Step 3:** Converge sparkup queue role (or restart compose) so the runner container comes up; heartbeat present  
- [ ] **Step 4:** From laptop, `sparks submit --data …` still completes a smoke job  

If image publish is out of band, document VERIFY_LATER rather than blocking the merge of the Python restructure.

---

## Spec coverage (self-review)

| Decision | Task |
|---|---|
| `sparks.client.cli:main` | 1 |
| `sparks.fire.cli:main` / binary `fire` | 2 |
| No public wrap binary; supervise via `-m` | 2 |
| Engine no longer calls `sparks-run` | 2 |
| Docs + sparkup ENTRYPOINT comment | 3 |
| Shared libs stay at `sparks.*` | (no move — by design) |

## Placeholder scan

None intentional. Engine method name `supervise_argv` may be inlined into existing `argv` — implementer should match whatever `test_engine.py` already calls after the rename.

## Type consistency

- Package: `sparks.client.remote` (not `sparks.client.client`)
- Package: `sparks.fire.launch` (file `launch.py`, not `launcher.py`)
- Module for nesting: `sparks.fire.supervise`
- Console scripts: only `sparks`, `fire`
