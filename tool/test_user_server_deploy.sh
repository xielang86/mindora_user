#!/usr/bin/env bash
# Post-deploy smoke test for the user_server HTTP API.
# Uses tool/user_server_client.py to call update_profile and query_profile
# on the remote user_server domains and checks key user_profile fields.
#
# Usage:
#   ./tool/test_user_server_deploy.sh
#   DEPLOY_TEST_UID=mindora_test_uid1 ./tool/test_user_server_deploy.sh
#   JWT_TOKEN=xxx ./tool/test_user_server_deploy.sh

set -euo pipefail

# Run from the repo root so tool/user_server_client.py is found
cd "$(dirname "$0")/.."

ENDPOINTS=(
  "https://api.mindora316.com/user_server"
  "https://api.mindora316.cn/user_server"
)

TEST_UID="${DEPLOY_TEST_UID:-test_debug_user_001}"
JWT_TOKEN="${JWT_TOKEN:-}"
UPDATE_TIMEOUT="${UPDATE_TIMEOUT:-120}"
QUERY_TIMEOUT="${QUERY_TIMEOUT:-30}"

common_args=("--uid" "$TEST_UID")
if [ -n "$JWT_TOKEN" ]; then
  common_args+=("--jwt-token" "$JWT_TOKEN")
fi

run_client() {
  python3 tool/user_server_client.py "$@" 2>&1 || true
}

fail=0

for endpoint in "${ENDPOINTS[@]}"; do
  echo ""
  echo "=== Testing $endpoint ==="

  echo "-> update_profile (timeout ${UPDATE_TIMEOUT}s)"
  update_out=$(run_client update_profile \
    --base-url "$endpoint" \
    --timeout "$UPDATE_TIMEOUT" \
    "${common_args[@]}")
  echo "$update_out"

  update_code=$(echo "$update_out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("response", {}).get("code", "?"))
except Exception:
    print("?")
')

  if [ "$update_code" != "0" ]; then
    echo "   FAIL: update_profile returned code=$update_code"
    fail=1
    continue
  fi
  echo "   update_profile OK"

  echo "-> query_profile (timeout ${QUERY_TIMEOUT}s)"
  query_out=$(run_client query_profile \
    --base-url "$endpoint" \
    --timeout "$QUERY_TIMEOUT" \
    "${common_args[@]}")
  echo "$query_out"

  query_code=$(echo "$query_out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("response", {}).get("code", "?"))
except Exception:
    print("?")
')

  if [ "$query_code" != "0" ]; then
    echo "   FAIL: query_profile returned code=$query_code"
    fail=1
    continue
  fi
  echo "   query_profile OK"

  fields=$(echo "$query_out" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    up = d.get("response", {}).get("data", {}).get("user_profile", {})
    print(
        len(up.get("standard_sop_reco", [])),
        len(up.get("sleep_scenarios_reco") or []),
        len(up.get("long_term_profile", [])),
        len(up.get("behaviors", {}).get("heart_rate", [])),
    )
except Exception:
    print("0 0 0 0")
')
  read sop_len sleep_len long_len hr_len <<< "$fields"

  echo "   standard_sop_reco:  $sop_len"
  echo "   sleep_scenarios_reco: $sleep_len"
  echo "   long_term_profile:  $long_len"
  echo "   behaviors.heart_rate: $hr_len"

  if [ "$sop_len" -eq 0 ]; then
    echo "   FAIL: standard_sop_reco is empty"
    fail=1
  fi

done

echo ""
if [ "$fail" -ne 0 ]; then
  echo "DEPLOY TEST FAILED"
  exit 1
fi

echo "DEPLOY TEST PASSED"
