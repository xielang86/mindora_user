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
#
# 新增运行依赖文件时，必须同步加进下面的清单（--check 会帮你发现漏加）。

set -euo pipefail
PUBLIC_KEY_SOURCE="$(realpath -m "${PUBLIC_KEY_SOURCE:-$(dirname "$0")/jwt_public.pem}")"
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
DATA_FILES=(
  data/popup_survey_config.json   # 弹窗/问卷运营配置（漏了 → /popup 恒空）
  data/reco_candidates.json       # SOP 推荐候选集
  data/content_tags/recommendation_rules.json
  data/content_tags/sop_source.json
  data/content_tags/audio_source.json
  certs/AppleRootCA-G3.cer        # App Store Server API 验证根证书
)
DB_FILES=(
  db/knowledge_base.md            # 推荐 prompt 知识库
  db/topology.md
)
OPS_FILES=(
  requirments.txt
  deploy_user_server.sh
  systemd/mindora-user.service
  systemd/mindora.env.example
  systemd/mindora-user.env.example
  systemd/mindora-auth.env.example
  debug_server.py             # 调试页需读取按启动时间轮换的 LLM 日志
  tool/migrate_profile_v2.py   # 停服后用于旧单 key 画像离线迁移与校验
  tool/migrate_sleep_scores.py # 睡眠分数离线回填与校验
  tool/rebuild_mindora_records.py # 运营按保留的 plays 与睡眠阶段重建场景记录
  tool/refresh_user_llm.py    # 管理员触发指定用户的一次 LLM 全量刷新
)

OPTIONAL_FILES=(
  data/user_profiles.txt          # txt_json 模式的种子数据（leveldb 模式用不到）
  uuid_mac_time.enc               # 设备 uuid 加密文件（端上部署用）
)

REQUIRED=( "${PY_MODULES[@]}" "${DATA_FILES[@]}" "${DB_FILES[@]}" "${OPS_FILES[@]}" )

# ── 校验 ──────────────────────────────────────────────────
missing=0
if [[ ! -s "$PUBLIC_KEY_SOURCE" ]]; then
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

if [[ "${1:-}" == "--check" ]]; then
  echo "清单校验通过：${#REQUIRED[@]} 个文件 + ${#PACKAGES[@]} 个包目录齐全。"
  exit 0
fi

# ── 打包 ──────────────────────────────────────────────────
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/user_server"
mkdir -p "$DEST"
cp "$PUBLIC_KEY_SOURCE" "$DEST/jwt_public.pem"

for f in "${REQUIRED[@]}"; do
  mkdir -p "$DEST/$(dirname "$f")"
  cp "$f" "$DEST/$f"
done
for d in "${PACKAGES[@]}"; do
  # 只拷源码，排除 __pycache__
  mkdir -p "$DEST/$d"
  tar -C "$d" --exclude='__pycache__' --exclude='*.py[cod]' -cf - . | tar -C "$DEST/$d" -xf -
done
for f in "${OPTIONAL_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    mkdir -p "$DEST/$(dirname "$f")"
    cp "$f" "$DEST/$f"
  else
    echo "skip optional: $f"
  fi
done

mkdir -p dist
OUT="dist/user_server_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$OUT" -C "$STAGE" user_server

echo "打包完成: $OUT"
tar -tzf "$OUT" | sort
