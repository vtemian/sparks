# Client + registry submit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Laptop `sparks` builds and pushes a training image to the box registry, uploads one `--data` folder, and enqueues a job; the queue runner only pulls and runs — no build-on-box, no `sparks run` on the laptop.

**Architecture:** Split entrypoints (`sparks` client vs `sparks-runner` + `sparks-run` on the box). sparkup adds a local registry and `registry_url` to `/etc/sparks/box.toml`. Submit becomes build→push→reserve→rsync data→commit(image). Runner requires `job.image`, mounts `job/data` at `/data`, and drops `engine.build` from the happy path.

**Tech Stack:** Python 3.12, Docker CLI (laptop + box), rsync/ssh (client), Ansible/sparkup (registry), existing spool (`job.json` last).

**Design spec:** `docs/superpowers/specs/2026-08-06-client-registry-submit-design.md`

## Global Constraints

- Client never calls `launcher.launch` or talks to the box Docker socket.
- Training images are built only on the laptop; the box registry is the source of truth for pulls.
- Job data is one directory uploaded to `$queue/$job_id/data/` and mounted read-only at `/data` in the training container.
- `job.image` is required on every new job; shipping `context/` for `docker build` on the box is deleted.
- Laptop CLI surface is only: `submit`, `queue`, `cancel`, `abort`, `remove` (keep `retry` if cheap; do not reintroduce `demo` or laptop `run`).
- Cross-repo: sparkup changes are a hard prerequisite for an end-to-end box test; sparks unit tests may fake `registry_url`.
- Prefer TDD: failing test → minimal implementation → pass → commit per task.
- Do not commit secrets; do not force-push `main`.

## File map

| Path | Responsibility after this work |
|---|---|
| `../sparkup/roles/registry/` (new) | Run `registry:2`, LAN-reachable |
| `../sparkup/roles/sparks/templates/box.toml.j2` | Add `registry_url` |
| `../sparkup/roles/docker/` | Allow box daemon to pull from local registry; document laptop `insecure-registries` |
| `src/sparks/box.py` | Parse `registry_url` |
| `src/sparks/spool.py` | `Job.image: str` required; `DATA_DIR = "data"` |
| `src/sparks/engine.py` | Mount job data at `/data`; optional explicit `docker pull`; delete or gut `build()` |
| `src/sparks/runner.py` | Fail jobs with no image; no `_build` happy path |
| `src/sparks/client.py` | `build_and_push`, `submit` = reserve+data+commit; drop context ship as default |
| `src/sparks/cli.py` | Client-only verbs; require host |
| `src/sparks/runner_cli.py` (new) | `sparks-runner` entry |
| `src/sparks/run_cli.py` (new) or slim `cli_run.py` | `sparks-run` for nesting inside the queue container |
| `pyproject.toml` | Three scripts: `sparks`, `sparks-runner`, `sparks-run` |
| `Dockerfile` | `ENTRYPOINT ["sparks-runner"]` |
| `README.md`, `INSTALL_CLAUDE.md` | New mental model |
| Tests under `tests/test_*.py` | Updated for image+data path |

---

### Task 1: sparkup — local registry + `registry_url` in box.toml

**Repos:** `/Users/whitemonk/projects/ai/sparkup` (sibling of sparks).

**Files:**
- Create: `roles/registry/tasks/main.yml`, `roles/registry/defaults/main.yml`, `roles/registry/templates/compose.yml.j2` (or extend an existing compose stack)
- Modify: `roles/sparks/templates/box.toml.j2`
- Modify: `roles/sparks/defaults/main.yml` — default `sparks_registry_url`
- Modify: `roles/docker/tasks/main.yml` or `daemon.json` template — insecure registry / mirror for `{{ sparks_registry_host }}:{{ sparks_registry_port }}`
- Modify: `site.yml` / sparks role deps so registry is applied before/with sparks
- Modify: `roles/sparks/README.md` — document laptop `insecure-registries` for the same host:port

**Interfaces:**
- Produces: `/etc/sparks/box.toml` contains `registry_url = "http://spark.local:5000"` (exact default from sparkup vars)
- Produces: registry listening and accepting `docker push` from a LAN client after insecure-registry config

- [ ] **Step 1: Add role defaults**

```yaml
# roles/registry/defaults/main.yml
registry_image: registry:2
registry_port: 5000
registry_host: "{{ inventory_hostname }}"  # or spark.local from group_vars
# Published URL that laptops and the box daemon both use:
sparks_registry_url: "http://{{ registry_host }}:{{ registry_port }}"
```

- [ ] **Step 2: Compose service for registry**

Bind `{{ registry_port }}:5000`, persist `/var/lib/registry`, restart unless-stopped. No auth for v1 LAN (document the trust boundary: same people who already have ssh to the box).

- [ ] **Step 3: Wire `registry_url` into box.toml.j2**

```jinja
shared_dir     = "{{ spark_shared_dir }}"
shared_group   = "{{ spark_shared_group }}"
textfile_dir   = "{{ sparks_textfile_dir }}"
prometheus_url = "{{ sparks_prometheus_url }}"
grafana_url    = "{{ sparks_grafana_url | trim }}"
registry_url   = "{{ sparks_registry_url | trim }}"
```

- [ ] **Step 4: Configure Docker on the box to pull from that registry**

If HTTP: add to daemon.json `insecure-registries: ["spark.local:5000"]` (use the same host string clients will push to). Restart docker only when the file changes.

- [ ] **Step 5: Document laptop daemon.json**

In `roles/sparks/README.md`:

```text
On each laptop that submits, Docker must allow the insecure registry, e.g.:
  { "insecure-registries": ["spark.local:5000"] }
then restart Docker Desktop / dockerd.
```

- [ ] **Step 6: Converge a dev box and smoke-test**

```bash
cd /Users/whitemonk/projects/ai/sparkup && make apply   # or project-equivalent
ssh spark.local 'cat /etc/sparks/box.toml | grep registry_url'
docker pull alpine:latest
docker tag alpine:latest spark.local:5000/sparks-smoke:test
docker push spark.local:5000/sparks-smoke:test
ssh spark.local 'docker pull spark.local:5000/sparks-smoke:test'
```

Expected: push and pull both succeed.

- [ ] **Step 7: Commit in sparkup**

```bash
git add roles/registry roles/sparks roles/docker site.yml
git commit -m "$(cat <<'EOF'
feat: local image registry and registry_url on the sparks contract

Training jobs build on laptops and push here; the queue only pulls.
EOF
)"
```

---

### Task 2: sparks — `Box.registry_url`

**Files:**
- Modify: `src/sparks/box.py`
- Modify: `tests/test_box.py`

**Interfaces:**
- Consumes: TOML field `registry_url: str`
- Produces: `Box.registry_url: str` (required like `prometheus_url`)

- [ ] **Step 1: Write the failing test**

In `tests/test_box.py`, extend `CONTRACT` and assert:

```python
def test_registry_url_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "box.toml"
    path.write_text(
        'shared_dir = "/srv/spark"\nshared_group = "spark"\n'
        'textfile_dir = "/var/lib/node_exporter/textfile"\n'
        'prometheus_url = "http://127.0.0.1:9090"\n'
        'grafana_url = "http://spark.local"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(path))
    with pytest.raises(box.Malformed, match="registry_url"):
        box.load()


def test_registry_url_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "box.toml"
    path.write_text(
        'shared_dir = "/srv/spark"\nshared_group = "spark"\n'
        'textfile_dir = "/var/lib/node_exporter/textfile"\n'
        'prometheus_url = "http://127.0.0.1:9090"\n'
        'grafana_url = "http://spark.local"\n'
        'registry_url = "http://spark.local:5000"\n'
    )
    monkeypatch.setenv("SPARKS_BOX_CONFIG", str(path))
    assert box.load().registry_url == "http://spark.local:5000"
```

Update every other fixture contract string in the suite to include `registry_url` (grep `grafana_url` and add the sibling line).

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/whitemonk/projects/ai/sparks
uv run pytest tests/test_box.py::test_registry_url_is_required -v
```

Expected: FAIL (field ignored or load succeeds without it).

- [ ] **Step 3: Implement**

In `box.py`:
- Add `"registry_url"` to `STRINGS`
- Add `registry_url: str` on `Box`
- Thread through `from_dict` / constructor

- [ ] **Step 4: Fix all contract fixtures; run box + cli tests**

```bash
rg -l 'grafana_url' tests src | xargs -I{} echo {}
uv run pytest tests/test_box.py tests/test_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sparks/box.py tests
git commit -m "feat: require registry_url on the box contract"
```

---

### Task 3: Job model — required image + `data/` directory

**Files:**
- Modify: `src/sparks/spool.py`
- Modify: `tests/test_spool.py`, `tests/test_client.py`, `tests/test_runner.py` as needed

**Interfaces:**
- Produces: `spool.DATA_DIR = "data"`
- Produces: `Job.image: str` (required; not `str | None`)
- Produces: `Entry.data_dir -> path / DATA_DIR`

- [ ] **Step 1: Failing tests**

```python
def test_job_requires_an_image() -> None:
    with pytest.raises(TypeError):
        spool.Job(
            job_id="job-1",
            name="n",
            user="u",
            command=["true"],
            submitted_unix=1.0,
            # image omitted
        )


def test_data_dir_is_beside_the_manifest(tmp_path: Path) -> None:
    _, path = spool.reserve(tmp_path, "n", "u")
    entry = spool.commit(
        path,
        spool.Job(
            job_id=path.name,
            name="n",
            user="u",
            command=["true"],
            submitted_unix=1.0,
            image="spark.local:5000/demo:1",
        ),
    )
    assert entry.data_dir == path / "data"
```

- [ ] **Step 2: Run to verify fail**

```bash
uv run pytest tests/test_spool.py::test_job_requires_an_image -v
```

- [ ] **Step 3: Implement**

```python
DATA_DIR = "data"

@dataclass(frozen=True)
class Job:
    ...
    image: str  # required — registry ref the runner will pull
    # remove "Absent for the normal path, where the box builds…" docstring

# Entry:
@property
def data_dir(self) -> Path:
    return self.path / DATA_DIR
```

Update `from_dict` to require `image` (KeyError/Malformed-style ClientError at commit sites is fine). Update all test `Job(...)` / `job.json` fixtures to pass an image string.

- [ ] **Step 4: Full spool/client/runner unit tests**

```bash
uv run pytest tests/test_spool.py tests/test_client.py tests/test_runner.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/sparks/spool.py tests
git commit -m "feat: jobs always name a registry image and a data dir"
```

---

### Task 4: Engine — mount `/data`, pull-only (no box build)

**Files:**
- Modify: `src/sparks/engine.py`
- Modify: `tests/test_engine.py`
- Modify: `src/sparks/runner.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: `entry.data_dir`, `entry.job.image`
- Produces: `docker run … --volume {data_dir}:/data:ro -e SPARKS_DATA=/data`
- Deletes happy path: `Docker.build` / `Runner._build` (keep `BuildFailed` only if still referenced — otherwise remove)

- [ ] **Step 1: Failing engine test**

```python
def test_job_data_is_mounted_read_only_at_slash_data(tmp_path: Path) -> None:
    entry = make_entry(tmp_path, image="spark.local:5000/t:1")  # helper
    (entry.data_dir).mkdir(parents=True)
    argv = engine.Docker(shared_dir=tmp_path, url="").container_argv(
        entry, "spark.local:5000/t:1", tmp_path / "cid"
    )
    assert f"{entry.data_dir}:/data:ro" in argv
    assert "SPARKS_DATA=/data" in " ".join(argv) or (
        "--env", "SPARKS_DATA=/data"
    ) in list(zip(argv, argv[1:]))
```

Adapt to the exact `container_argv` / `start` signature in the file.

- [ ] **Step 2: Run fail, then add the volume + env in `container_argv`**

```python
"--volume", f"{entry.data_dir}:/data:ro",
"--env", "SPARKS_DATA=/data",
```

If `data_dir` is missing at start time, fail the job with a clear detail (runner checks before start).

- [ ] **Step 3: Runner — image required, no build**

```python
def process(self, entry: spool.Entry) -> None:
    if not entry.job.image:
        spool.advance(
            entry.path,
            state=spool.FAILED,
            finished_unix=self.now(),
            detail="job has no image; rebuild and submit from a laptop",
        )
        ...
        return
    if not entry.data_dir.is_dir():
        spool.advance(..., detail="job data/ directory is missing")
        return
    self._run(entry, entry.job.image)
```

Remove `_build` and tests that expect Dockerfile build on the box. Replace with: prebuilt image runs; missing image fails cleanly.

Optional: `Docker.pull(image)` before run so pull errors become `failed` with detail rather than a confusing `docker run` error — recommended.

- [ ] **Step 4: Tests**

```bash
uv run pytest tests/test_engine.py tests/test_runner.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/sparks/engine.py src/sparks/runner.py tests
git commit -m "feat: runner pulls images and mounts job data at /data"
```

---

### Task 5: Client — build, push, upload data, commit image

**Files:**
- Modify: `src/sparks/client.py`
- Modify: `tests/test_client.py`
- Modify: `src/sparks/cli.py` (submit flags only; full CLI split is Task 6)

**Interfaces:**
- Produces:
  - `tag_for(registry_url: str, user: str, name: str, ref: str) -> str`
  - `build(context: Path, tag: str) -> None`
  - `push(tag: str) -> None`
  - `submit_remote(host, *, name, command, context, data: Path, image: str | None) -> str`
- `submit_remote` steps: resolve tag → build/push (unless `--image`) → `reserve` → rsync `data/` → `commit` with image (no `context/` rsync)

- [ ] **Step 1: Pure helper tests**

```python
def test_tag_for_uses_registry_user_and_name() -> None:
    assert (
        client.tag_for("http://spark.local:5000", "vlad", "exp", "abc1234")
        == "spark.local:5000/vlad/exp:abc1234"
    )
```

Strip scheme from registry_url when forming a docker tag host.

- [ ] **Step 2: `commit_argv` always passes `--image`**

```python
def test_commit_argv_requires_image() -> None:
    argv = client.commit_argv(
        "/srv/spark/queue/job-1",
        name="n",
        command=["python", "train.py"],
        sha="deadbeef",
        dirty=False,
        image="spark.local:5000/vlad/n:deadbeef",
    )
    assert "--image" in argv
    assert "spark.local:5000/vlad/n:deadbeef" in argv
```

- [ ] **Step 3: Implement build/push wrappers**

```python
def build(context: Path, tag: str) -> None:
    if not (context / "Dockerfile").is_file():
        raise ClientError(f"{context}/Dockerfile is missing")
    done = subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=False,
    )
    if done.returncode != 0:
        raise ClientError(f"docker build failed for {tag}")


def push(tag: str) -> None:
    done = subprocess.run(["docker", "push", tag], check=False)
    if done.returncode != 0:
        raise ClientError(
            f"docker push failed for {tag}. Is the registry in "
            f"insecure-registries and is SPARKS_HOST reachable?"
        )
```

- [ ] **Step 4: Rewrite `submit_remote`**

```python
def submit_remote(
    host: str,
    *,
    name: str,
    command: list[str],
    context: Path,
    data: Path,
    image: str | None = None,
    registry_url: str,
) -> str:
    if not data.is_dir():
        raise ClientError(f"--data {data} is not a directory")
    who = local_user()
    sha, dirty = provenance(context)
    tag = image or tag_for(registry_url, who, name, sha if sha != "unknown" else "latest")
    if image is None:
        build(context, tag)
        push(tag)
    reserved = remote_capture(host, ["reserve", "--name", name])
    # rsync data into reserved/data/
    dest = f"{host}:{reserved}/{spool.DATA_DIR}/"
    done = subprocess.run(rsync_argv(data, dest), ...)
    ...
    return remote_capture(
        host, commit_argv(reserved, name, command, sha, dirty, tag)
    )
```

Stop rsyncing project `context/` for remote submit.

- [ ] **Step 5: CLI `submit` requires `--data` and `--host` / `SPARKS_HOST`**

```python
submit.add_argument("--data", type=Path, required=True, help="folder mounted at /data in the job")
submit.add_argument("--context", type=Path, default=Path.cwd(), help="Docker build context")
submit.add_argument("--image", help="skip build/push; use this registry tag")
```

Fetch `registry_url` via `remote_capture(host, ["contract"])` (Task 6 adds hidden `contract` that prints box.toml JSON or `registry_url=…`) **or** parse `ssh host cat /etc/sparks/box.toml` in client for this task.

Minimal for this task — add `client.fetch_registry_url(host) -> str` using ssh+tomllib on the remote file contents.

- [ ] **Step 6: Tests with mocks for docker/ssh**

Prefer testing argv construction and sequencing with monkeypatched `subprocess.run` / `remote_capture`, not real Docker.

- [ ] **Step 7: Commit**

```bash
git add src/sparks/client.py src/sparks/cli.py tests/test_client.py
git commit -m "feat: submit builds, pushes, and ships --data to the box"
```

---

### Task 6: Split entrypoints — client / runner / run

**Files:**
- Create: `src/sparks/runner_main.py` (or `runner_cli.py`) — parse only `runner` flags, call existing runner serve path
- Create: `src/sparks/run_main.py` — move current `cmd_run` / `_settings` / deep_link helpers used by nesting
- Modify: `src/sparks/cli.py` — client only: `submit`, `queue`, `cancel`, `abort`, `retry`, `remove` + hidden `reserve`/`commit`/`contract`
- Modify: `pyproject.toml` scripts
- Modify: `Dockerfile` ENTRYPOINT/CMD
- Modify: sparkup `roles/queue/templates/compose.yml.j2` command if it still says `sparks … runner`

**Interfaces:**
- Produces scripts:
  - `sparks = "sparks.cli:main"`
  - `sparks-runner = "sparks.runner_main:main"`
  - `sparks-run = "sparks.run_main:main"`
- Engine nesting must call `sparks-run`, not `sparks run`

- [ ] **Step 1: Failing test — client parser has no `run`**

```python
def test_client_cli_has_no_run() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--", "true"])
```

- [ ] **Step 2: Split modules**

- `cli.py`: client verbs only; **require** host via `host_from` (error if missing):  
  `"set SPARKS_HOST or pass --host; the client always talks to the box"`
- `run_main.py`: `sparks-run` wrapping `launcher.launch` (today’s `cmd_run`)
- `runner_main.py`: today’s `cmd_runner` body
- Hidden on client (still needed over ssh): `reserve`, `commit`, `contract`

- [ ] **Step 3: Engine argv uses `sparks-run`**

In `engine.py` where the outer process is constructed, replace `sparks` + `run` with `sparks-run` (single binary, flags unchanged aside from dropping the `run` token).

- [ ] **Step 4: Dockerfile**

```dockerfile
ENTRYPOINT ["sparks-runner"]
CMD []
```

Update sparkup compose `command:` to drop the leading `runner` subcommand if the entrypoint is already the daemon (compose currently passes `runner` as a sparks subcommand — align so the container still gets `--shared-dir` etc.).

- [ ] **Step 5: `make check`**

```bash
uv run ruff check src tests
uv run mypy
uv run pytest -m "not live" -q
uv run python tests/check_dashboard.py
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml Dockerfile src/sparks tests
git commit -m "refactor: split sparks client, runner, and run entrypoints"
```

---

### Task 7: Docs + delete dead paths

**Files:**
- Modify: `README.md` — laptop workflow is submit/queue/cancel; data at `/data`
- Modify: `INSTALL_CLAUDE.md` — registry, client/runner split, no build-on-box, no demo
- Modify: `../sparkup/roles/queue/templates/compose.yml.j2` comment (socket still needed for pull/run, not build)
- Delete unused: context-ship helpers only used by old submit; `engine.build` if still present; obsolete tests

- [ ] **Step 1: Rewrite README “queue” section**

```sh
export SPARKS_HOST=spark.local
sparks submit --data ./corpus --name e0 -- python train.py --data /data
sparks queue
sparks cancel <job>
```

- [ ] **Step 2: INSTALL_CLAUDE traps**

Add:
- Laptop Docker needs `insecure-registries` for `registry_url`
- `--data` is required; training code should read `/data` or `$SPARKS_DATA`
- Box no longer builds Dockerfiles from `context/`

- [ ] **Step 3: Grep for leftovers**

```bash
rg -n 'context/|docker build|cmd_run|submit_remote|BuildFailed|sparks demo' src tests README.md INSTALL_CLAUDE.md
```

Remove or update every hit that describes the old model.

- [ ] **Step 4: Commit**

```bash
git add README.md INSTALL_CLAUDE.md src tests
git commit -m "docs: client submits images and data; runner only pulls"
```

---

### Task 8: End-to-end acceptance on the box

**Files:** none required (manual / `tests/on_box.py` extension optional).

- [ ] **Step 1: Tiny training image**

Dockerfile that `FROM python:3.12-slim`, `CMD` cats `/data/hello.txt`.

- [ ] **Step 2: Submit from laptop**

```bash
echo hi > /tmp/corpus/hello.txt
export SPARKS_HOST=you@spark.local
sparks submit --data /tmp/corpus --name smoke -- python -c 'print(open("/data/hello.txt").read())'
sparks queue
```

Expected: job finishes; output shows `hi`; Grafana/run index still works if metrics enabled.

- [ ] **Step 3: Cancel path**

Submit a `sleep 3600` job; `sparks cancel` or `abort`; confirm terminal state.

- [ ] **Step 4: Record results in INSTALL_CLAUDE under a short “Verified on box” note if anything surprising appears.

- [ ] **Step 5: Commit only if on_box harness or docs changed.**

---

## Spec coverage (self-review)

| Spec item | Task |
|---|---|
| Build on laptop, push to box registry | 1, 5 |
| `registry_url` on contract | 1, 2 |
| `--data` one folder with submit | 3, 5 |
| Mount at `/data` | 4 |
| Client vs runner split; no laptop `run` | 6 |
| Runner pull-only; no box build | 4, 7 |
| Cancel / queue / remove plumbing | 6 (keep verbs), 8 |
| sparkup owns registry | 1 |

## Placeholder scan

No TBD steps; sparkup host/port defaults are concrete (`spark.local:5000`) and may be overridden in group_vars.

## Type consistency

- `Job.image: str` (required) everywhere after Task 3
- `spool.DATA_DIR == "data"`; container path `/data`; env `SPARKS_DATA`
- Scripts: `sparks` / `sparks-runner` / `sparks-run`
