#!/usr/bin/env bash
# Generate one fixture and seed the isolated service. Keep the fixture for both versions.
set -euo pipefail
cd "$(dirname "$0")"

PRESS_RUN_DIR=${PRESS_RUN_DIR:-/tmp/mindora-loadtest}
PRESS_PORT=${PRESS_PORT:-19001}
PRESS_PYTHON=${PRESS_PYTHON:-$(command -v python || command -v python3)}
PRESS_JWT_SECRET=${PRESS_JWT_SECRET:-mindora-local-load-test}
PRESS_PROFILES=${PRESS_PROFILES:-$PRESS_RUN_DIR/profiles.json}
PRESS_COUNT=${PRESS_COUNT:-100}
PRESS_NIGHTS=${PRESS_NIGHTS:-7}
PRESS_SEED=${PRESS_SEED:-316}

mkdir -p "$PRESS_RUN_DIR"
if [[ ! -f "$PRESS_PROFILES" ]]; then
  "$PRESS_PYTHON" tool/generate_load_profiles.py \
    --count "$PRESS_COUNT" --nights "$PRESS_NIGHTS" --seed "$PRESS_SEED" \
    --output "$PRESS_PROFILES"
fi
JWT_SECRET_KEY="$PRESS_JWT_SECRET" "$PRESS_PYTHON" tool/mixed_load_test.py \
  --base-url "http://127.0.0.1:$PRESS_PORT" --profiles "$PRESS_PROFILES" \
  --seed-profiles --duration 0.1 --qps 1 --concurrency 16 --mix revision=1 \
  --seed "$PRESS_SEED" --output "$PRESS_RUN_DIR/seed-result.json"
