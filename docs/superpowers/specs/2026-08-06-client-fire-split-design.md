# Client / Fire — design

**Status:** approved in conversation. Plan: `docs/superpowers/plans/2026-08-06-client-fire-split.md`.

## Model

Client / server only:

| Binary | Package | Role |
|---|---|---|
| `sparks` | `sparks.client` | Laptop: submit, queue, cancel, … |
| `fire` | `sparks.fire` | Box: queue daemon |

Job supervision (energy, metrics, summary around `docker run`) is **private** to the server: `python -m sparks.fire.supervise`, not a third console script. It exists so measurement stays outside the training image without putting sparks in every Dockerfile.

## Non-goals

- Renaming the PyPI/distribution name away from `sparks`
- HTTP API replacing ssh for the client
- In-process supervision inside the daemon (child via `-m` keeps crash isolation)
