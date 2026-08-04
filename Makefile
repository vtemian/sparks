.PHONY: sync fmt lint typecheck test dashboard check live harness-up harness-down

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
	uv run pytest -m live
	$(MAKE) harness-down

harness-up:
	./tests/harness-up.sh

harness-down:
	./tests/harness-down.sh
