"""洞察规则阈值运营配置（Mindora_App睡眠数据展示与分析对照规范_v3.md §4）。

配置存于 JSON 文件（默认 data/insight_rules.json，可用 INSIGHT_RULES_CONFIG_PATH
环境变量覆盖），由运营平台上传写入；user_server 按文件 mtime 热加载（无需重启，
重启当然也生效），文件内容按 key 深度合并在内置默认值（config.INSIGHT_RULES_DEFAULT）
之上——运营只需写要改的阈值，没写的项沿用内置默认；解析失败沿用上一份可用配置。
"""
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from config import Config

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))


def _config_path() -> Path:
  override = os.getenv("INSIGHT_RULES_CONFIG_PATH")
  if override:
    return Path(override)
  return Path(run_dir) / "data" / "insight_rules.json"


_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "rules": None}


def _type_compatible(default, override) -> bool:
  """覆盖值与默认值的类型是否兼容（int/float 互通；bool 不与数字互通）。"""
  if isinstance(default, bool) or isinstance(override, bool):
    return isinstance(default, bool) and isinstance(override, bool)
  if isinstance(default, (int, float)):
    return isinstance(override, (int, float))
  return isinstance(override, type(default))


def _deep_merge(base: dict, override: dict, ignored: Optional[list] = None) -> dict:
  """override 按 key 递归合并进 base。类型与默认不兼容的叶子（如把数字阈值
  写成字符串）忽略该覆盖项并记警告（同步追加到 ignored 列表）——坏配置只会让
  该项回退默认，不会弄挂规则计算。"""
  out = copy.deepcopy(base)
  for k, v in override.items():
    if isinstance(v, dict) and isinstance(out.get(k), dict):
      out[k] = _deep_merge(out[k], v, ignored)
    elif k in out and not _type_compatible(out[k], v):
      logging.warning("insight rules: ignore override %r=%r (default type %s)",
                      k, v, type(out[k]).__name__)
      if ignored is not None:
        ignored.append(k)
      continue
    else:
      out[k] = v
  return out


def _load() -> dict:
  """读取运营阈值配置；文件无变化时直接返回缓存。文件缺失/解析失败时
  回退内置默认值（首次）或上一份可用配置。"""
  path = _config_path()
  try:
    mtime = os.path.getmtime(path)
  except OSError:
    if _cache["rules"] is None:
      logging.warning("insight rules config not found: %s, using built-in defaults", path)
      _cache["rules"] = copy.deepcopy(Config.INSIGHT_RULES_DEFAULT)
    return _cache["rules"]

  with _lock:
    if _cache["path"] == str(path) and _cache["mtime"] == mtime and _cache["rules"] is not None:
      return _cache["rules"]
    try:
      raw = json.loads(path.read_text(encoding="utf-8"))
      if not isinstance(raw, dict):
        raise ValueError("insight rules config must be a JSON object")
      rules = _deep_merge(Config.INSIGHT_RULES_DEFAULT, raw)
      _cache.update({"path": str(path), "mtime": mtime, "rules": rules})
      logging.info("insight rules config reloaded: %s (keys=%s)", path, sorted(rules.keys()))
    except (json.JSONDecodeError, ValueError) as e:
      logging.error("insight rules config parse failed (%s), keep last good config: %s", path, e)
      if _cache["rules"] is None:
        _cache["rules"] = copy.deepcopy(Config.INSIGHT_RULES_DEFAULT)
    return _cache["rules"]


def get_insight_rules() -> dict:
  """当前生效的阈值配置（内置默认 ← 运营文件覆盖）。"""
  return _load()


def save_insight_rules(rules: dict) -> tuple[bool, str]:
  """运营平台上传全量阈值配置：与内置默认合并后原子写回文件（read-modify-write
  同构，直接覆盖整个 rules 节），mtime 变化触发热加载 → 下一次规则计算生效。"""
  if not isinstance(rules, dict):
    return False, "rules 必须是 JSON object"
  ignored: list = []
  merged = _deep_merge(Config.INSIGHT_RULES_DEFAULT, rules, ignored)
  path = _config_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp_path = path.with_suffix(path.suffix + ".tmp")
  tmp_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
  tmp_path.replace(path)
  logging.info("insight rules config saved -> %s", path)
  # 立即触发一次热加载（不等下一次请求），让缓存与磁盘一致
  with _lock:
    _cache["mtime"] = None
  _load()
  msg = f"saved {path}"
  if ignored:
    msg += f"；{len(ignored)} 项类型不兼容已回退默认: {', '.join(ignored)}"
  return True, msg


def insight_rules_status() -> dict:
  """配置状态快照（启动自检/运营后台展示用）。"""
  rules = _load()
  path = _config_path()
  return {
    "path": str(path),
    "exists": path.is_file(),
    "keys": sorted(rules.keys()),
    "effective": rules,
  }
