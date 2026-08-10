.PHONY: install sync fmt lint typecheck test dashboard check live on-box harness-up harness-down deploy deploy-preflight sparks

# Your box's values, untracked, same split sparkup uses between tracked
# defaults and an untracked host file. Copy local.mk.example to local.mk.
# Included first, so what it sets wins over the defaults below.
-include local.mk

# The box, as ssh addresses it. No default: nobody else's hostname belongs in a
# tracked file, and a wrong guess here talks to the wrong machine.
#
# Exported, so `sparks submit` run from this directory sees the same value the
# Makefile does. Without that, local.mk sets a Make variable the client cannot
# read, and you get "set SPARKS_HOST" while looking at a file that sets it.
SPARKS_HOST ?=
export SPARKS_HOST

# Where training artifacts live: sparkup's `spark_shared_dir`. The repo default
# is /srv/spark; a box that overrode it in host_vars needs the same value here.
SPARKS_SHARED_DIR ?= /srv/spark

# The venv your training code runs in, which is whatever your project built.
# There is no sensible default, because it belongs to a project this one does
# not know about.
SPARKS_VENV ?=


# The one command a new user runs: install the client, then ask the box which
# registry it publishes and let this machine's Docker push to it. Docker still
# has to be restarted afterwards, which is why setup prints the command rather
# than running it: a restart kills whatever containers you have running.
install: sync
	uv run sparks setup

sync:
	uv sync

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests
	# House rules ruff cannot express yet.
	uv run python tests/check_names.py
	uv run python tests/check_banners.py
	uv run python tests/check_short_funcs.py
	uv run python tests/check_file_header.py
	uv run python tests/check_unused.py
	uv run python tests/check_private_prefix.py
	uv run python tests/check_no_docstrings.py
	uv run python tests/check_blank_after_return.py

typecheck:
	uv run mypy

test:
	uv run pytest -m "not live"

# Never query a metric nobody emits. The allowlist is derived from
# sparks.metrics.METRICS, so a panel naming something the emitter cannot
# produce fails here rather than showing an empty graph on the box.
dashboard:
	uv run python tests/check_dashboard.py

check: lint typecheck test dashboard

# Everything above runs without Docker. This does not: it brings up a real
# Prometheus, pushes real samples and reads them back. The thing under test is
# the wire format and the receiver's opinion of it, and a fake would only ever
# confirm our own assumptions.
live: harness-up
	@uv run pytest -m live; status=$$?; $(MAKE) harness-down; exit $$status

# The checks that only mean something on the real box: the 2775 migration, the
# tier-1 acceptance matrix across three umasks and two accounts, and the energy
# constants that were calibrated on one machine and guessed for every other.
# Run it ON the box, not from here. ARGS passes --other-user, --url or
# --fix-permissions through: make on-box ARGS="--other-user alice".
on-box:
	uv run python tests/on_box.py --shared-dir $(SPARKS_SHARED_DIR) $(ARGS)

harness-up:
	./tests/harness-up.sh

harness-down:
	./tests/harness-down.sh

# Refuse to install onto a box that cannot run what we are installing, and
# refuse to scp dashboards into a directory that is not the one the box uses.
# Both were silent before: the framework would install happily and the first run
# would record itself where nothing reads. Checked over one SSH round trip,
# before anything is written.
deploy-preflight:
	@test -n "$(SPARKS_HOST)" || { \
	  echo "set SPARKS_HOST to the box, as ssh addresses it."; \
	  echo "cp local.mk.example local.mk and edit it, or pass it inline:"; \
	  echo "  make deploy SPARKS_HOST=you@your-box"; exit 2; }
	@test -n "$(SPARKS_VENV)" || { \
	  echo "set SPARKS_VENV to the venv your training code runs in."; \
	  echo "cp local.mk.example local.mk and edit it, or pass it inline:"; \
	  echo "  make deploy SPARKS_VENV=\$$HOME/myproject/.venv"; exit 2; }
	@contract=$$(ssh $(SPARKS_HOST) \
	  'cat /etc/sparks/box.toml 2>/dev/null || echo __ABSENT__') || { \
	  echo "cannot reach $(SPARKS_HOST) over ssh, so there is nothing to check."; \
	  echo "This is a connection problem, not a provisioning one."; exit 2; }; \
	declared=$$(printf '%s' "$$contract" \
	  | sed -n 's/^shared_dir *= *"\(.*\)"/\1/p'); \
	if [ -z "$$declared" ]; then \
	  echo "$(SPARKS_HOST) is not configured for sparks."; \
	  echo "/etc/sparks/box.toml is missing or unreadable there, so a run on that"; \
	  echo "box would have nowhere to record itself. sparkup writes that file:"; \
	  echo "  cd sparkup && make apply"; \
	  exit 78; \
	fi; \
	if [ "$$declared" != "$(SPARKS_SHARED_DIR)" ]; then \
	  echo "SPARKS_SHARED_DIR does not match what $(SPARKS_HOST) provides."; \
	  echo "  here: $(SPARKS_SHARED_DIR)"; \
	  echo "  box:  $$declared"; \
	  echo "The box wins. Set it in local.mk, or drop the override."; \
	  exit 78; \
	fi

# Push the working tree, install it into the training venv, and hand Grafana the
# dashboards. The dashboard directory is setgid to the shared group, so this
# needs no root, and Grafana rescans on a 10s timer, so it needs no restart.
deploy: deploy-preflight
	rsync -az --delete \
	  --exclude '.git/' --exclude '.venv/' --exclude '.claude/' \
	  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.*_cache/' \
	  ./ $(SPARKS_HOST):sparks/
	ssh $(SPARKS_HOST) 'PATH=$$HOME/.local/bin:$$PATH \
	  VIRTUAL_ENV=$(SPARKS_VENV) uv pip install -q -e $$HOME/sparks'
	scp -q monitoring/dashboards/*.json \
	  $(SPARKS_HOST):$(SPARKS_SHARED_DIR)/dashboards/
	@echo "deployed to $(SPARKS_HOST):$(SPARKS_SHARED_DIR)"

# The client, with SPARKS_HOST already set from local.mk, so a checkout has one
# source of truth for which box it talks to:
#   make sparks ARGS="queue"
#   make sparks ARGS="submit --data ./corpus --name e0 -- python train.py"
sparks:
	uv run sparks $(ARGS)
