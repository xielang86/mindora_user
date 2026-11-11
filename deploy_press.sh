#!/usr/bin/env bash
# Start an isolated, local-only user_server for load tests.
set -euo pipefail
cd "$(dirname "$0")"

PRESS_RUN_DIR=${PRESS_RUN_DIR:-/tmp/mindora-loadtest}
PRESS_PORT=${PRESS_PORT:-19001}
PRESS_PYTHON=${PRESS_PYTHON:-$(command -v python || command -v python3)}
PRESS_JWT_SECRET=${PRESS_JWT_SECRET:-mindora-local-load-test}

mkdir -p "$PRESS_RUN_DIR/data"
cp data/popup_survey_config.json "$PRESS_RUN_DIR/data/popup_survey_config.json"
echo "press server: code=$PWD data=$PRESS_RUN_DIR port=$PRESS_PORT"
exec env RUN_DIR="$PRESS_RUN_DIR" USER_SERVER_HOST=127.0.0.1 \
  USER_SERVER_PORT="$PRESS_PORT" JWT_SECRET_KEY="$PRESS_JWT_SECRET" \
  ENABLE_LLM=false ENABLE_SLEEP_RECO=false \
  "$PRESS_PYTHON" user_server.py



