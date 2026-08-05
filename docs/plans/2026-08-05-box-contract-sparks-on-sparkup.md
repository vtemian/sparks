# The box contract: sparks runs on a box sparkup provisioned

Status: **implemented and verified on the box, 2026-08-05.** The one deviation is
section 7: the `/srv/bbm` rename was folded in rather than deferred, because the
box turned out to be idle with nothing holding the tree open. See the note there.
Repos: `sparks` (this one) and `sparkup`
Supersedes: nothing. First plan that writes down the layering.

## The rule this encodes

Three layers, and until now only two of them were written down anywhere:

- **sparkup** provisions the box. It owns the shared tree, the shared group,
  the node_exporter textfile directory, Prometheus, Grafana, and — after this
  plan — sparks' alert rules and the file that tells sparks what was
  provisioned. If it is on the box and survives a reboot, sparkup put it there.
- **sparks** is a framework people write training jobs against. It reads what
  the box provides. It never invents a location, never creates infrastructure,
  and never quietly writes somewhere that is not scraped.
- **jobs** (bbm, and whatever comes next) import sparks. They know nothing
  about the box.

The failure this fixes: sparks currently assumes provisioning it cannot see,
and when the assumption is wrong it degrades silently instead of complaining.
`launcher.textfile_dir()` falls back to `<shared_dir>/index` when
`/var/lib/node_exporter/textfile` is missing — a directory nothing scrapes. The
run looks fine, the summary is on disk, and the box's run index silently stops
growing. `--shared-dir` defaults to `/srv/spark`, which is the *repo's* default
and not this box's (`host_vars/spark.yml` sets `/srv/bbm`), so a user who omits
the flag records runs into a directory nobody reads.

## The contract: `/etc/sparks/box.toml`

sparkup writes one file describing what it provisioned. sparks reads it and
refuses to guess when it is absent.

```toml
# Managed by Ansible: roles/sparks/templates/box.toml.j2
shared_dir     = "/srv/spark"
shared_group   = "spark"
textfile_dir   = "/var/lib/node_exporter/textfile"
prometheus_url = "http://127.0.0.1:9090"
grafana_url    = "http://spark.local"
```

Why a file and not probing for the directories:

- Probing cannot distinguish "this box was never set up for sparks" from
  "it was set up and something broke". Those need different errors: the first
  is *go run sparkup*, the second is *something is wrong, here is what*.
- Probing cannot discover the box's chosen values. This box's shared dir is
  `/srv/bbm`, not the repo default. No amount of probing finds that; the box
  has to say so.
- It makes provisioning legible. `cat /etc/sparks/box.toml` answers "is this
  box set up for sparks, and how" in one command.

Why `/etc` and not inside the shared tree: `/etc` is where box configuration
belongs, it is outside anything `make deploy` rsyncs with `--delete`, and it
survives renaming the shared directory — which is exactly the migration that
is coming.

Root-owned, `0644`. Every user reads it; only provisioning writes it.

## What sparks changes

### 1. A config module that loads the contract

New `src/sparks/box.py`:

- `load(path=BOX_CONFIG) -> Box | None` — parses the TOML with `tomllib`
  (stdlib), returns `None` when the file does not exist.
- `Box` dataclass with the five fields above.
- `preflight(box) -> list[str]` — verifies the claims rather than trusting
  them, because a stale contract is worse than a missing one. Checks
  `shared_dir/runs` exists and is writable by this user, and `textfile_dir`
  exists and is writable. Returns human-readable complaints.

Prometheus reachability is **not** part of the preflight. An unreachable
Prometheus is degraded telemetry, and the rule that telemetry never kills a
run still holds. An unwritable shared directory is different in kind: the
record of the run would be lost, which is the one thing the wrapper exists to
prevent.

### 2. The CLI is the boundary, not the library

`launcher.launch()` stays a plain library call taking explicit paths. The
187-test unit suite keeps working untouched, and embedding sparks in a harness
stays possible.

`sparks run` and `sparks demo` grow the preflight:

- `--shared-dir`, `--url`, `--grafana` lose their hardcoded defaults. Their
  values come from `/etc/sparks/box.toml`.
- An explicit flag always wins, so a laptop or a box sparkup does not manage
  stays usable: `sparks run --shared-dir /tmp/x --url "" -- cmd`.
- No contract and no explicit flags is an error, exit **78** (`EX_CONFIG`),
  chosen so a queue can tell a misconfigured box from a crashed job:

```
sparks: this box is not configured for sparks.

/etc/sparks/box.toml does not exist, so there is no provisioned shared
directory to record this run in and no Prometheus to publish it to.

Provision the box with sparkup:
    cd sparkup && make apply

Or, for a box sparkup does not manage, say where things are explicitly:
    sparks run --shared-dir DIR --url URL -- your command
```

### 3. Delete the silent fallback

`textfile_dir()` loses `return default if default.is_dir() else shared_dir / "index"`.
The textfile directory comes from the contract, `SPARKS_TEXTFILE_DIR` still
overrides it for tests, and an unwritable one is a preflight failure. A
directory nothing scrapes is not a fallback; it is data loss with a friendly
face.

### 4. Tests (written first)

- No contract, no flags → exit 78, message names the missing file.
- No contract, explicit `--shared-dir` and `--url` → runs normally.
- Contract present → its values are the defaults; flags still override.
- Contract pointing at a non-existent `runs/` → error names that directory,
  not the config file.
- Contract pointing at a read-only `runs/` → error says unwritable.
- Contract whose `textfile_dir` is missing → error, no fallback anywhere.
- Malformed TOML → error naming the file and the parse problem.

## What sparkup changes

### 5. `monitoring`: a rules directory Prometheus reads

- `rule_files: [/etc/prometheus/rules/*.yml]` in `prometheus.yml.j2`.
- A `./prometheus/rules` bind mount, read-only, in `compose.yml.j2`.
- The reload handler already exists and is reused.

**Not** a drop-off directory in the shared tree, which is how dashboards work.
The asymmetry is deliberate and worth the inconsistency: a malformed dashboard
degrades one Grafana panel, while a malformed rule file makes Prometheus
**refuse to start**. A shared-group-writable rules directory would let any user
brick monitoring on the next reboot. Rules are installed by root, from the
repo, validated first.

### 6. A `sparks` role

Owns sparks' box-side infrastructure and nothing else. It does *not* install
the Python package: the framework is a library users pip-install into their own
venv, and `make deploy` remains the fast dev loop.

- Writes `/etc/sparks/box.toml` from the role's template, values taken from the
  existing `spark_shared_dir` / `spark_shared_group` / port variables so there
  is one source of truth.
- Installs the alert rules into the new rules directory, notifying the reload
  handler. Vendored as `roles/sparks/files/sparks-alerts.yml` with a header
  naming the sparks commit it came from; sparks keeps authoring them in
  `alerts/sparks.yml`.
- Validates the rules with `promtool` **before** installing them, run in the
  already-pinned Prometheus image, so an invalid file cannot reach the mount.
  This is the assert-loudly rule: a converge that would stop Prometheus at the
  next reboot must fail now, while the box is still working.
- Asserts its preconditions: the shared tree exists (the `users` role made it),
  the textfile directory exists (the `exporters` role made it), and
  `--web.enable-remote-write-receiver` is in the Prometheus command line.
- `README.md`, per the one-README-per-role rule.

Ordered after `monitoring` in `site.yml`, since it writes into the rules
directory that role mounts.

### 7. The `/srv/bbm` rename — deferred here, then done anyway

`host_vars/spark.yml` set `spark_shared_dir: /srv/bbm` and
`spark_shared_group: bbm`, naming one consumer project for the tree every job
shares. It is now `/srv/spark` and group `spark`, matching the repo default.

This section originally deferred it, on the grounds that moving checkpoints and
renaming a group that running processes reference needs an idle box and a plan of
its own. Surveying the box first showed the first half of that was pessimism: the
GPU was idle, `lsof +D` found nothing holding the tree, no systemd unit, cron
entry or file under `/etc` referenced the path, and the whole tree was 3 test runs
on the same filesystem as its destination.

Two things made it a rename rather than a migration:

- `groupmod -n spark bbm` keeps GID 1002, so **every file on the box followed
  without a single chown** — including `/var/lib/node_exporter/textfile`, which
  was group `bbm` and is now group `spark` because it was always really group
  1002. Both members survived.
- `/srv` and its destination are one filesystem, so `mv` was a directory rename,
  not a copy of checkpoint data.

What did need editing was `SPARKS_SHARED_DIR` in three `local.mk` files (the box's
checkout, its worktree, and the laptop's). A first survey missed them: the `sudo
grep` that was supposed to find them had failed for want of a password, with
stderr discarded, so it reported no matches rather than no access. **A silent
`sudo` failure reads exactly like a clean result.** Re-running it with a working
`--become-password-file` found all three.

Worth recording: with the contract in place, that mistake is now self-correcting.
`make deploy` compares `SPARKS_SHARED_DIR` against what the box declares and
refuses on a mismatch, so a stale `local.mk` is an error at the boundary instead
of a deploy into the wrong tree.

## Acceptance — all of it done

Local:

- `make check` green in sparks: ruff, mypy strict, **207 tests**, dashboards.
- `make lint` (production profile) and `make syntax` green in sparkup, plus
  `make offline` including the container idempotence run.
- `sparks run` on the laptop, no contract, exits **78** with the message above.
- `make deploy` distinguishes three cases: no contract on the box, a
  `SPARKS_SHARED_DIR` that disagrees with the box, and a box it cannot reach.

On the box, after `make apply --tags users,exporters,monitoring,sparks`
(44 ok, 7 changed, 0 failed):

- `/etc/sparks/box.toml` declares `/srv/spark`, group `spark`, node_exporter's
  textfile directory and loopback Prometheus.
- `sparks run --name contract-ok -b 2 -- python3 -c "print(1+1)"` with **no path
  flags at all** recorded to `/srv/spark/runs/...`, status `finished`, exit 0, and
  published the index to `/var/lib/node_exporter/textfile/sparks_runs.prom`.
- The same command with `SPARKS_BOX_CONFIG` pointed at a missing file exits 78.
- `tests/on_box.py --section contract` passes 4/4.
- `/api/v1/rules` lists the `sparks` group with all three rules `inactive`, which
  also proves the run index survived the rename.
- All four scrape targets `up`, including `node_textfile` — the job label
  `SparksTextfileError` depends on, which did not exist on this box before.
- Grafana serves all three dashboards from the renamed mount.

Not exercised: a deliberately broken rule file failing the converge. The staging
and promtool steps ran for real (they are `check_mode: false`), so the mechanism
is live, but no invalid file was ever fed to it.

## Known residue

Three `summary.json` files from 2026-08-04 (`run-...-t1`, `t2`, `t4`) predate the
nullable-energy schema and now fail to load with
`Energy.__init__() got an unexpected keyword argument 'sources_agree'`. This is
the backward-incompatible change made knowingly earlier; the launcher skips them
and logs one line each, so every rebuild prints three warnings. They are test
runs. Deleting the three directories removes the noise and costs nothing real.
