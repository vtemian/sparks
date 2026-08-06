# Fire SSH-RPC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fire` the server control plane (verbs + daemon in one container); laptop `sparks` only SSHes a host `fire-ctl` wrapper and rsyncs `--data`.

**Architecture:** Dispatch inside `fire`: bare flags → daemon (compose unchanged); first token in `{queue,cancel,abort,retry,remove,reserve,commit,contract}` → control verb. Control helpers move out of `sparks.client` into `sparks.fire.control`. sparkup installs `/usr/local/bin/fire-ctl` → `docker compose exec -T runner fire …`. Client `ssh_argv` prefixes `fire-ctl` instead of `sparks`.

**Tech Stack:** Python 3.12, argparse, pytest, ssh/rsync, Docker Compose (sparkup queue role).

**Spec:** `docs/superpowers/specs/2026-08-06-fire-ssh-rpc-design.md`

## Global Constraints

- Compose must keep working as `fire --url … --shared-dir …` with **no** leading subcommand (sparkup `roles/queue/templates/compose.yml.j2`).
- `$shared_dir` bind-mounted at the **same absolute path** host↔container; `reserve` paths are rsyncable from the host.
- LAN trust: no new auth. `may_be_controlled_by` may stay; with one container uid it always allows.
- No HTTP control plane. No third console script. Supervision stays `python -m sparks.fire.supervise`.
- Laptop `sparks` must not register hidden server verbs after migration.
- Remote binary default: `fire-ctl` (override via `SPARKS_REMOTE`).

## File map

| Path | Responsibility |
|---|---|
| `src/sparks/fire/control.py` | **Create.** resolve/ask/retry/remove/render + queue_dir helper (moved from client) |
| `src/sparks/fire/ctl.py` | **Create.** argparse + handlers for control verbs |
| `src/sparks/fire/cli.py` | **Modify.** Dispatch: verb → ctl, else daemon |
| `src/sparks/client/remote.py` | **Modify.** `ssh_argv` → `fire-ctl`; verb names without `_`; drop or re-export moved helpers |
| `src/sparks/client/cli.py` | **Modify.** Client-only; call `queue`/`cancel`/… not `_queue` |
| `tests/test_fire_ctl.py` | **Create.** Control verb parsing + local `--shared-dir` behavior |
| `tests/test_fire_cli.py` | **Modify.** Daemon flags still work; unknown leading token still fails; verbs dispatch |
| `tests/test_cli.py` / `tests/test_client.py` | **Modify.** Expect `fire-ctl` + public verb names |
| `README.md` / `INSTALL_CLAUDE.md` | **Modify.** Document SSH → fire-ctl |
| sparkup `roles/queue/templates/fire-ctl.j2` | **Create** (sibling repo). Host wrapper |
| sparkup `roles/queue/tasks/main.yml` | **Modify.** Install fire-ctl |

---

### Task 1: Move control helpers to `sparks.fire.control`

**Files:**
- Create: `src/sparks/fire/control.py`
- Modify: `src/sparks/client/remote.py` (remove moved functions; keep `submit`/`submit_remote`/ssh/build)
- Modify: `tests/test_client.py` (import control helpers from `sparks.fire.control` where tests exercise ask/retry/remove/resolve/render)
- Test: existing `tests/test_client.py` control-plane tests must keep passing after import path change

**Interfaces:**
- Consumes: `spool.entries`, `spool.request`, `spool.remove`, `spool.reserve`, `spool.commit`, `Entry.may_be_controlled_by`
- Produces:
  - `resolve(queue_dir: Path, needle: str) -> spool.Entry`
  - `ask(queue_dir: Path, needle: str, action: str) -> spool.Entry`
  - `retry(queue_dir: Path, entry: spool.Entry) -> spool.Entry`
  - `remove(queue_dir: Path, needle: str) -> spool.Entry`
  - `render(entries: list[spool.Entry], now: float | None = None) -> str`
  - `queue_dir(shared_dir: Path | None = None) -> Path` — if `shared_dir` set use `{shared_dir}/queue`, else contract `queue_dir` (must exist)

- [ ] **Step 1: Write a failing import smoke test**

Create `tests/test_fire_control.py`:

```python
from pathlib import Path

from sparks.fire import control


def test_queue_dir_uses_shared_dir(tmp_path: Path) -> None:
    q = tmp_path / "queue"
    q.mkdir()
    assert control.queue_dir(tmp_path) == q
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fire_control.py::test_queue_dir_uses_shared_dir -v`

Expected: FAIL with `ImportError` / module missing

- [ ] **Step 3: Create `control.py` by moving code**

Move from `src/sparks/client/remote.py` into `src/sparks/fire/control.py`:
`resolve`, `ask`, `retry`, `remove`, `render`, `_age`, `HEADINGS`, and any private helpers they need.

Use a dedicated `ClientError` in `control.py` **or** import `ClientError` from `sparks.client.remote` — prefer a small shared error:

In `control.py`:

```python
class ControlError(Exception):
    """Something the caller can fix, reported without a traceback."""
```

Update moved functions to raise `ControlError` instead of `remote.ClientError`.

Add:

```python
def queue_dir(shared_dir: Path | None = None) -> Path:
    from sparks import box

    if shared_dir is not None:
        return Path(shared_dir) / "queue"
    contract = box.load()
    if contract is None:
        raise ControlError(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )
    qd = contract.queue_dir
    if not qd.is_dir():
        raise ControlError(
            f"this box is provisioned for sparks but not for the queue: "
            f"{qd} does not exist"
        )
    return qd
```

For `retry`: keep the same data hardlink/copy behavior currently in `remote.retry` (read that function and move it wholesale).

- [ ] **Step 4: Point `test_client.py` at `control` for ask/retry/remove/resolve/render**

Where tests call `client.ask` / `client.retry` / etc., change imports to `from sparks.fire import control` (or `from sparks.fire.control import ask, …`). Leave `client.submit`, `client.submit_remote`, `client.ssh_argv` on `remote`.

- [ ] **Step 5: Run control + client tests**

Run: `uv run pytest tests/test_fire_control.py tests/test_client.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/sparks/fire/control.py src/sparks/client/remote.py tests/test_fire_control.py tests/test_client.py
git commit -m "$(cat <<'EOF'
refactor: move queue control helpers into sparks.fire.control

EOF
)"
```

---

### Task 2: `fire` control verbs (`ctl.py`) + dispatch in `cli.py`

**Files:**
- Create: `src/sparks/fire/ctl.py`
- Modify: `src/sparks/fire/cli.py`
- Create: `tests/test_fire_ctl.py`
- Modify: `tests/test_fire_cli.py`

**Interfaces:**
- Consumes: `sparks.fire.control.*`, `spool.reserve`, `spool.commit`, `box.load`
- Produces:
  - `VERBS: frozenset[str]`
  - `ctl_main(argv: list[str]) -> int`
  - `cli.main` dispatches: if `argv and argv[0] in VERBS` → `ctl_main`, else daemon parser

- [ ] **Step 1: Write failing tests for verbs + daemon**

`tests/test_fire_ctl.py`:

```python
from pathlib import Path

import pytest

from sparks.fire import cli


def test_queue_lists_empty_shared_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["queue", "--shared-dir", str(tmp_path)]) == 0
    assert "empty" in capsys.readouterr().out


def test_reserve_prints_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "queue").mkdir()
    assert cli.main(["reserve", "--name", "e0", "--shared-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(tmp_path / "queue"))
```

In `tests/test_fire_cli.py`, keep existing daemon flag test. Update the “leading runner token” test to still fail. Add:

```python
def test_daemon_flags_still_parse_without_subcommand() -> None:
    from sparks.fire.cli import build_daemon_parser

    args = build_daemon_parser().parse_args(
        ["--shared-dir", "/srv/spark", "--poll-seconds", "2"]
    )
    assert args.shared_dir == Path("/srv/spark")
```

(Rename `build_parser` → `build_daemon_parser` in implementation, or keep `build_parser` as the daemon parser alias for this test.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fire_ctl.py tests/test_fire_cli.py -v`

Expected: FAIL (`queue` unknown / ImportError)

- [ ] **Step 3: Implement `ctl.py`**

```python
"""fire control verbs — the SSH-RPC surface inside the queue container."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sparks import box, spool
from sparks.fire import control

EX_CONFIG = 78

VERBS = frozenset(
    {"queue", "cancel", "abort", "retry", "remove", "reserve", "commit", "contract"}
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fire", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    q = sub.add_parser("queue", help="what is running and what is waiting")
    q.add_argument("--all", action="store_true")
    _shared(q)
    q.set_defaults(func=_cmd_queue)

    for verb, help_text in (
        ("cancel", "drop a job that has not started yet"),
        ("abort", "stop a job, whether it has started or not"),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("job")
        _shared(p)
        p.set_defaults(func=_ask(verb))

    for verb, help_text, func in (
        ("retry", "submit the same job again", _cmd_retry),
        ("remove", "delete a finished job", _cmd_remove),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("job")
        _shared(p)
        p.set_defaults(func=func)

    reserve = sub.add_parser("reserve", help=argparse.SUPPRESS)
    reserve.add_argument("--name", default="job")
    reserve.add_argument("--user", default="fire")
    _shared(reserve)
    reserve.set_defaults(func=_cmd_reserve)

    commit = sub.add_parser("commit", help=argparse.SUPPRESS)
    commit.add_argument("path", type=Path)
    commit.add_argument("--name", default="job")
    commit.add_argument("--user", default="fire")
    commit.add_argument("--git-sha", default="unknown")
    commit.add_argument("--git-dirty", action="store_true")
    commit.add_argument("--image", required=True)
    commit.add_argument("command", nargs="+")
    commit.set_defaults(func=_cmd_commit)

    contract = sub.add_parser("contract", help=argparse.SUPPRESS)
    contract.set_defaults(func=_cmd_contract)
    return parser


def _shared(p: argparse.ArgumentParser) -> None:
    p.add_argument("--shared-dir", type=Path, default=None)


def ctl_main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except control.ControlError as e:
        print(f"fire: {e}", file=sys.stderr)
        return 1
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"fire: {e}", file=sys.stderr)
        return EX_CONFIG


def _cmd_queue(args: argparse.Namespace) -> int:
    qd = control.queue_dir(args.shared_dir)
    entries = spool.entries(qd) if args.all else spool.publishable(qd)
    print(control.render(entries), end="")
    return 0


def _ask(action: str):
    def run(args: argparse.Namespace) -> int:
        entry = control.ask(control.queue_dir(args.shared_dir), args.job, action)
        print(f"asked the runner to {action} {entry.job.job_id}")
        return 0

    return run


def _cmd_retry(args: argparse.Namespace) -> int:
    qd = control.queue_dir(args.shared_dir)
    again = control.retry(qd, control.resolve(qd, args.job))
    print(again.job.job_id)
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    print(
        f"removed {control.remove(control.queue_dir(args.shared_dir), args.job).job.job_id}"
    )
    return 0


def _cmd_reserve(args: argparse.Namespace) -> int:
    _, path = spool.reserve(
        control.queue_dir(args.shared_dir), args.name, args.user
    )
    print(path)
    return 0


def _cmd_commit(args: argparse.Namespace) -> int:
    entry = spool.commit(
        args.path,
        spool.Job(
            job_id=args.path.name,
            name=args.name,
            user=args.user,
            command=args.command,
            submitted_unix=time.time(),
            git_sha=args.git_sha,
            git_dirty=args.git_dirty,
            image=args.image,
        ),
    )
    print(entry.job.job_id)
    return 0


def _cmd_contract(_args: argparse.Namespace) -> int:
    c = box.load()
    if c is None:
        raise box.NotProvisioned(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )
    print(f"shared_dir = {c.shared_dir}")
    print(f"shared_group = {c.shared_group}")
    print(f"textfile_dir = {c.textfile_dir}")
    print(f"prometheus_url = {c.prometheus_url}")
    print(f"grafana_url = {c.grafana_url}")
    print(f"registry_url = {c.registry_url}")
    return 0
```

- [ ] **Step 4: Dispatch in `cli.py`**

Replace `main` / rename daemon parser:

```python
from sparks.fire import ctl

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = list(argv if argv is not None else sys.argv[1:])
    if given and not given[0].startswith("-") and given[0] in ctl.VERBS:
        return ctl.ctl_main(given)
    args = build_parser().parse_args(given)
    try:
        return cmd_runner(args)
    except (box.NotProvisioned, box.Malformed) as e:
        print(f"fire: {e}", file=sys.stderr)
        return EX_CONFIG
```

Update module docstring: daemon when no verb; verbs are the SSH-RPC surface.

Keep `test_runner_rejects_a_leading_runner_token`: `runner` ∉ `VERBS` → falls through to daemon parser → SystemExit.

- [ ] **Step 5: Run fire tests**

Run: `uv run pytest tests/test_fire_ctl.py tests/test_fire_cli.py -q`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/sparks/fire/ctl.py src/sparks/fire/cli.py tests/test_fire_ctl.py tests/test_fire_cli.py
git commit -m "$(cat <<'EOF'
feat(fire): add SSH-RPC control verbs beside the daemon

EOF
)"
```

---

### Task 3: Point laptop client at `fire-ctl` + public verb names

**Files:**
- Modify: `src/sparks/client/remote.py`
- Modify: `src/sparks/client/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_client.py`

**Interfaces:**
- Consumes: host `fire-ctl` on PATH (sparkup Task 4)
- Produces:
  - `REMOTE_BIN_ENV = "SPARKS_REMOTE"`
  - `remote_bin() -> str` default `"fire-ctl"`
  - `ssh_argv(host, argv) -> ["ssh", host, shlex.join([remote_bin(), *argv])]`
  - Laptop verbs send `queue` / `cancel` / … (no `_` prefix)
  - `commit_argv` gains `--user` from `local_user()` for display provenance
  - Optional: `fetch_registry_url` via `remote_capture(host, ["contract"])` parsing `registry_url = …`

- [ ] **Step 1: Write failing tests for remote binary + verb names**

In `tests/test_client.py`:

```python
def test_ssh_argv_uses_fire_ctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARKS_REMOTE", raising=False)
    argv = client.ssh_argv("box", ["queue", "--all"])
    assert argv[2] == "fire-ctl queue --all"


def test_ssh_argv_honours_SPARKS_REMOTE(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKS_REMOTE", "fire")
    assert client.ssh_argv("box", ["queue"])[2] == "fire queue"
```

In `tests/test_cli.py`:

```python
def test_queue_sshes_fire_ctl_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.HOST_ENV, raising=False)
    with patch.object(client, "remote", return_value=0) as remote_fn:
        assert cli.main(["queue", "--host", "box", "--all"]) == 0
    remote_fn.assert_called_once_with("box", ["queue", "--all"])
```

Remove / rewrite tests that invoke local `["_queue", …]` / `contract` / `reserve` on the **client** CLI — those verbs no longer exist on `sparks`. Contract/reserve coverage lives in `tests/test_fire_ctl.py`.

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_client.py::TestReachingTheBox tests/test_cli.py -q`

Expected: FAIL on `sparks _queue` / `["_queue", …]`

- [ ] **Step 3: Implement client changes**

`remote.py`:

```python
REMOTE_BIN_ENV = "SPARKS_REMOTE"
DEFAULT_REMOTE_BIN = "fire-ctl"

def remote_bin() -> str:
    return os.environ.get(REMOTE_BIN_ENV) or DEFAULT_REMOTE_BIN

def ssh_argv(host: str, argv: list[str]) -> list[str]:
    return ["ssh", host, shlex.join([remote_bin(), *argv])]
```

Update module docstring accordingly.

`commit_argv`: add `"--user", local_user()` so job.json shows the laptop person (files still owned by container uid).

`cli.py`:
- Delete `_add_server_commands` and all `cmd_serve_*` / `cmd_reserve` / `cmd_commit` / `cmd_contract` / `_queue_dir`.
- Change remote argv to `["queue"]`, `["cancel", job]`, etc. (no underscore).
- Docstring: client SSHes `fire-ctl`; server is `fire`.

Optional cleanup: switch `fetch_registry_url` to parse `fire-ctl contract` stdout; if you do, update `test_fetch_registry_url_*`.

- [ ] **Step 4: Run client tests**

Run: `uv run pytest tests/test_cli.py tests/test_client.py tests/test_package_layout.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sparks/client/cli.py src/sparks/client/remote.py tests/test_cli.py tests/test_client.py
git commit -m "$(cat <<'EOF'
feat(client): SSH fire-ctl for queue control instead of sparks verbs

EOF
)"
```

---

### Task 4: sparkup `fire-ctl` wrapper

**Files (sibling repo `sparkup`):**
- Create: `roles/queue/templates/fire-ctl.j2`
- Modify: `roles/queue/tasks/main.yml`
- Modify: `roles/queue/defaults/main.yml` (add `queue_fire_ctl_path: /usr/local/bin/fire-ctl`)
- Modify: `roles/queue/README.md` (one short paragraph)

**Interfaces:**
- Produces: `/usr/local/bin/fire-ctl` → `docker compose -f {{ queue_dir }}/compose.yml -p {{ queue_project_name }} exec -T runner fire "$@"`

- [ ] **Step 1: Add template**

`roles/queue/templates/fire-ctl.j2`:

```bash
#!/bin/sh
# {{ ansible_managed }}
# SSH-RPC into the queue container's fire binary.
set -eu
exec docker compose \
  -f '{{ queue_dir }}/compose.yml' \
  -p '{{ queue_project_name }}' \
  exec -T runner \
  fire "$@"
```

- [ ] **Step 2: Install task** (after compose file is rendered, before or after Bring the queue up)

```yaml
- name: Install fire-ctl (SSH-RPC into the queue container)
  ansible.builtin.template:
    src: fire-ctl.j2
    dest: "{{ queue_fire_ctl_path }}"
    owner: root
    group: root
    mode: "0755"
```

Add default:

```yaml
queue_fire_ctl_path: /usr/local/bin/fire-ctl
```

- [ ] **Step 3: Document in queue README**

State: laptop `sparks` SSHes `fire-ctl <verb>`; wrapper `docker compose exec`s into service `runner`.

- [ ] **Step 4: Commit on sparkup**

```bash
git add roles/queue/templates/fire-ctl.j2 roles/queue/tasks/main.yml roles/queue/defaults/main.yml roles/queue/README.md
git commit -m "$(cat <<'EOF'
feat(queue): install fire-ctl for SSH-RPC into the fire container

EOF
)"
```

- [ ] **Step 5: Converge box (manual verification later)**

```bash
cd ~/projects/ai/sparkup && make apply BECOME="--become-password-file ~/.sparkup-become" EXTRA="--tags queue"
ssh "$SPARKS_HOST" 'fire-ctl contract'
ssh "$SPARKS_HOST" 'fire-ctl queue'
```

Expected: contract prints `registry_url`; queue prints empty or job table. **Requires a queue image that includes Task 2 `fire` verbs** — deploy sparks image before or after as needed.

---

### Task 5: Docs + full quality gate

**Files:**
- Modify: `README.md`
- Modify: `INSTALL_CLAUDE.md` (client/server paragraph: SSH → fire-ctl → fire)
- Modify: `docs/superpowers/specs/2026-08-06-client-fire-split-design.md` — add a one-line note that SSH-RPC into `fire` supersedes “hidden sparks verbs on the box” (do not rewrite the whole doc)

- [ ] **Step 1: Update README client section**

Replace any implication that ssh runs `sparks` on the box for queue verbs. Example:

```markdown
The laptop client SSHes `fire-ctl` on the box (installed by sparkup), which
`docker exec`s into the queue container and runs `fire <verb>`. Bulk `--data`
still uses rsync over SSH into the shared spool path.
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -q`

Expected: PASS (all green)

- [ ] **Step 3: Commit**

```bash
git add README.md INSTALL_CLAUDE.md docs/superpowers/specs/2026-08-06-client-fire-split-design.md
git commit -m "$(cat <<'EOF'
docs: describe SSH fire-ctl client→server path

EOF
)"
```

---

## Migration / deploy order

1. Land sparks Tasks 1–3 + 5; build/push new `fire` image (GHCR / whatever `make deploy` uses).
2. Land sparkup Task 4; converge `--tags queue`.
3. Restart queue so the new image is running **before** laptops expect `fire-ctl queue` (old image has no verbs → exec fails).
4. Smoke from laptop:

```bash
export SPARKS_HOST=vlad@spark.local
sparks queue
sparks submit --data ./corpus --name smoke -- python -c 'print(1)'
sparks queue
```

## Rollback

- Revert client `DEFAULT_REMOTE_BIN` to `sparks` and restore hidden verbs **or** set `SPARKS_REMOTE=sparks` only if old on-box `sparks` still has server verbs.
- Prefer forward fix: keep `fire-ctl` and roll queue image.

## Spec coverage check

| Spec item | Task |
|---|---|
| One container API + worker | Task 2 |
| SSH + docker exec via fire-ctl | Tasks 3–4 |
| rsync for `--data` | unchanged submit_remote (Task 3 only retargets reserve/commit binary) |
| LAN trust, no auth | Task 1–2 leave uid checks; always pass as container user |
| Same-path bind mount | documented; sparkup already mounts that way |
| Compose flags unchanged | Task 2 dispatch |
| Client drops hidden verbs | Task 3 |
| No HTTP | all tasks |

## Placeholder scan

None intentional. sparkup paths assume repo at sibling `../sparkup` as in prior work.
