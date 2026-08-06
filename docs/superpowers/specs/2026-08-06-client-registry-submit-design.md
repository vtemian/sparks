# Client + registry submit — design

**Status:** approved in conversation (Approach A). Implementation plan: `docs/superpowers/plans/2026-08-06-client-registry-submit.md`.

## Intent

`sparks submit` from a laptop packages a job (Docker image + one data folder), registers it on the box queue, and the runner executes it. Cancel/list/remove stay available. No `sparks run` on the laptop.

## Decisions

| Decision | Choice |
|---|---|
| Where images build | Always on the laptop |
| Where images live | Registry on the box (sparkup provisions) |
| Data | One folder via `--data`; uploaded with submit; not baked into the image |
| Data mount in container | `/data` (fixed) |
| Client vs runner | Split install/entrypoints: laptop = client; queue container = runner |
| `sparks run` | Box-only, invoked by the runner around the training container — not a laptop command |
| Build-on-box / `context/` ship | Removed as the normal path |
| Heavy assets (corpus, base weights) | Travel via `--data` or already on `$shared_dir`; never via image layers |

## Flow

```text
Laptop                                      Box
------                                      ---
sparks submit --data ./corpus -- cmd
  docker build
  docker push ──────────────────────────► registry (from box.toml)
  ssh reserve ──────────────────────────► job dir (invisible)
  rsync data/ ──────────────────────────► job/data/
  ssh commit (image + command) ─────────► job.json visible
                                        runner: pull image, mount
                                          shared_dir + job/data→/data,
                                          sparks-run -- docker run …
```

## Out of scope for this change

- Named reusable datasets (`sparks data push`)
- TLS for the registry beyond whatever sparkup chooses (document insecure-registry for LAN)
- Replacing ssh with an HTTP API
- Multi-node / multi-GPU scheduling
