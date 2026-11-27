# Sourced by deploy_user_server.sh for an existing systemd installation.

update_hosts() (
  local stage host remote failed=()
  local -a ssh_args=(-o BatchMode=yes -o ConnectTimeout=10) flags=(--update)
  command -v ssh >/dev/null && command -v rsync >/dev/null ||
    { echo 'ssh and rsync are required on the relay.' >&2; exit 1; }
  if ((CHECK_ONLY)); then flags+=(--check); fi
  for host in "${HOSTS[@]}"; do
    [[ "$host" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid host: $host" >&2; exit 2; }
  done
  stage="$(mktemp -d)"
  trap 'rm -rf -- "$stage"' EXIT
  bash "$SOURCE_DIR/package.sh" --update --stage "$stage"
  if ((LOCAL_TARGET)); then
    echo 'Preparing update on localhost'
    # Run the local update in its own shell so failure does not skip remote targets.
    if ! bash "$SOURCE_DIR/deploy_user_server.sh" --hosts localhost "${flags[@]}"; then
      failed+=(localhost)
    fi
  fi
  for host in "${HOSTS[@]}"; do
    echo "Preparing update on $host"
    if ! remote="$(ssh "${ssh_args[@]}" "root@$host" \
        'command -v rsync >/dev/null && mktemp -d /root/server/.mindora-user-update.XXXXXXXX')"; then
      failed+=("$host")
      continue
    fi
    if [[ ! "$remote" =~ ^/root/server/\.mindora-user-update\.[A-Za-z0-9]+$ ]]; then
      echo "Invalid staging directory returned by $host" >&2
      failed+=("$host")
      continue
    fi
    if ! rsync -a --checksum --copy-dest="$APP_DIR" -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
       -- "$stage/" "root@$host:$remote/" ||
       ! ssh "${ssh_args[@]}" "root@$host" "bash '$remote/deploy_user_server.sh' --local ${flags[*]}"; then
      failed+=("$host")
    fi
    if ! ssh "${ssh_args[@]}" "root@$host" "rm -rf -- '$remote'"; then
      echo "Could not remove update staging directory: $host:$remote" >&2
    fi
  done
  if (("${#failed[@]}")); then echo "Failed hosts: ${failed[*]}" >&2; exit 1; fi
)

update_local() (
  ((EUID == 0)) || { echo 'Run the update on the target as root.' >&2; exit 1; }
  [[ -f "$SOURCE_DIR/package.sh" ]] || { echo 'Run --local --update from a checkout or an update package.' >&2; exit 1; }
  if ((!CHECK_ONLY)); then lock_deployment; fi
  local installed_unit workdir exec_start installed_python="$VENV_DIR/bin/python"
  installed_unit="$(systemctl show "$SERVICE" --property=FragmentPath --value)"
  workdir="$(systemctl show "$SERVICE" --property=WorkingDirectory --value)"
  exec_start="$(systemctl show "$SERVICE" --property=ExecStart --value)"
  if [[ "$installed_unit" != "$UNIT_PATH" ]]; then
    echo "$SERVICE unit: expected $UNIT_PATH, found ${installed_unit:-no unit}." >&2
    exit 1
  fi
  if [[ "$workdir" != "$APP_DIR" || ! -f "$APP_DIR/user_server.py" ]]; then
    echo "$SERVICE must run from $APP_DIR with user_server.py present (WorkingDirectory=${workdir:-unset})." >&2
    exit 1
  fi
  if ((LOCAL_TARGET)); then
    local python_path='path=(/[^[:space:];]+/bin/python([0-9]+(\.[0-9]+)?)?)[[:space:]]*;'
    if [[ "$exec_start" =~ $python_path ]]; then
      installed_python="${BASH_REMATCH[1]}"
    else
      echo "$SERVICE ExecStart must use an existing Python virtualenv for localhost updates." >&2
      exit 1
    fi
    VENV_DIR="${installed_python%/bin/*}"
    if [[ ! -f "$VENV_DIR/pyvenv.cfg" ]]; then
      echo "Missing virtualenv configuration: $VENV_DIR/pyvenv.cfg." >&2
      exit 1
    fi
  fi
  if [[ "$exec_start" != *"argv[]=$installed_python $APP_DIR/user_server.py "* || ! -x "$installed_python" ]]; then
    echo "$SERVICE ExecStart must run $APP_DIR/user_server.py with $installed_python." >&2
    exit 1
  fi

  local stage release src path unit python="$installed_python" port
  local deps_changed=0 unit_changed=0 restart=0 was_active=0 service_touched=0 unit_applied=0 committed=0
  local new_venv="" previous_venv=""
  local -a changes=() applied=()
  stage="$(mktemp -d)"
  release="$stage/release"

  finish_update() {
    local status=$? path restore_failed=0
    trap - EXIT
    if ((!committed && (${#applied[@]} || service_touched))); then
      set +e
      echo 'Update failed; restoring previous deployment files.' >&2
      if ((service_touched)); then systemctl stop "$SERVICE" || restore_failed=1; fi
      for path in "${applied[@]}"; do
        if [[ -e "$stage/previous/$path" || -L "$stage/previous/$path" ]]; then
          cp -a --remove-destination -- "$stage/previous/$path" "$APP_DIR/$path" || restore_failed=1
        else
          rm -f -- "$APP_DIR/$path" || restore_failed=1
        fi
      done
      if [[ -n "$previous_venv" && ( -e "$previous_venv" || -L "$previous_venv" ) ]]; then
        if [[ -L "$VENV_DIR" ]]; then rm -- "$VENV_DIR" || restore_failed=1; fi
        mv -T -- "$previous_venv" "$VENV_DIR" || restore_failed=1
      fi
      if ((unit_applied)); then
        cp -a --remove-destination -- "$stage/previous-unit" "$UNIT_PATH" || restore_failed=1
        systemctl daemon-reload || restore_failed=1
      fi
      if ((service_touched && was_active && !restore_failed)); then
        local previous_port
        previous_port="$(env_value USER_SERVER_PORT "$UNIT_PATH")"
        if ! systemctl start "$SERVICE" || ! wait_for_service "${previous_port:-9001}" "$installed_python"; then
          restore_failed=1
        fi
      fi
      if ((restore_failed)); then
        echo "Rollback incomplete. Recovery files kept at $stage." >&2
        exit 1
      fi
    fi
    if ((!committed)) && [[ -n "$new_venv" ]]; then rm -rf -- "$new_venv"; fi
    rm -rf -- "$stage"
    exit "$status"
  }
  trap finish_update EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  bash "$SOURCE_DIR/package.sh" --update --stage "$release"
  find "$release" -type f -print0 | sort -z > "$stage/files"
  while IFS= read -r -d '' src; do
    path="${src#"$release/"}"
    if [[ -f "$APP_DIR/$path" ]] && cmp -s -- "$src" "$APP_DIR/$path"; then continue; fi
    if [[ -d "$APP_DIR/$path" ]]; then
      echo "Expected a deployment file at $APP_DIR/$path" >&2
      exit 1
    fi
    changes+=("$path")
    case "$path" in
      requirments.txt) deps_changed=1; restart=1 ;;
      package.sh|deploy_user_server*.sh|debug_server.py|tool/*|systemd/*) ;;
      *) restart=1 ;;
    esac
  done < "$stage/files"

  unit="$UNIT_PATH"
  local bundled_unit="$release/systemd/$SERVICE"
  if ! cmp -s -- "$bundled_unit" "$UNIT_PATH"; then
    if ((LOCAL_TARGET)); then
      echo "Using host-specific systemd unit $UNIT_PATH (Python: $installed_python)."
    else
      unit_changed=1
      restart=1
      unit="$bundled_unit"
    fi
  fi
  if (("${#changes[@]}" == 0 && !unit_changed)); then
    echo 'No deployment files changed; restart skipped.'
    exit 0
  fi
  printf 'Changed deployment files:\n'
  if (("${#changes[@]}")); then printf '  %s\n' "${changes[@]}"; fi
  if ((unit_changed)); then printf '  %s\n' "$UNIT_PATH"; fi
  printf 'Plan: install dependencies=%s, daemon-reload=%s, restart=%s\n' \
    "$deps_changed" "$unit_changed" "$restart"

  if ((restart)); then
    local run_dir
    run_dir="$(env_value RUN_DIR "$unit")"
    if [[ -n "$run_dir" && "$run_dir" != "$APP_DIR" ]]; then
      echo "RUN_DIR must point to $APP_DIR for this installation." >&2
      exit 1
    fi
    port="$(env_value USER_SERVER_PORT "$unit")"
    "$python" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; assert 0 < int(sys.argv[1]) <= 65535' "${port:-9001}"
  fi
  if ((unit_changed)); then
    if ! grep -Fxq "WorkingDirectory=$APP_DIR" "$unit" ||
       ! grep -Fq "ExecStart=$VENV_DIR/bin/python $APP_DIR/user_server.py" "$unit"; then
      echo 'Update mode requires the existing application and virtualenv paths in the unit.' >&2
      exit 1
    fi
    systemd-analyze verify "$unit"
  fi
  if ((CHECK_ONLY)); then
    if ((restart && !deps_changed)); then check_release "$release" "$python"; fi
    if ((deps_changed)); then echo 'Import check deferred until the changed dependencies are installed.'; fi
    exit 0
  fi

  if ((deps_changed)); then
    # Resolve packages in a copy because the installed venv may also serve auth/ops.
    new_venv="$(mktemp -d "$VENV_DIR.release.XXXXXXXX")"
    cp -a --reflink=auto -- "$VENV_DIR/." "$new_venv/"
    python="$new_venv/bin/${installed_python##*/}"
    "$python" -m pip install -r "$release/requirments.txt"
    "$python" -m pip check
  fi
  if ((restart)); then check_release "$release" "$python"; fi
  if ((unit_changed)); then cp -a -- "$UNIT_PATH" "$stage/previous-unit"; fi
  for path in "${changes[@]}"; do
    if [[ -e "$APP_DIR/$path" || -L "$APP_DIR/$path" ]]; then
      mkdir -p -- "$stage/previous/$(dirname "$path")"
      cp -a -- "$APP_DIR/$path" "$stage/previous/$path"
    fi
  done
  if ((restart)) && systemctl is-active --quiet "$SERVICE"; then
    was_active=1
    service_touched=1
    systemctl stop "$SERVICE"
  fi
  for path in "${changes[@]}"; do
    applied+=("$path")
    mkdir -p -- "$APP_DIR/$(dirname "$path")"
    cp -a --remove-destination -- "$release/$path" "$APP_DIR/$path"
  done
  if ((deps_changed)); then
    previous_venv="$new_venv.previous"
    mv -T -- "$VENV_DIR" "$previous_venv"
    ln -s -- "$new_venv" "$VENV_DIR"
  fi
  if ((unit_changed)); then
    unit_applied=1
    install -m 0644 "$unit" "$UNIT_PATH"
    systemctl daemon-reload
  fi
  if ((restart)); then
    service_touched=1
    systemctl start "$SERVICE"
    wait_for_service "${port:-9001}" "$installed_python"
  fi
  committed=1
  if [[ -n "$previous_venv" ]]; then echo "Previous virtualenv kept at $previous_venv"; fi
  if ((!restart)); then echo 'Only deployment or diagnostic tools changed; restart skipped.'; fi
)
