#!/usr/bin/env bash
# Deploy from a relay host to one or more systemd hosts with Python 3.12.
# From the parent directory: bash mindora_user/deploy_user_server.sh --hosts IP1,IP2
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/root/server/mindora_user
VENV_DIR=/root/server/mindora_py
ENV_DIR=/etc/mindora
UNIT_PATH=/etc/systemd/system/mindora-user.service
PUBLIC_KEY_SOURCE="$SOURCE_DIR/jwt_public.pem"
CHECK_ONLY=0
LOCAL_MODE=0
HOSTS=()

usage() {
  echo "Usage: bash $0 --hosts IP1,IP2 [--public-key PATH] [--check]" >&2
  echo "       sudo bash $0 --local [--public-key PATH] [--check]" >&2
}

while (($#)); do
  case "$1" in
    --public-key)
      (($# >= 2)) || { usage; exit 2; }
      PUBLIC_KEY_SOURCE=$2
      shift 2
      ;;
    --hosts)
      (($# >= 2)) || { usage; exit 2; }
      IFS=',' read -r -a parsed_hosts <<< "$2"
      HOSTS+=("${parsed_hosts[@]}")
      shift 2
      ;;
    --local) LOCAL_MODE=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) HOSTS+=("$1"); shift ;;
  esac
done

if ((LOCAL_MODE)) && ((${#HOSTS[@]})); then
  echo '--local cannot be combined with target hosts.' >&2
  exit 2
fi
if ((!LOCAL_MODE)) && ((${#HOSTS[@]} == 0)) && ((!CHECK_ONLY)); then
  usage
  exit 2
fi

for path in user_server.py requirments.txt \
            systemd/mindora-user.service systemd/mindora.env.example \
            systemd/mindora-user.env.example systemd/mindora-auth.env.example; do
  [[ -f "$SOURCE_DIR/$path" ]] || { echo "Missing $SOURCE_DIR/$path" >&2; exit 1; }
done
if [[ ! -f "$PUBLIC_KEY_SOURCE" ]]; then
  echo "Public key not found: $PUBLIC_KEY_SOURCE" >&2
  exit 1
fi
PUBLIC_KEY_SOURCE="$(realpath "$PUBLIC_KEY_SOURCE")"
export PUBLIC_KEY_SOURCE
if ((!LOCAL_MODE)) && ((${#HOSTS[@]})); then
  command -v ssh >/dev/null || { echo 'ssh is required on the relay host.' >&2; exit 1; }
  command -v rsync >/dev/null || { echo 'rsync is required on the relay host.' >&2; exit 1; }
  command -v tar >/dev/null || { echo 'tar is required on the relay host.' >&2; exit 1; }
  for host in "${HOSTS[@]}"; do
    [[ "$host" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid host: $host" >&2; exit 2; }
  done
fi
if ((CHECK_ONLY)); then
  if ((LOCAL_MODE)); then
    command -v python3.12 >/dev/null || { echo 'python3.12 is required on the target host' >&2; exit 1; }
    python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
  fi
  if [[ -f "$SOURCE_DIR/package.sh" ]]; then
    bash "$SOURCE_DIR/package.sh" --check
  fi
  if ((LOCAL_MODE)); then
    echo 'Preflight passed (source files and Python 3.12 found).'
  else
    echo 'Preflight passed (source files and relay tools found).'
  fi
  exit 0
fi
if [[ -f "$SOURCE_DIR/package.sh" ]]; then
  bash "$SOURCE_DIR/package.sh" --check
fi

if ((!LOCAL_MODE)); then
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  if [[ -f "$SOURCE_DIR/package.sh" ]]; then
    ARCHIVE="$(bash "$SOURCE_DIR/package.sh" | sed -n 's/^打包完成: //p')"
    [[ "$ARCHIVE" = /* ]] || ARCHIVE="$SOURCE_DIR/$ARCHIVE"
    [[ -f "$ARCHIVE" ]] || { echo 'package.sh did not create an archive.' >&2; exit 1; }
    tar -xzf "$ARCHIVE" -C "$STAGE"
    DEPLOY_SOURCE="$STAGE/user_server"
  else
    # The script can also run from an already extracted deployment package.
    DEPLOY_SOURCE="$STAGE/user_server"
    cp -a "$SOURCE_DIR" "$DEPLOY_SOURCE"
  fi
  if [[ ! -f "$SOURCE_DIR/package.sh" ]]; then
    cp "$PUBLIC_KEY_SOURCE" "$DEPLOY_SOURCE/jwt_public.pem"
  fi
  REMOTE_STAGE="/root/server/mindora_user_incoming_$(date +%Y%m%d_%H%M%S)_$$"
  failed=()
  for host in "${HOSTS[@]}"; do
    echo "Deploying to $host (stage: $REMOTE_STAGE)"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "root@$host" \
        "install -d -m 0700 '$REMOTE_STAGE' && (command -v rsync >/dev/null || { command -v apt-get >/dev/null && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y rsync; })"; then
      failed+=("$host")
      continue
    fi
    if ! rsync -a -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' -- \
        "$DEPLOY_SOURCE/" "root@$host:$REMOTE_STAGE/"; then
      failed+=("$host")
      continue
    fi
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$host" \
        "bash '$REMOTE_STAGE/deploy_user_server.sh' --local"; then
      failed+=("$host")
      continue
    fi
    echo "Deployed $host successfully."
  done
  if ((${#failed[@]})); then
    echo "Failed hosts: ${failed[*]}" >&2
    exit 1
  fi
  exit 0
fi

command -v python3.12 >/dev/null || { echo 'python3.12 is required on the target host' >&2; exit 1; }
python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'

((EUID == 0)) || { echo 'Run this script with sudo/root.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'systemctl is required.' >&2; exit 1; }
command -v tar >/dev/null || { echo 'tar is required.' >&2; exit 1; }

env_value() {
  local name=$1 file value result=""
  while IFS= read -r file; do
    [[ -f "$file" ]] || continue
    value="$(awk -F= -v key="$name" '$1 == key {print substr($0, index($0, "=") + 1)}' "$file" | tail -n 1)"
    if awk -F= -v key="$name" '$1 == key {found=1} END {exit !found}' "$file"; then
      value="${value#\"}"
      value="${value%\"}"
      result="$value"
    fi
  done < <(sed -n 's/^EnvironmentFile=//p' "$SOURCE_DIR/systemd/mindora-user.service")
  printf '%s' "$result"
}

install -d -m 0755 "$ENV_DIR" "$APP_DIR/data" "$APP_DIR/user_server_logs"
if [[ ! -e "$ENV_DIR/mindora.env" ]]; then
  # The example's known JWT_SECRET_KEY placeholder must never enable HS256 fallback.
  sed 's/^JWT_SECRET_KEY=__CHANGE_ME_RANDOM__$/JWT_SECRET_KEY=/' \
    "$SOURCE_DIR/systemd/mindora.env.example" > "$ENV_DIR/mindora.env"
  chmod 0600 "$ENV_DIR/mindora.env"
fi
if [[ ! -e "$ENV_DIR/mindora-user.env" ]]; then
  install -m 0600 "$SOURCE_DIR/systemd/mindora-user.env.example" "$ENV_DIR/mindora-user.env"
fi
if [[ -z "$(env_value JWT_PUBLIC_KEY_PATH)" ]]; then
  user_env="$ENV_DIR/mindora-user.env"
  cp -a "$user_env" "$user_env.backup.$(date +%Y%m%d_%H%M%S)"
  python3.12 - "$user_env" "$ENV_DIR/jwt_public.pem" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
line = f"JWT_PUBLIC_KEY_PATH={sys.argv[2]}"
lines = [item for item in path.read_text().splitlines() if not item.startswith("JWT_PUBLIC_KEY_PATH=")]
path.write_text("\n".join([*lines, line]) + "\n")
PY
  chmod 0600 "$user_env"
  echo "Added JWT_PUBLIC_KEY_PATH to $user_env (backup created)."
fi
if [[ ! -e "$ENV_DIR/mindora-auth.env" ]]; then
  install -m 0600 "$SOURCE_DIR/systemd/mindora-auth.env.example" "$ENV_DIR/mindora-auth.env"
fi
while IFS= read -r env_file; do
  [[ -f "$env_file" ]] || { echo "Missing required systemd EnvironmentFile: $env_file" >&2; exit 1; }
done < <(sed -n 's/^EnvironmentFile=//p' "$SOURCE_DIR/systemd/mindora-user.service")
configured_run_dir="$(env_value RUN_DIR)"
if [[ "$configured_run_dir" != "$APP_DIR" ]]; then
  echo "RUN_DIR in $ENV_DIR must be $APP_DIR (got $configured_run_dir)." >&2
  exit 1
fi
configured_key_path="$(env_value JWT_PUBLIC_KEY_PATH)"
if [[ "$configured_key_path" != "$ENV_DIR/jwt_public.pem" ]]; then
  echo "JWT_PUBLIC_KEY_PATH in $ENV_DIR must be $ENV_DIR/jwt_public.pem (got $configured_key_path)." >&2
  exit 1
fi

if ! python3.12 -m venv "$VENV_DIR"; then
  command -v apt-get >/dev/null || { echo 'Install the Python 3.12 venv module and retry.' >&2; exit 1; }
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12-venv
  python3.12 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
if ! "$VENV_DIR/bin/python" -m pip install -r "$SOURCE_DIR/requirments.txt"; then
  command -v apt-get >/dev/null || { echo 'Dependency install failed; check plyvel build requirements.' >&2; exit 1; }
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential python3.12-dev libleveldb-dev
  "$VENV_DIR/bin/python" -m pip install -r "$SOURCE_DIR/requirments.txt"
fi
"$VENV_DIR/bin/python" - "$PUBLIC_KEY_SOURCE" <<'PY'
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_public_key
import sys
load_pem_public_key(Path(sys.argv[1]).read_bytes())
PY

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
if [[ -f "$SOURCE_DIR/package.sh" ]]; then
  ARCHIVE="$(bash "$SOURCE_DIR/package.sh" | sed -n 's/^打包完成: //p')"
  [[ "$ARCHIVE" = /* ]] || ARCHIVE="$SOURCE_DIR/$ARCHIVE"
  [[ -f "$ARCHIVE" ]] || { echo 'package.sh did not create an archive.' >&2; exit 1; }
  tar -xzf "$ARCHIVE" -C "$STAGE"
  DEPLOY_SOURCE="$STAGE/user_server"
else
  # An extracted package already contains the exact runtime file set.
  DEPLOY_SOURCE="$SOURCE_DIR"
fi

# Check lazy recommendation dependencies from the package before stopping the service.
(cd "$DEPLOY_SOURCE" && RUN_DIR="$DEPLOY_SOURCE" "$VENV_DIR/bin/python" -B - <<'PY'
import user_server
from content_tags import catalog_by_command
from llm.reco import _load_sop_candidate_scenarios
from sop_ranking import rules

if not rules()["version"] or not catalog_by_command() or not _load_sop_candidate_scenarios():
    raise SystemExit("Recommendation rules, catalog or candidates are empty.")
PY
)

if systemctl is-active --quiet mindora-user.service; then
  systemctl stop mindora-user.service
fi
if [[ -d "$APP_DIR/data/userprofile_level_db" ]]; then
  BACKUP="$APP_DIR/data/userprofile_level_db.backup.$(date +%Y%m%d_%H%M%S)"
  cp -a "$APP_DIR/data/userprofile_level_db" "$BACKUP"
  echo "LevelDB backup: $BACKUP"
fi

if [[ "$(readlink -f "$DEPLOY_SOURCE")" != "$(readlink -f "$APP_DIR")" ]]; then
  cp -a "$DEPLOY_SOURCE/." "$APP_DIR/"
fi
install -m 0644 "$PUBLIC_KEY_SOURCE" "$ENV_DIR/jwt_public.pem"
install -m 0644 "$APP_DIR/systemd/mindora-user.service" "$UNIT_PATH"
systemctl daemon-reload
systemctl enable --now mindora-user.service
PORT="$(env_value USER_SERVER_PORT)"
PORT="${PORT:-9001}"
for _ in {1..15}; do
  if systemctl is-active --quiet mindora-user.service; then
    if "$VENV_DIR/bin/python" - "$PORT" <<'PY'
import http.client
import sys
connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=1)
try:
    connection.request("GET", "/user_profile")
    if connection.getresponse().status != 405:
        sys.exit(1)
except (OSError, http.client.HTTPException):
    sys.exit(1)
finally:
    connection.close()
PY
    then
      echo "mindora-user.service is active; code=$APP_DIR python=$VENV_DIR/bin/python"
      exit 0
    fi
  fi
  sleep 1
done
systemctl status mindora-user.service --no-pager || true
journalctl -u mindora-user.service -n 40 --no-pager || true
echo 'mindora-user.service did not stay active.' >&2
exit 1
