#!/usr/bin/env bash
# A real Prometheus on 127.0.0.1:19091, for the live tests.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/harness"

docker compose up -d --wait

# --wait returns when the container is running, not when Prometheus answers.
for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:19091/-/ready >/dev/null 2>&1; then
    echo "harness: prometheus ready on 127.0.0.1:19091"
    exit 0
  fi
  sleep 1
done

echo "harness: prometheus did not become ready" >&2
docker compose logs prometheus >&2
exit 1
