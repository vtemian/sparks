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

The gate is real: in `src`, no function exceeds complexity 5, 4 branches, 4
returns or 15 statements. Do not noqa your way past it; decompose. Test modules
are held to the same complexity and branch limits; only the two Makefile-invoked
scripts in `tests/` (`check_dashboard.py`, `on_box.py`) are exempt, and
`tests/**` may run long in statements because one arrange/act/assert is one
thought.

### No nesting
Flatten control flow with guard clauses and early returns. One level of
indentation inside a function body.

### Names are contracts
A function does what its name says, all of it, and nothing else. `fetch_*`
returns or raises; `find_*` may return None; `*_argv` builds argv and never
runs it. No Manager/Service/Helper/Data names. Private helpers are `_` plus
one or two terse domain words. Spell the word: `submit_parser`, never
`submit_p`; `directory`, never `qd`. Letter-suffix abbreviations (`*_p`,
`*_f`, …) and the few banned short locals (`p`, `q`, `qd`) fail
`tests/test_naming.py`. pep8-naming (`N` in ruff) covers the rest.

### Fail fast, fail visible
Catch specific exceptions. The only broad excepts are the deliberate ones
(telemetry never kills a run; a bad job never stops the queue), and each carries
`# noqa: BLE001` and a comment saying why. Never add one without both. The
exception is a handler that re-raises or logs a traceback: ruff does not call
those blind, so a directive there fails the gate the other way. INSTALL_CLAUDE.md
names the four sites that correctly carry none.

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
