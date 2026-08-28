"""query_profile 时段黑名单（uid 级流控）。

场景：个别设备/App 异常高频全量拉取画像时，把该 uid 拉进黑名单，只允许它在
指定时段 query_profile，时段外直接 403；update_profile / query_revision 不受影响
（探测通道留着，窗口一到设备会自动补拉）。

配置文件（默认 data/profile_query_blacklist.json，可用环境变量
PROFILE_QUERY_BLACKLIST_PATH 覆盖），按文件 mtime 热加载，解析失败沿用上一份可用配置：

{
  "blacklist": {
    "uid-a": {
      "allow_windows": ["02:00-06:00", "22:30-23:59"],   // 允许拉取的时段，支持跨零点（"22:00-02:00"）
      "timezone": "Asia/Shanghai"                         // 可选，默认 UTC
    },
    "uid-b": {"allow_windows": []}                          // 空数组 = 全天禁止
  }
}

名单外的 uid 不受影响；配置文件缺失/损坏视为空名单（全部放行）。
"""
import datetime
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))

_DEFAULT_PATH = "data/profile_query_blacklist.json"

_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "blacklist": {}}


def _config_path() -> Path:
  override = os.getenv("PROFILE_QUERY_BLACKLIST_PATH")
  if override:
    return Path(override)
  return Path(run_dir) / _DEFAULT_PATH


def _parse_hhmm(text: str) -> Optional[int]:
  """"HH:MM" → 当天分钟数；非法返回 None。"""
  try:
    hh, mm = text.strip().split(":")
    hh, mm = int(hh), int(mm)
    if 0 <= hh <= 23 and 0 <= mm <= 59:
      return hh * 60 + mm
  except (ValueError, AttributeError):
    pass
  return None


def _validate_entry(uid: str, entry: dict) -> Optional[dict]:
  """校验单条黑名单配置；返回 {timezone: tzinfo, windows: [(start_min, end_min)]}。"""
  if not isinstance(entry, dict):
    logging.error("blacklist entry for uid=%s must be object, ignored", uid)
    return None
  tz_name = entry.get("timezone") or "UTC"
  try:
    tz = ZoneInfo(tz_name)
  except Exception:
    logging.error("blacklist uid=%s invalid timezone %r, fallback UTC", uid, tz_name)
    tz = datetime.timezone.utc
  windows = []
  for w in entry.get("allow_windows") or []:
    if not isinstance(w, str) or "-" not in w:
      logging.error("blacklist uid=%s invalid window %r, skipped", uid, w)
      continue
    start_s, end_s = w.split("-", 1)
    start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    if start is None or end is None:
      logging.error("blacklist uid=%s invalid window %r, skipped", uid, w)
      continue
    windows.append((start, end))
  return {"timezone": tz, "windows": windows}


def _load() -> dict:
  """按 mtime 热加载；返回 {uid: entry}。文件缺失/损坏沿用上一份（初始为空名单）。"""
  path = _config_path()
  try:
    mtime = path.stat().st_mtime
  except OSError:
    with _lock:
      return _cache["blacklist"] if _cache["path"] == str(path) else {}
  with _lock:
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
      return _cache["blacklist"]
  try:
    raw = json.loads(path.read_text(encoding="utf-8"))
    blacklist = {}
    for uid, entry in (raw.get("blacklist") or {}).items():
      valid = _validate_entry(uid, entry)
      if valid is not None:
        blacklist[uid] = valid
  except Exception as e:
    logging.error("load profile query blacklist failed: %s (keep previous)", e)
    with _lock:
      return _cache["blacklist"] if _cache["path"] == str(path) else {}
  with _lock:
    _cache.update({"path": str(path), "mtime": mtime, "blacklist": blacklist})
  logging.info("profile query blacklist loaded: %d uids from %s", len(blacklist), path)
  return blacklist


def _reset_cache_for_test() -> None:
  with _lock:
    _cache.update({"path": None, "mtime": None, "blacklist": {}})


def is_query_allowed(uid: str, now: Optional[datetime.datetime] = None) -> bool:
  """uid 当前是否允许 query_profile。名单外恒 True；名单内按时段判定（空窗口=全天禁止）。"""
  entry = _load().get(uid)
  if entry is None:
    return True
  if not entry["windows"]:
    return False
  local = (now or datetime.datetime.now(entry["timezone"])).astimezone(entry["timezone"])
  minutes = local.hour * 60 + local.minute
  for start, end in entry["windows"]:
    if start <= end:  # 普通时段 02:00-06:00
      if start <= minutes <= end:
        return True
    else:  # 跨零点 22:00-02:00
      if minutes >= start or minutes <= end:
        return True
  return False
