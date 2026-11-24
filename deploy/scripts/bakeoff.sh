#!/bin/sh
# Run the understand bake-off in the existing kagebox/worker:dev image WITHOUT rebuilding it: the
# working tree's pipeline/ and orchestration/ are bind-mounted read-only over the image's copy.
# `compose run` never builds an image that already exists (pull_policy: never). A rebuild needs apt, which fails with an invalid-signature error while the Debian archive keys
# are out of date here, and a failed rebuild fills the Docker VM disk with build cache.)
#   sh deploy/scripts/bakeoff.sh --chapter <id> --pages 7-20 --limit 10 ...
#   sh deploy/scripts/bakeoff.sh --record /reports/fx --synthetic-cases speech,speech_injected,blank,night
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
mkdir -p data/reports
exec docker compose -f deploy/compose.yaml --env-file .env --profile bakeoff run --rm \
  -v "$ROOT/pipeline":/app/pipeline:ro -v "$ROOT/orchestration":/app/orchestration:ro \
  understand-bakeoff python -m orchestration.understand_bakeoff "$@"
