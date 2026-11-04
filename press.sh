#!/usr/bin/env bash
# Run a measured load test against the isolated service; never seeds users.
set -euo pipefail
cd "$(dirname "$0")"

PRESS_RUN_DIR=${PRESS_RUN_DIR:-/tmp/mindora-loadtest}
PRESS_PORT=${PRESS_PORT:-19001}
PRESS_PYTHON=${PRESS_PYTHON:-$(command -v python || command -v python3)}
PRESS_JWT_SECRET=${PRESS_JWT_SECRET:-mindora-local-load-test}
PRESS_PROFILES=${PRESS_PROFILES:-$PRESS_RUN_DIR/profiles.json}
PRESS_DURATION=${PRESS_DURATION:-60}
PRESS_QPS=${PRESS_QPS:-900}
PRESS_CONCURRENCY=${PRESS_CONCURRENCY:-64}
PRESS_MIX=${PRESS_MIX:-revision=70,device=15,app=5,analysis=5,update=5}
PRESS_SEED=${PRESS_SEED:-316}
PRESS_OUTPUT=${PRESS_OUTPUT:-$PRESS_RUN_DIR/press-$(date +%Y%m%d_%H%M%S).json}

[[ -f "$PRESS_PROFILES" ]] || { echo "missing fixture: $PRESS_PROFILES; run gen_press_user.sh first" >&2; exit 2; }
mkdir -p "$(dirname "$PRESS_OUTPUT")"
JWT_SECRET_KEY="$PRESS_JWT_SECRET" "$PRESS_PYTHON" tool/mixed_load_test.py \
  --base-url "http://127.0.0.1:$PRESS_PORT" --profiles "$PRESS_PROFILES" \
  --duration "$PRESS_DURATION" --qps "$PRESS_QPS" \
  --concurrency "$PRESS_CONCURRENCY" --mix "$PRESS_MIX" \
  --seed "$PRESS_SEED" --output "$PRESS_OUTPUT"
