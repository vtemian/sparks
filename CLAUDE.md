# CLAUDE.md

Standing rules for working in sparks. How to install it and run a job is
INSTALL_CLAUDE.md.

## Before every commit

- `make check` must pass: lint + typecheck + tests + dashboard allowlist.
- Work on a branch, never directly on main. Push after committing.
- Anything touching the emitter's threading, shutdown, or the launch/supervise
  seam also needs `make live` (real Prometheus) before merge. No unit test can
  catch a second writer.

## Code quality standards (mandatory, enforced by ruff)

The gate is real: in `src`, no function exceeds complexity 8, 8 branches, 4
returns or 30 statements. Do not noqa your way past it. But do not treat the
cap as a target either: it is deliberately wide enough that a linear function
which narrates itself fits whole. Splitting a function you could have read top
to bottom is the more common mistake here, and it has cost this repo real bugs
— see the helper-soup note below. Test modules are held to the same complexity
and branch limits; only the Makefile-invoked scripts in `tests/`
(`check_dashboard.py`, `on_box.py`) are exempt, and `tests/**` may run long in
statements because one arrange/act/assert is one thought.

### Narrate at runtime, not in the margin
Say what is happening in `LOG.debug`, present tense, one line per state
transition: "starting pid %d", "got SIGTERM; forwarding and waiting %ds",
"child reaped with returncode %d". A comment saying the same thing is invisible
to whoever is debugging a box at 2am, which is the only moment it is wanted.
A comment is only for what a log line cannot carry: an ordering that looks
reorderable, a value that must not be recomputed.

### Say a shared constraint once
State it once, at the site that enforces it, in one line. Re-deriving the same
argument everywhere is how the source reached 29% prose: one fact about
remote-write rollback had been written out nine times across six files.

### No nesting, and no helper soup
Flatten control flow with guard clauses and early returns. One level of
indentation inside a function body. Private helpers and nested `def`s need
at least three statements — a one- or two-liner is an inline, not a function.
`tests/check_short_funcs.py` enforces that (`make lint`). Class methods and
public module names may stay thin; they are the API.

A single-use private helper has to earn its frame: it should spare the reader
the body, not just move it. Prefer one function a reader follows top to bottom
over five they must assemble. This is not a style preference — splitting
`contain.main` to fit an older, tighter cap pushed its signal-handler state out
into module globals and left a test-only hook in production code, while the
same problem stayed correct and local in `launch` because that one was allowed
to remain whole.

### Names are contracts
A function does what its name says, all of it, and nothing else. `fetch_*`
returns or raises; `find_*` may return None; `*_argv` builds argv and never
runs it. No Manager/Service/Helper/Data names. A leading `_` means private to
a class: module-level and nested functions take plain names, because privacy is
modules and classes rather than punctuation (`tests/check_private_prefix.py`).
Locals are a noun or a verb, never a letter:
`parser` / `exc` / `handle`, not `p` / `e` / `f`; `submit_parser`, never
`submit_p`. Ruff has no VNE001 yet — `tests/check_names.py` is the rule
(`make lint`). pep8-naming (`N` in ruff) covers class/function shape.

### Fail fast, fail visible
Catch specific exceptions. The only broad excepts are the deliberate ones
(telemetry never kills a run; a bad job never stops the queue), and each carries
`# noqa: BLE001` and a comment saying why. Never add one without both. The
exception is a handler that re-raises or logs a traceback: ruff does not call
those blind, so a directive there fails the gate the other way: the queue guard
in `runner.py`, the record-failed handler in `launch.py`, the pump guard in
`emit.py` and the cleanup in `summary.py` correctly carry none.

### Write almost none
**There are no docstrings anywhere.** Not in `src`, not in `tests`, not on
modules, classes or functions. The only exceptions are the five modules whose
`__doc__` argparse prints as `--help` text, and those are one line.
`tests/check_no_docstrings.py` enforces it from `make lint`, so adding one
fails the build. The name and the signature are the documentation; if they are
not enough, fix them rather than writing prose about them.

`src` runs at 3.4% prose, all of it comments. That is the ceiling, not a floor
to grow toward. What earns a comment is one short line, on the statement it
guards, where violating it causes a bug someone would otherwise introduce. If
it does not meet that test, delete it. If it describes what happens at runtime,
it is a `LOG.debug` line instead.

No section banners (`# -- title ---`, `# ====`) and no `#` comments before the
first top-level `def`/`class` (`tests/check_banners.py`,
`tests/check_file_header.py`, both from `make lint`).

### Let it breathe
A block ending in `return` gets a blank line after it, so a guard clause is not
welded to the code it guards (`tests/check_blank_after_return.py`). Beyond that
rule, use blank lines freely between statements: a function is a few paragraphs,
not one. Vertical space is the cheapest thing in the file and the formatter
keeps whatever you write.

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
  the code actually produced.
- Never delete a failing test. Never relax `[tool.mypy] strict`.
- Error paths are behaviour: pin the message the user sees
  (`pytest.raises(match=...)`).

## Commits

Plain imperative sentences in the repo's voice; `fix:` prefix for bugfixes.
Small and frequent. Never `git add -A` without `git status` first.
