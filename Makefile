.PHONY: sync fmt lint typecheck test dashboard check live harness-up harness-down deploy

# Your box's values, untracked, same split sparkup uses between tracked
# defaults and an untracked host file. Copy local.mk.example to local.mk.
# Included first, so what it sets wins over the defaults below.
-include local.mk

# The box. Defaults to your own SSH login, since nobody else's belongs in a
# tracked file.
SPARKS_HOST ?= spark.local

# Where training artifacts live: sparkup's `spark_shared_dir`. The repo default
# is /srv/spark; a box that overrode it in host_vars needs the same value here.
SPARKS_SHARED_DIR ?= /srv/spark

# The venv your training code runs in, which is whatever your project built.
# There is no sensible default, because it belongs to a project this one does
# not know about.
SPARKS_VENV ?=


sync:
	uv sync

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests

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

harness-up:
	./tests/harness-up.sh

harness-down:
	./tests/harness-down.sh

# Push the working tree, install it into the training venv, and hand Grafana the
# dashboard. The dashboard directory is setgid to the shared group, so this
# needs no root, and Grafana rescans on a 10s timer, so it needs no restart.
deploy:
	@test -n "$(SPARKS_VENV)" || { \
	  echo "set SPARKS_VENV to the venv your training code runs in."; \
	  echo "cp local.mk.example local.mk and edit it, or pass it inline:"; \
	  echo "  make deploy SPARKS_VENV=\$$HOME/myproject/.venv"; exit 2; }
	rsync -az --delete \
	  --exclude '.git/' --exclude '.venv/' --exclude '.claude/' \
	  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.*_cache/' \
	  ./ $(SPARKS_HOST):sparks/
	ssh $(SPARKS_HOST) 'PATH=$$HOME/.local/bin:$$PATH \
	  VIRTUAL_ENV=$(SPARKS_VENV) uv pip install -q -e $$HOME/sparks'
	scp -q dashboards/training-runs.json \
	  $(SPARKS_HOST):$(SPARKS_SHARED_DIR)/dashboards/
	@echo "deployed to $(SPARKS_HOST):$(SPARKS_SHARED_DIR)"
