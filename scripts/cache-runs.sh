#!/usr/bin/env bash
# Cache experiment for one engine: skewed (Zipf) query popularity at a fixed rate, with the
# engine's result cache off, on, and on while documents are being written.
# Usage: scripts/cache-runs.sh <subset> <engine> [rate] [writes_per_s] [label_suffix]
# FACETS=0 skips the facet runs (pg_textsearch can only rank, not count; its facets would
# measure a sequential scan).
set -euo pipefail
subset=$1; engine=$2; rate=${3:-100}; writes=${4:-20}; suffix=${5:-}
bench() { uv run bench --subset "$subset" "$@"; }
common=(--profile website --rate "$rate" --concurrency 16 --repeats 20 --zipf 1.0)
ops=("--facets" ""); [[ ${FACETS:-1} == 0 ]] && ops=("")
# Warm up first: right after a container start the first requests are slow (JIT, cold
# caches), and that belongs in the cold runs, not here.
for op in "${ops[@]}"; do
  bench latency "$engine" --profile website --concurrency 1 --repeats 2 $op --label warmup >/dev/null
done
for op in "${ops[@]}"; do
  bench latency "$engine" "${common[@]}" $op --label "cache-off$suffix"
  bench latency "$engine" "${common[@]}" $op --cache --label "cache-on$suffix"
  bench latency "$engine" "${common[@]}" $op --cache --writes "$writes" --label "cache-on-writes$suffix"
done
