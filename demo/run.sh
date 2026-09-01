#!/bin/sh
set -e

teardown() {
  docker compose down --remove-orphans -v
}

trap teardown EXIT

rm -rf home
mkdir -p home/app
mkdir -p home/state
mkdir -p home/codex
docker compose build
docker compose up --exit-code-from coordinator
