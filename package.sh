#!/usr/bin/bash
# package.sh — 构建 user_server 部署包（显式清单，缺文件即失败）。
#
# 背景：旧的 user_server.tar.gz 漏了 data/popup_survey_config.json，线上 /popup 恒空
# 且配置缺失只在首次请求时打一行 warning，极难发现。本脚本改为显式列出全部运行
# 所需文件，任何一个缺失都直接报错退出，杜绝"打出去的包过时"。
#
# 用法：
#   ./package.sh                 # 产出 dist/user_server_<日期>.tar.gz
#   ./package.sh --check         # 只校验清单完整性，不打包
#   ./package.sh --update --stage DIR  # 已安装机器用的增量文件集
#
# 新增运行依赖文件时，必须同步加进下面的清单（--check 会帮你发现漏加）。

set -euo pipefail
UPDATE=0
CHECK=0
DEST=""
while (($#)); do
  case "$1" in
    --update) UPDATE=1; shift ;;
    --check) CHECK=1; shift ;;
    --stage)
      (($# >= 2)) || exit 2
      DEST="$(realpath -m "$2")"
      shift 2
      ;;
    *) echo "Unknown package option: $1" >&2; exit 2 ;;
  esac
done
if ((!UPDATE)); then
  PUBLIC_KEY_SOURCE="$(realpath -m "${PUBLIC_KEY_SOURCE:-$(dirname "$0")/jwt_public.pem}")"
fi
cd "$(dirname "$0")"

# ── 运行所需文件清单（相对仓库根） ──────────────────────────
PY_MODULES=(
  user_server.py
  user_profile.py
  config.py
  logger.py
  auth.py
  profile_service.py
  profile_repository.py
  engagement_service.py
  analysis_content.py
  analysis_builders.py
  analysis_score.py
  sleep_score_cache.py
  analysis_fallback.py
  insight_rules.py
  insight_rules_config.py
  sleep_metrics.py
  sleep_session_builder.py
  sleep_plan_service.py
  profile_query_gate.py
  sop_tag_profile.py
  sop_ranking.py
  content_tags.py
  ops_config.py
  iot_server.py
)
PACKAGES=(
  common          # jwt_keys / util
  llm             # ark_chat / analysis / reco
  uid             # uuid
)
READ_ONLY_DATA=(
  data/reco_candidates.json       # SOP 推荐候选集
  data/content_tags/recommendation_rules.json
  data/content_tags/sop_source.json
  data/content_tags/audio_source.json
  db/knowledge_base.md            # 推荐 prompt 知识库
  db/topology.md
)
OPS_FILES=(
  requirments.txt
  deploy_user_server.sh
  systemd/mindora-user.service
  debug_server.py             # 调试页需读取按启动时间轮换的 LLM 日志
  tool/migrate_profile_v2.py   # 停服后用于旧单 key 画像离线迁移与校验
  tool/migrate_sleep_scores.py # 睡眠分数离线回填与校验
  tool/rebuild_mindora_records.py # 运营按保留的 plays 与睡眠阶段重建场景记录
  tool/refresh_user_llm.py    # 管理员触发指定用户的一次 LLM 全量刷新
)

# 运行时或运维维护这些文件。首次安装可补齐模板，更新包不携带它们。
# 洞察覆盖、画像黑名单、数据库、上传资源、日志和设备 UUID 始终由目标机维护。
INSTALL_FILES=(
  data/popup_survey_config.json
  certs/AppleRootCA-G3.cer
  systemd/mindora.env.example
  systemd/mindora-user.env.example
  systemd/mindora-auth.env.example
)

REQUIRED=( "${PY_MODULES[@]}" "${READ_ONLY_DATA[@]}" "${OPS_FILES[@]}" )
if ((UPDATE)); then
  REQUIRED+=(package.sh deploy_user_server_update.sh)
else
  REQUIRED+=("${INSTALL_FILES[@]}")
fi

# ── 校验 ──────────────────────────────────────────────────
missing=0
if ((!UPDATE)) && [[ ! -s "$PUBLIC_KEY_SOURCE" ]]; then
  echo "MISSING or empty JWT public key: $PUBLIC_KEY_SOURCE" >&2
  missing=1
fi
for f in "${REQUIRED[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "MISSING(required): $f" >&2
    missing=1
  fi
done
for d in "${PACKAGES[@]}"; do
  if [[ ! -d "$d" ]]; then
    echo "MISSING(required dir): $d" >&2
    missing=1
  fi
done
if [[ $missing -ne 0 ]]; then
  echo "清单校验失败，请先补齐缺失文件。" >&2
  exit 1
fi

if ((CHECK)); then
  echo "清单校验通过：${#REQUIRED[@]} 个文件 + ${#PACKAGES[@]} 个包目录齐全。"
  exit 0
fi

# ── 打包 ──────────────────────────────────────────────────
if [[ -n "$DEST" ]]; then
  if [[ -d "$DEST" ]] && [[ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "Staging directory must be empty: $DEST" >&2
    exit 1
  fi
  ARCHIVE=0
else
  ARCHIVE=1
  STAGE=$(mktemp -d)
  trap 'rm -rf "$STAGE"' EXIT
  DEST="$STAGE/user_server"
fi
mkdir -p "$DEST"
if ((!UPDATE)); then
  cp "$PUBLIC_KEY_SOURCE" "$DEST/jwt_public.pem"
fi

for f in "${REQUIRED[@]}"; do
  mkdir -p "$DEST/$(dirname "$f")"
  cp "$f" "$DEST/$f"
done
for d in "${PACKAGES[@]}"; do
  # 包目录内只分发 Python 源文件，避免顺手打包本地配置和缓存。
  mkdir -p "$DEST/$d"
  (cd "$d" && find . -name '__pycache__' -prune -o -type f -name '*.py' -print0 |
    tar -cf - --null -T -) |
    tar -C "$DEST/$d" --no-same-owner --no-same-permissions -xf -
done

if ((!ARCHIVE)); then
  exit 0
fi
mkdir -p dist
PREFIX=user_server
if ((UPDATE)); then PREFIX=user_server_update; fi
OUT="dist/${PREFIX}_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$OUT" -C "$STAGE" user_server

echo "打包完成: $OUT"
tar -tzf "$OUT" | sort
