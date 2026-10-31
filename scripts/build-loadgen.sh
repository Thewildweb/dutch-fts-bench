#!/usr/bin/env bash
# Build the Go load generator in a container (no local Go needed): loadgen/bin/loadgen
set -euo pipefail
cd "$(dirname "$0")/../loadgen"
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e GOFLAGS=-buildvcs=false \
  -v "$PWD":/src -w /src golang:1.25 sh -c \
  '[ -f go.mod ] || go mod init dutch-fts-bench/loadgen; go mod tidy && go vet ./... && CGO_ENABLED=0 go build -o bin/loadgen .'
ls -la bin/loadgen
