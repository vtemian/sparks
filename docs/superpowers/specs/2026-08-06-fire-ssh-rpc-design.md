# Fire SSH-RPC — design

**Status:** architecture approved in conversation (2026-08-06). Plan:
`docs/superpowers/plans/2026-08-06-fire-ssh-rpc.md`.

## Model

Classic client → server roles over SSH (not HTTP):

| Piece | Role |
|---|---|
| `sparks` (laptop) | Thin client: local Docker build/push, then SSH + rsync |
| `fire` (one container) | Server: control verbs **and** queue worker |
| Spool on disk | Private to `fire`; clients never mutate it directly |

```text
laptop sparks
  ├─ docker build/push  →  box registry
  ├─ ssh host fire-ctl <verb> …  →  docker exec → fire <verb>
  └─ rsync over ssh     →  host bind-mount of $shared_dir/queue/…
```

## Decisions

- **One container** serves control verbs and runs the queue loop.
- **Transport:** SSH + `docker exec` into `fire`. No HTTP control plane.
- **Bulk `--data`:** rsync over SSH (HTTP upload rejected as too slow).
- **Auth:** LAN + ability to SSH to the box. No tokens, no per-user authz.
  `job.user` remains display/provenance from the laptop.
- **Host wrapper `fire-ctl`:** installed by sparkup so the laptop does not
  hardcode compose project / container names.
- **Paths:** `$shared_dir` is bind-mounted at the same absolute path in the
  container as on the host (already true in sparkup). `reserve` prints a path
  that rsync on the host can use.

## CLI shape

- `fire --url … --shared-dir …` (no subcommand) → daemon (compose ENTRYPOINT
  unchanged).
- `fire <verb> …` where verb is one of `queue`, `cancel`, `abort`, `retry`,
  `remove`, `reserve`, `commit`, `contract` → control plane inside the
  container.
- Laptop `sparks` drops all hidden server verbs; it only SSHes `fire-ctl`.

## Non-goals

- HTTP/gRPC API
- Per-user authorization inside `fire`
- Changing registry push/pull or job image/`--data` mount semantics
- In-process supervision (stays `python -m sparks.fire.supervise`)
