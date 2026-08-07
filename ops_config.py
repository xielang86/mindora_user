"""弹窗 / 问卷运营配置（tanchuang_suvey.md）。

配置存于 JSON 文件（默认 data/popup_survey_config.json，可用 POPUP_SURVEY_CONFIG_PATH
环境变量覆盖），由运营后台线上写入；user_server 每次请求按文件 mtime 检查更新并热加载，
解析失败时沿用上一份可用配置。
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from config import Config

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))


def _i18n(entry: dict, language: str) -> dict:
  langs = entry.get("i18n") or {}
  if language in langs:
    return langs[language]
  if "zh-Hans" in langs:
    return langs["zh-Hans"]
  return next(iter(langs.values()), {})


def _ops_config_path() -> Path:
  override = os.getenv("POPUP_SURVEY_CONFIG_PATH")
  if override:
    return Path(override)
  return Path(run_dir) / Config.POPUP_SURVEY_CONFIG_PATH


_ops_config_lock = threading.Lock()
_ops_config_cache: dict = {"path": None, "mtime": None, "popups": [], "surveys": {}}

# 配置成功热加载后的回调（fn(popups, surveys)）。用于消息目录等派生状态同步，
# 由 engagement_service 在初始化时注册；单个 hook 失败不影响加载主流程。
_reload_hooks: list = []


def register_reload_hook(fn) -> None:
  _reload_hooks.append(fn)

# action_type="route" 时 action_payload.route 的白名单（tanchuang_suvey.md「route 候选常量」，与客户端硬编码一致；
# 新增路由必须先发客户端版本，服务端才能下发）
POPUP_ROUTE_WHITELIST = {
  # 一级 Tab
  "home", "sleep", "explore", "store",
  # 二级页面
  "subscription", "redeem", "footprint", "device", "profile", "settings", "faq", "notifications",
}

# query_popups 响应 data.next_query_after（客户端轮询间隔）的有效区间：60s ~ 24h，超出夹到边界；
# 运营配置不填则服务端不下发该字段，客户端用默认 300s
NEXT_QUERY_AFTER_MIN = 60
NEXT_QUERY_AFTER_MAX = 24 * 3600


def _clamp_next_query_after(value) -> Optional[int]:
  """把运营配置的 next_query_after 夹到 [60s, 24h]；未配置/非法值返回 None（不下发）。"""
  if value is None:
    return None
  try:
    seconds = int(value)
  except (TypeError, ValueError):
    logging.error("invalid next_query_after=%r in ops config, ignored", value)
    return None
  return max(NEXT_QUERY_AFTER_MIN, min(NEXT_QUERY_AFTER_MAX, seconds))


def _validate_popups(popups: list) -> list:
  """校验运营配置的弹窗：route 动作的路由必须在白名单内（忽略大小写与首尾空格），
  非法条目丢弃并记日志，避免坏配置下发到客户端。"""
  valid = []
  for popup in popups:
    if popup.get("action_type") == "route":
      payload = popup.get("action_payload") or {}
      route = str(payload.get("route") or "").strip().lower()
      if route not in POPUP_ROUTE_WHITELIST:
        logging.error(
          "popup %s dropped: route %r not in whitelist",
          popup.get("popup_id"), payload.get("route"),
        )
        continue
      payload["route"] = route
      popup["action_payload"] = payload
    valid.append(popup)
  return valid


def find_dangling_survey_refs(popups: list, surveys: dict) -> list[str]:
  """弹窗 action_payload.survey_id 必须在 surveys 字典里，否则用户点弹窗拉题会 404。

  返回悬空引用列表（"popup_id→survey_id"），空列表表示一致。
  """
  dangling = []
  for popup in popups:
    sid = (popup.get("action_payload") or {}).get("survey_id")
    if sid and sid not in surveys:
      dangling.append(f"{popup.get('popup_id')}→{sid}")
  return dangling


def _load_ops_config() -> tuple[list, dict, Optional[int]]:
  """读取弹窗/问卷运营配置 (popups, surveys, next_query_after)。文件无变化时直接返回缓存。"""
  path = _ops_config_path()
  try:
    mtime = os.path.getmtime(path)
  except OSError:
    if _ops_config_cache["path"] is None:
      logging.warning("ops config not found: %s", path)
    return _ops_config_cache["popups"], _ops_config_cache["surveys"], _ops_config_cache.get("next_query_after")

  with _ops_config_lock:
    if _ops_config_cache["path"] == str(path) and _ops_config_cache["mtime"] == mtime:
      return _ops_config_cache["popups"], _ops_config_cache["surveys"], _ops_config_cache.get("next_query_after")
    try:
      raw = json.loads(path.read_text(encoding="utf-8"))
      popups = raw.get("popups") or []
      surveys = raw.get("surveys") or {}
      if not isinstance(popups, list) or not isinstance(surveys, dict):
        raise ValueError("popups must be a list and surveys must be a dict")
      popups = _validate_popups(popups)
      dangling = find_dangling_survey_refs(popups, surveys)
      if dangling:
        # 文件能解析但引用悬空：弹窗能下发，用户点击后 query_survey 必 404
        logging.error("ops config dangling survey refs (点弹窗拉题会 404): %s", dangling)
      next_query_after = _clamp_next_query_after(raw.get("next_query_after"))
    except (json.JSONDecodeError, ValueError) as e:
      logging.error("ops config parse failed (%s), keep last good config: %s", path, e)
      return _ops_config_cache["popups"], _ops_config_cache["surveys"], _ops_config_cache.get("next_query_after")

    _ops_config_cache.update({
      "path": str(path), "mtime": mtime, "popups": popups, "surveys": surveys,
      "next_query_after": next_query_after,
    })
    logging.info(
      "ops config reloaded: %s (popups=%d surveys=%d next_query_after=%s)",
      path, len(popups), len(surveys), next_query_after,
    )
    for hook in _reload_hooks:
      try:
        hook(popups, surveys)
      except Exception as e:
        logging.error("ops config reload hook failed: %s", e)
    return popups, surveys, next_query_after


def ops_config_status() -> dict:
  """启动自检用：触发一次加载（或命中缓存）并返回配置状态快照。"""
  popups, surveys, next_query_after = _load_ops_config()
  path = _ops_config_path()
  return {
    "path": str(path),
    "exists": path.is_file(),
    "popups": len(popups),
    "surveys": len(surveys),
    "next_query_after": next_query_after,
    "dangling_survey_refs": find_dangling_survey_refs(popups, surveys),
  }
