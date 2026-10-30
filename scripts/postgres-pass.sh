#!/usr/bin/env bash
# One full benchmark pass for the arms in one Postgres container.
# Usage: scripts/postgres-pass.sh <subset> <arm>...   e.g. scripts/postgres-pass.sh full pg-fts pg-search
# The container must be up with the needed index arms built (see README). Writing the result
# lists first also warms the caches, so the latency runs that follow are labelled "warm".
set -euo pipefail
subset=$1; shift
bench() { uv run bench --subset "$subset" "$@"; }

for arm in "$@"; do
  echo "== $arm: result lists"
  bench runs "$arm"
done

for arm in "$@"; do
  echo "== $arm: latency"
  if [[ $arm == pg-ilike || $arm == pg-trgm ]]; then
    # Substring scans are seconds per query at full size: fewer repeats, no high concurrency.
    bench latency "$arm" --k 10 --concurrency 1 4 --repeats 1
    continue
  fi
  bench latency "$arm" --k 10 --concurrency 1 4 16 --repeats 3
  bench latency "$arm" --k 100 --concurrency 1 --repeats 3
  bench latency "$arm" --facets --concurrency 1 --repeats 3
  for rate in 50 200; do
    bench latency "$arm" --profile website --rate "$rate" --concurrency 16 --repeats 20
  done
done
