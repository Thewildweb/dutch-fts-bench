#!/usr/bin/env bash
# Cold-start latency for one engine: restart its container with an empty OS page cache, then
# run every query once (single client). Needs Docker and a sudoers rule for drop_caches:
#   erik ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches
# Usage: scripts/cold-run.sh <subset> <compose-service> <engine>...
set -euo pipefail
subset=$1; service=$2; shift 2
for engine in "$@"; do
  echo "== $engine: cold start ($service)"
  docker compose stop "$service" >/dev/null
  sync
  echo 3 | sudo -n /usr/bin/tee /proc/sys/vm/drop_caches >/dev/null
  docker compose up -d "$service" >/dev/null
  if [[ $service == elasticsearch ]]; then
    until curl -s localhost:${ES_PORT:-9200}/_cluster/health | grep -q '"status":"\(green\|yellow\)"'; do sleep 2; done
  else
    until docker compose exec -T "$service" pg_isready -U bench >/dev/null 2>&1; do sleep 1; done
  fi
  uv run bench --subset "$subset" latency "$engine" --k 10 --concurrency 1 --repeats 1 --label cold
done
