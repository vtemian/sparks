# Contributing

`make check` is the gate, and CI runs that identical command, so a green `make check` is a
green pull request. A clean pre-commit hook is not: it runs ruff and nothing else.

## Running the checks

```sh
uv sync
make check   # ruff, the house-rule checkers, mypy strict, pytest -m "not live", dashboards
make live    # the same emitter against a real Prometheus in Docker
```

`make check` needs no hardware and no Docker. `make live` needs Docker and **CI never runs
it**, so nothing but you will catch a regression in `src/sparks/emit.py`, `buffer.py`,
`fire/launch.py` or `fire/process.py`: the emitter's threading and the launch/supervise seam.
Run it before you push a change to any of them.

`make on-box` runs the acceptance checks that only mean something on a real Spark, and CI
never runs that either.

## The rules that actually matter

- **`uv run` re-locks silently.** After touching `pyproject.toml`, run `uv sync --locked` and
  commit `uv.lock`. Otherwise `make check` passes here against a lockfile CI refuses, and the
  queue image fails its build the same way. This has already broken CI once.
- Mock only true externals: the Docker SDK, captured subprocess argv. Never mock our own code.
- Never delete a failing test. Never relax `[tool.mypy] strict`.
- Never query a metric nobody emits. The vocabulary is closed in `sparks.metrics.METRICS`, and
  a panel naming anything else fails `make dashboard` rather than drawing an empty graph on
  somebody's box.
- A broad `except Exception` carries both `# noqa: BLE001 -- reason` and the reason. A handler
  that re-raises or logs a traceback correctly carries neither, and `RUF100` fails a directive
  that was not needed.

## Style

Ruff and the eight checkers in `gates/check_*.py` enforce it from `make lint`, so read the
failure rather than a list. The two that catch first-timers:

- **No docstrings, anywhere.** Not in `src`, not in `tests`, not in `examples`. Every file
  starts with its imports, and `--help` text is a string passed to
  `ArgumentParser(description=...)`.
- Complexity 8, 8 branches, 4 returns, 30 statements, and those caps are wide on purpose.
  Splitting a function a reader could have followed top to bottom is the more common mistake
  here, and it has cost this repo real bugs. Do not `noqa` past a cap; do not aim at one either.

The checkers' roots differ, so a rule that fires in one tree may not exist in another.
`check_no_docstrings` scans `src`, `tests`, `examples` and `gates`, `check_banners` scans
`src`, `tests` and `gates`, `check_unused` reports only `src`, and the other five scan
`src/sparks` alone.

[CLAUDE.md](CLAUDE.md) is the long form, with the reasoning behind each of these.

## Pull requests

- Branch from `main`, never commit to it. One concern per pull request.
- Commit messages are plain imperative sentences in the repo's voice, `fix:` prefixed for a
  bugfix. No conventional-commit types, no scopes.
- Explain the failure mode you are preventing, not just the change.
- Say whether you ran `make live` or `make on-box`, and paste what they said.

## Reporting a bug

Include what you ran, what happened, and whether it was on a laptop or on the box. For a run
that went wrong, `sparks status <job> --json` and the job's `summary.json` say more than a
description. [INSTALL_CLAUDE.md](INSTALL_CLAUDE.md) lists the failures that already have a
known cause; check there first.
