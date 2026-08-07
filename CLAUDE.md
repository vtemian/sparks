# CLAUDE.md

Standing rules for working in sparks. Repo facts and traps live in
INSTALL_CLAUDE.md; read that too, first.

## Before every commit

- `make check` must pass: lint + typecheck + tests + dashboard allowlist.
- Work on a branch, never directly on main. Push after committing.
- Anything touching the emitter's threading, shutdown, or the launch/supervise
  seam also needs `make live` (real Prometheus) before merge. No unit test can
  catch a second writer; INSTALL_CLAUDE.md explains why.

## Code quality standards (mandatory, enforced by ruff)

The gate is real: complexity 5, branches 4, returns 4, statements 15 per
function, at most. Do not noqa your way past it; decompose.

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
ones (telemetry never kills a run; a bad job never stops the queue); each
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
- Mock only true externals (the Docker SDK client, subprocess argv capture),
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
