"""profile_service.py — 用户画像的存储、行为聚合与更新编排。

从 user_server.py 拆出（原 UserProfileServ 的职责A/B + 更新路径）：
  - 存储 CRUD（leveldb / txt_json 双模式，带锁）
  - behaviors 合并、mindora_record 聚合、场景统计
  - update_profile / update_profile_basic / update_profile_llm 三条更新路径

LLM 内容生成（analysis_content）与 engagement（弹窗/问卷/足迹）已拆为独立服务，
这里以门面委托保留原有 UserProfileServ 的方法签名，调用方零改动。
"""
import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional, List
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from llm import RecommendationEngine
try:
  import plyvel
except ImportError:
  plyvel = None

from analysis_content import AnalysisContentService
from common import util
from config import Config
from engagement_service import EngagementService
from llm import SleepAnalysisLLM
from user_profile import UserProfile, SleepScenario, Profile

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 100
  # sleep_data 保留条数：与分析报告日级保留一致（日级 30）
  MAX_SLEEP_DATA_LEN = 30

  # ── 健康数据口径版本（健康数据同步接口_0814.md §8）─────────────────────
  # v2 新增/改名的 key：一晚只有几~几十个点（已裁剪到睡眠跨度），上限对齐
  # md 客户端单切片上限 5000（约覆盖 100 晚），否则对账窗口稍长就被截断失真。
  # 未列出的 key（v1 全天序列、plays/clicks 等）沿用 MAX_BEHAVIOR_LEN。
  HEALTH_V2_BEHAVIOR_KEYS = {
    "sleep_heart_rate_min", "sleep_heart_rate_max",
    "sleep_heart_rate_variability_sdnn", "sleep_respiratory_rate",
    "sleep_body_temperature",
    "sleep_stage_unspecified", "sleep_stage_awake", "sleep_in_bed",
  }
  HEALTH_V2_BEHAVIOR_CAP = 5000
  # v1→v2 被改名或改语义的 key：v2 批次覆盖某天时，清除这些 key 当天的旧样本，
  # 使该天"现存数据"收敛为单一版本（md §8.4 同天多版本取最低的语义才能闭环）。
  HEALTH_V1_DEPRECATED_KEYS = {
    "heart_rate", "heart_rate_variability_sdnn", "respiratory_rate",
    "body_temperature", "sleep_stage_light",
  }
  # 参与自然日登记/对账的健康指标 key（v1+v2；plays/clicks 等交互行为不算健康数据）。
  # deep/rem/resting/wrist 在 v1/v2 名字与语义均未变，两个版本都算。
  HEALTH_BEHAVIOR_KEYS = HEALTH_V2_BEHAVIOR_KEYS | HEALTH_V1_DEPRECATED_KEYS | {
    "resting_heart_rate", "sleeping_wrist_temperature",
    "sleep_stage_deep", "sleep_stage_rem",
  }

  # -------------------- 门面：委托给拆分后的子服务 --------------------
  _INSIGHT_MODULE_KEYS = AnalysisContentService._INSIGHT_MODULE_KEYS
  _analysis_specs_for_today = staticmethod(AnalysisContentService._analysis_specs_for_today)
  _upsert_analysis_report = staticmethod(AnalysisContentService._upsert_analysis_report)
  _find_analysis_report = staticmethod(AnalysisContentService._find_analysis_report)
  _visible_insight_dict = staticmethod(AnalysisContentService._visible_insight_dict)

  @property
  def content(self) -> AnalysisContentService:
    """LLM 分析内容服务（惰性创建；llm 通过回调实时读取，兼容测试替换 self.llm）。"""
    if "_content" not in self.__dict__:
      self.__dict__["_content"] = AnalysisContentService(lambda: getattr(self, "llm", None))
    return self.__dict__["_content"]

  @property
  def engagement(self) -> EngagementService:
    """弹窗/问卷/足迹服务（惰性创建，兼容 __new__ 构造的测试实例）。"""
    if "_engagement" not in self.__dict__:
      self.__dict__["_engagement"] = EngagementService(self)
    return self.__dict__["_engagement"]

  def calc_sleep_insight(self, uid: str, profile: UserProfile):
    return self.content.calc_sleep_insight(uid, profile)

  def calc_analysis_reports(self, uid: str, profile: UserProfile, language: Optional[str] = None):
    return self.content.calc_analysis_reports(uid, profile, language)

  def query_popups(self, uid: str, language: str, placement: str = "home") -> dict:
    return self.engagement.query_popups(uid, language, placement)

  def query_message_history(self, uid: str, language: str, popup_ids) -> list:
    return self.engagement.query_message_history(uid, language, popup_ids)

  def report_popup_event(self, uid: str, popup_id: str, event: str, event_at: int) -> bool:
    return self.engagement.report_popup_event(uid, popup_id, event, event_at)

  def get_survey(self, survey_id: str, language: str):
    return self.engagement.get_survey(survey_id, language)

  def submit_survey(self, uid: str, data, email=None):
    return self.engagement.submit_survey(uid, data, email=email)

  def list_survey_records(self, survey_id=None) -> list:
    return self.engagement.list_survey_records(survey_id)

  def merge_footprint_days(self, uid: str, days: list) -> int:
    return self.engagement.merge_footprint_days(uid, days)

  def query_footprint(self, uid: str, scope: str, year: int, month, timezone: str) -> dict:
    return self.engagement.query_footprint(uid, scope, year, month, timezone)

  # -------------------- 存储与初始化 --------------------
  def __init__(self, llm: Optional[SleepAnalysisLLM] = None):
    self.lock = threading.RLock()
    self.storage_mode = (Config.USER_PROFILE_STORAGE_MODE or "leveldb").strip().lower()
    self.db = None
    self.json_path = Path(run_dir) / Config.USER_PROFILE_JSON_PATH
    self.text_profiles: dict[str, Any] = {}
    self.llm = llm or SleepAnalysisLLM()

    if self.storage_mode == "leveldb":
      if plyvel is None:
        raise ImportError("plyvel is required when USER_PROFILE_STORAGE_MODE=leveldb")
      # 初始化LevelDB（若路径不存在则自动创建）
      self.db = plyvel.DB(f"{run_dir}/{Config.DB_PATH}", create_if_missing=True)
    elif self.storage_mode not in {"txt_json", "json_txt", "json"}:
      raise ValueError(f"unsupported USER_PROFILE_STORAGE_MODE: {self.storage_mode}")
    else:
      self.text_profiles = self._load_profiles_from_text_unlocked()
      logging.info(f"preloaded {len(self.text_profiles)} user profiles from {self.json_path}")

    logging.info(f"user profile storage mode={self.storage_mode}")

  def _profile_to_json_data(self, profile: UserProfile) -> dict:
    return profile.model_dump(mode="json")

  def _load_profiles_from_text_unlocked(self) -> dict[str, Any]:
    if not self.json_path.exists():
      return {}

    raw_text = self.json_path.read_text(encoding="utf-8").strip()
    if not raw_text:
      return {}

    profiles = json.loads(raw_text)
    if not isinstance(profiles, dict):
      raise ValueError(f"profile json file should be a dict keyed by uid: {self.json_path}")
    return profiles

  def _save_profiles_to_text_unlocked(self, profiles: dict[str, Any]):
    self.json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = self.json_path.with_suffix(self.json_path.suffix + ".tmp")
    tmp_path.write_text(
      json.dumps(profiles, ensure_ascii=False, indent=2),
      encoding="utf-8",
    )
    tmp_path.replace(self.json_path)

  def _flush_text_profiles_unlocked(self):
    self._save_profiles_to_text_unlocked(self.text_profiles)

  def get_profile(self, uid: str) -> Optional[UserProfile]:
    """读取单个用户画像"""
    if not uid or not isinstance(uid, str):
      logging.error(f"erro uid : {uid}")
      return None

    with self.lock:
      if self.storage_mode == "leveldb":
        data = self.db.get(uid.encode('utf-8'))  # LevelDB键值为bytes类型
        if data:
          logging.info("get from leveldb uid=%s size=%d bytes", uid, len(data))
          return UserProfile.model_validate(json.loads(data.decode('utf-8')))
        logging.info("get from leveldb uid=%s not found", uid)
        return None

      data = self.text_profiles.get(uid)
      logging.info("get from json txt uid=%s found=%s size=%d", uid, data is not None, len(json.dumps(data)) if data else 0)
      if data is not None:
        return UserProfile.model_validate(data)
      return None

  def save_profile(self, uid: str, profile: UserProfile):
    """将单个用户的画像写入持久化存储"""
    with self.lock:
      if self.storage_mode == "leveldb":
        data = json.dumps(self._profile_to_json_data(profile)).encode('utf-8')
        self.db.put(uid.encode('utf-8'), data)
        return

      self.text_profiles[uid] = self._profile_to_json_data(profile)
      self._flush_text_profiles_unlocked()

  # -------------------- 全局 KV（非 per-user 数据） --------------------
  # 用途：消息目录 _meta:msg:<popup_id>（popup_survey.md 2.1 历史消息恢复）等。
  # "_meta:" 前缀保证与 uid 不冲突；txt_json 模式下与用户数据同文件共存（按 key 隔离）。

  def get_global(self, key: str) -> Optional[Any]:
    """点查全局 KV。"""
    with self.lock:
      if self.storage_mode == "leveldb":
        data = self.db.get(key.encode("utf-8"))
        return json.loads(data.decode("utf-8")) if data else None
      return self.text_profiles.get(key)

  def put_global(self, key: str, value: Any) -> None:
    """写全局 KV（单条）。"""
    with self.lock:
      if self.storage_mode == "leveldb":
        self.db.put(key.encode("utf-8"), json.dumps(value, ensure_ascii=False).encode("utf-8"))
        return
      self.text_profiles[key] = value
      self._flush_text_profiles_unlocked()

  def iter_global_prefix(self, prefix: str) -> list:
    """按前缀迭代全局 KV，返回 [(key, value), ...]（LevelDB key 有序，顺序遍历）。"""
    with self.lock:
      if self.storage_mode == "leveldb":
        out = []
        for k, v in self.db.iterator(prefix=prefix.encode("utf-8")):
          out.append((k.decode("utf-8"), json.loads(v.decode("utf-8"))))
        return out
      return [(k, v) for k, v in self.text_profiles.items() if k.startswith(prefix)]

  def _get_or_create_profile_unlocked(self, uid: str) -> UserProfile:
    """读取画像，不存在则返回一个新画像对象（调用方负责 save_profile）。"""
    profile = self.get_profile(uid)
    return profile if profile is not None else UserProfile()

  # -------------------- 行为聚合 --------------------
  def _merge_profile(self, old_profile, new_profile):
    return old_profile

  @staticmethod
  def _behavior_counts(behaviors: dict) -> dict:
    """Return a compact count summary for logging."""
    return {k: len(v) if isinstance(v, list) else v for k, v in behaviors.items()}

  def _merge_behavior(self, old_behaviors, new_behaviors):
    # merge sort, consider the old ones is sorted already
    logging.info(
      "merge behavior counts before=%s new=%s",
      self._behavior_counts(old_behaviors),
      self._behavior_counts(new_behaviors),
    )
    for behavior_type, values in new_behaviors.items():
      if not isinstance(values, list) or not values:
        # Skip empty updates to avoid wiping existing data (e.g. from remote sync
        # or compact query_profile responses). If a caller truly wants to clear a
        # behavior list, it should explicitly send a deletion marker instead.
        continue
      values.sort(key=lambda x:x[0])
      if behavior_type in old_behaviors:
        old_behaviors[behavior_type].sort(key=lambda x:x[0])
        old_behaviors[behavior_type]= util.merge_two_sorted_dedup(old_behaviors[behavior_type], values)
      else:
        old_behaviors[behavior_type] = values

      cap = (UserProfileServ.HEALTH_V2_BEHAVIOR_CAP
             if behavior_type in UserProfileServ.HEALTH_V2_BEHAVIOR_KEYS
             else UserProfileServ.MAX_BEHAVIOR_LEN)
      if len(old_behaviors[behavior_type]) > cap:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][-cap:]

    logging.info("after update behavior counts=%s", self._behavior_counts(old_behaviors))
    return old_behaviors

  # -------------------- 最近请求环境（每日 LLM 触发门口径） --------------------
  @staticmethod
  def _note_request_meta(profile: UserProfile, timezone: Optional[str], language: Optional[str],
                         fill_only: bool = False) -> bool:
    """把请求携带的 timezone/language 记到画像上；返回是否有变化。

    fill_only=True（/analysis 读路径）只补缺——AnalysisData 的 tz/lang 有默认值
    （"UTC"/"en"），无法区分"客户端显式传 UTC"与"老客户端没传"，不能覆盖
    update 路径记下的真实值。
    """
    changed = False
    if timezone and (not fill_only or not profile.last_request_timezone) \
        and profile.last_request_timezone != timezone:
      profile.last_request_timezone = timezone
      changed = True
    if language and (not fill_only or not profile.last_request_language) \
        and profile.last_request_language != language:
      profile.last_request_language = language
      changed = True
    if changed:
      profile.last_request_at = int(time.time())
    return changed

  def note_request_meta(self, uid: str, timezone: Optional[str], language: Optional[str],
                        fill_only: bool = True) -> None:
    """读路径（/analysis）登记请求环境：只在画像缺省时补缺，有变化才落盘。"""
    with self.lock:
      profile = self.get_profile(uid)
      if profile is None:
        return
      if self._note_request_meta(profile, timezone, language, fill_only=fill_only):
        self.save_profile(uid, profile)

  def list_uids(self) -> list:
    """全部用户 uid（兜底扫描用）；排除 "_meta:" 前缀的全局 KV。"""
    with self.lock:
      if self.storage_mode == "leveldb":
        return [
          k.decode("utf-8") for k, _v in self.db.iterator()
          if not k.startswith(b"_meta:")
        ]
      return [k for k in self.text_profiles if not k.startswith("_meta:")]

  # -------------------- 健康数据口径版本（健康数据同步接口_0814.md §8）--------------------
  @staticmethod
  def _resolve_tz(tz_name: Optional[str], warn: bool = True) -> datetime.tzinfo:
    """请求携带的 timezone → tzinfo；缺失/非法时回退 UTC。warn=False 用于高频读路径（不刷日志）。"""
    if tz_name:
      try:
        return ZoneInfo(tz_name)
      except Exception:
        if warn:
          logging.warning("invalid timezone %r, fallback to UTC", tz_name)
    elif warn:
      logging.warning("health sync without timezone, fallback to UTC")
    return datetime.timezone.utc

  @staticmethod
  def _health_day(ts: Any, tz: datetime.tzinfo) -> Optional[str]:
    """Unix 秒时间戳 → 该时区下的自然日 yyyy-MM-dd；非法输入返回 None。"""
    try:
      return datetime.datetime.fromtimestamp(int(ts), tz).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
      return None

  def _apply_health_schema_update(
    self,
    profile: UserProfile,
    new_profile: UserProfile,
    health_schema_version: Optional[int],
    timezone: Optional[str],
  ) -> None:
    """按本批 behaviors 覆盖的自然日登记口径版本；v2 批次清除当天 v1 废弃 key 的旧样本。

    版本语义（md §8.4）：某天现存数据的版本，多版本取最低。v2 批次 purge 后该天
    现存数据只剩 v2，故直接登记本批版本；v1 批次（缺省=1）只登记，不 purge——
    若该天已有 v2 数据，v1 样本混入后版本回落 1，下次 v2 批次再 purge 收敛。
    """
    version = health_schema_version if health_schema_version else 1
    if version > Config.HEALTH_SCHEMA_VERSION:
      logging.warning(
        "health_schema_version %s newer than server-known %s",
        version, Config.HEALTH_SCHEMA_VERSION,
      )
    tz = self._resolve_tz(timezone)

    days: set[str] = set()
    for key, values in (new_profile.behaviors or {}).items():
      if key not in self.HEALTH_BEHAVIOR_KEYS or not isinstance(values, list):
        continue
      for item in values:
        if isinstance(item, (list, tuple)) and item:
          day = self._health_day(item[0], tz)
          if day:
            days.add(day)
    if not days:
      return

    if version >= 2:
      for key in self.HEALTH_V1_DEPRECATED_KEYS:
        samples = profile.behaviors.get(key)
        if not samples:
          continue
        kept = [s for s in samples
                if not (isinstance(s, (list, tuple)) and s and self._health_day(s[0], tz) in days)]
        removed = len(samples) - len(kept)
        if removed:
          logging.info("purge %d v1 samples of %s on days %s (health v2)", removed, key, sorted(days))
          profile.behaviors[key] = kept

    for day in days:
      profile.health_sync_days[day] = version

  def query_health_sync_days(
    self,
    uid: str,
    start_date: str,
    end_date: str,
    timezone: Optional[str],
  ) -> Optional[list[dict]]:
    """对账（健康数据同步接口_0814.md §8.4）：窗口内已有健康数据的天 + 各天口径版本。

    "有哪些天"从 behaviors 实际时间戳按请求时区现算——老画像没有
    health_sync_days 也能回答（缺省版本 1），且被截断淘汰的天不会谎报。
    无画像返回 None。
    """
    profile = self.get_profile(uid)
    if profile is None:
      return None
    tz = self._resolve_tz(timezone)

    days: dict[str, int] = {}
    for key in self.HEALTH_BEHAVIOR_KEYS:
      for item in profile.behaviors.get(key) or []:
        if not isinstance(item, (list, tuple)) or not item:
          continue
        day = self._health_day(item[0], tz)
        if day and start_date <= day <= end_date:
          days[day] = profile.health_sync_days.get(day, 1)
    return [
      {"date": day, "health_schema_version": days[day]}
      for day in sorted(days)
    ]

  @staticmethod
  def _extract_sop_start_events(plays: list) -> list[tuple[str, int, dict]]:
    """Extract SOP start events from a plays list.

    Returns tuples of (cmd, timestamp, event_dict).
    """
    events: list[tuple[str, int, dict]] = []
    if not isinstance(plays, list):
      return events
    for item in plays:
      if not isinstance(item, (list, tuple)) or len(item) < 2:
        continue
      ts, event = item
      if not isinstance(event, dict):
        continue
      cmd = event.get("cmd")
      event_type = event.get("event")
      if isinstance(cmd, str) and cmd.startswith("sleep.scene.") and event_type == "sop_start":
        events.append((cmd, int(ts), event))
    return events

  def _update_mindora_record(self, profile: UserProfile, new_profile: UserProfile):
    """Move SOP play counts from behaviors.plays into mindora_record.

    Stores lightweight (timestamp, duration) tuples instead of full event dicts
    to keep storage and logs small.
    """
    plays = new_profile.behaviors.get("plays", [])
    for cmd, ts, event in self._extract_sop_start_events(plays):
      duration = event.get("duration") if isinstance(event, dict) else None
      record = profile.mindora_record.setdefault(cmd, [])
      record.append((ts, duration))
      # keep the list sorted by timestamp and cap the length
      record.sort(key=lambda x: x[0])
      if len(record) > UserProfileServ.MAX_BEHAVIOR_LEN:
        record[:] = record[-UserProfileServ.MAX_BEHAVIOR_LEN:]

  # -------------------- 场景统计 --------------------
  @staticmethod
  def _calc_scene_stats(mindora_record: dict, days: int | None = None) -> dict:
    """Compute usage counts and total duration per scene from mindora_record.

    If ``days`` is given, only entries whose timestamp is within the last
    ``days`` days are counted.
    """
    cutoff_ts = int(time.time()) - days * 86400 if days else 0
    stats: dict[str, dict] = {}
    for scene_id, records in (mindora_record or {}).items():
      if not isinstance(records, list) or not records:
        continue
      total_duration = 0
      count = 0
      for entry in records:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
          try:
            ts = int(entry[0])
          except (TypeError, ValueError):
            continue
          if ts < cutoff_ts:
            continue
          count += 1
          if len(entry) >= 2 and entry[1] is not None:
            try:
              total_duration += float(entry[1])
            except (TypeError, ValueError):
              pass
      if count > 0:
        stats[scene_id] = {"count": count, "total_duration": round(total_duration, 1)}
    return stats

  @staticmethod
  def _pick_most_used_scene(mindora_record: dict, days: int | None = None) -> Optional[tuple[str, dict]]:
    """Return (scene_id, stats) for the scene with the highest usage count."""
    stats = UserProfileServ._calc_scene_stats(mindora_record, days=days)
    if not stats:
      return None
    best_id = max(stats.items(), key=lambda x: x[1]["count"])[0]
    return best_id, stats[best_id]

  def _update_scene_stats(self, profile: UserProfile):
    """Pre-compute most-used scene (all-time and last 7 days) and persist them in sleep_analysis."""
    now = int(time.time())

    # All-time most used scene
    most_used = self._pick_most_used_scene(profile.mindora_record)
    if most_used is None:
      profile.sleep_analysis.pop("most_used_scene", None)
    else:
      scene_id, scene_stats = most_used
      short_id = scene_id.replace("sleep.scene.", "")
      profile.sleep_analysis["most_used_scene"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "count": scene_stats["count"],
        "total_duration": scene_stats["total_duration"],
        "updated_at": now,
      }

    # Most used scene in the last 7 days
    most_used_7d = self._pick_most_used_scene(profile.mindora_record, days=7)
    if most_used_7d is None:
      profile.sleep_analysis.pop("most_used_scene_7d", None)
    else:
      scene_id, scene_stats = most_used_7d
      short_id = scene_id.replace("sleep.scene.", "")
      profile.sleep_analysis["most_used_scene_7d"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "count": scene_stats["count"],
        "total_duration": scene_stats["total_duration"],
        "updated_at": now,
      }

  @staticmethod
  def _pick_best_sleep_quality_scene(profile: UserProfile, days: int = 7) -> Optional[dict]:
    """Return the scene whose usage before sleep onset produced the highest avg sleep_quality.

    For each sleep night in the last ``days`` days:
      - Compute sleep onset datetime from sleep_data.timestamp + first_sleep_time.
      - Look at scene usages in the 4-hour wind-down window before onset.
      - Pick the scene usage closest to onset as the "effective" scene for that night.
      - Attribute that night's sleep_quality to that scene.

    The returned dict contains the scene with the highest average sleep_quality.
    """
    if not profile or not profile.sleep_data or not profile.mindora_record:
      return None

    now_ts = int(time.time())
    cutoff_ts = now_ts - days * 86400

    # Collect all scene usages with metadata.
    usages: list[dict] = []
    for scene_id, records in profile.mindora_record.items():
      if not isinstance(records, list) or not records:
        continue
      short_id = scene_id.replace("sleep.scene.", "")
      scene_name = short_id.replace("_", " ").title()
      for entry in records:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
          try:
            ts = int(entry[0])
          except (TypeError, ValueError):
            continue
          if ts < cutoff_ts:
            continue
          usages.append({
            "scene_id": short_id,
            "scene_name": scene_name,
            "timestamp": ts,
          })

    if not usages:
      return None

    # Attribute sleep_quality to the effective scene per night.
    scene_qualities: dict[str, list[float]] = {}
    for record in profile.sleep_data[-days:]:
      if record.timestamp < cutoff_ts:
        continue
      if record.sleep_quality is None or not record.first_sleep_time:
        continue

      try:
        sleep_dt = datetime.datetime.fromtimestamp(record.timestamp)
        hour, minute = record.first_sleep_time.split(":")
        onset_dt = sleep_dt.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if onset_dt > sleep_dt:
          onset_dt -= datetime.timedelta(days=1)
      except Exception:
        continue

      onset_ts = int(onset_dt.timestamp())
      window_start_ts = onset_ts - 4 * 3600  # 4-hour wind-down window

      # Pick the scene usage closest to onset but not after it.
      best_usage = None
      best_delta = None
      for usage in usages:
        ts = usage["timestamp"]
        if ts < window_start_ts or ts > onset_ts:
          continue
        delta = onset_ts - ts
        if best_delta is None or delta < best_delta:
          best_delta = delta
          best_usage = usage

      if best_usage is None:
        continue

      scene_qualities.setdefault(best_usage["scene_id"], []).append(record.sleep_quality)

    if not scene_qualities:
      return None

    best_scene_id = max(
      scene_qualities.items(),
      key=lambda item: sum(item[1]) / len(item[1]),
    )[0]
    qualities = scene_qualities[best_scene_id]
    avg_quality = round(sum(qualities) / len(qualities), 1)

    # Find a human-readable name for the best scene.
    scene_name = best_scene_id.replace("_", " ").title()
    for usage in usages:
      if usage["scene_id"] == best_scene_id:
        scene_name = usage["scene_name"]
        break

    return {
      "scene_id": best_scene_id,
      "scene_name": scene_name,
      "avg_sleep_quality": avg_quality,
      "nights": len(qualities),
      "updated_at": now_ts,
    }

  def _update_best_scene_by_sleep_quality(self, profile: UserProfile):
    """Persist the scene with the highest avg sleep_quality in the last 7 days."""
    best = self._pick_best_sleep_quality_scene(profile, days=7)
    if best is None:
      profile.sleep_analysis.pop("best_sleep_quality_scene_7d", None)
      return
    profile.sleep_analysis["best_sleep_quality_scene_7d"] = best

  @staticmethod
  def _profile_for_log(profile: UserProfile) -> dict:
    """Return a compact dict for logging (large fields are summarized)."""
    data = profile.model_dump(mode="json", exclude_none=True)
    if isinstance(data.get("behaviors"), dict):
      data["behaviors"] = {
        k: len(v) if isinstance(v, list) else v for k, v in data["behaviors"].items()
      }
    if isinstance(data.get("sleep_scenarios_reco"), list):
      data["sleep_scenarios_reco"] = [
        {"scenario_id": s.get("scenario_id"), "scenario_name": s.get("scenario_name")}
        for s in data["sleep_scenarios_reco"]
      ]
    if isinstance(data.get("standard_sop_reco"), list):
      data["standard_sop_reco"] = [
        {"scenario_id": s.get("scenario_id"), "scenario_name": s.get("scenario_name")}
        for s in data["standard_sop_reco"]
      ]
    if isinstance(data.get("mindora_record"), dict):
      data["mindora_record"] = {
        k: len(v) if isinstance(v, list) else v for k, v in data["mindora_record"].items()
      }
    if isinstance(data.get("sleep_data"), list):
      data["sleep_data"] = len(data["sleep_data"])
    return data

  # -------------------- 推荐（sleep_reco 引擎） --------------------
  def calc_sleep_reco(self, uid: str, new_profile: UserProfile, old_profile: UserProfile) -> List[SleepScenario]:
    # 1. 触发推荐引擎逻辑
    sleep_scenarios = old_profile.sleep_scenarios_reco
    # if RecommendationEngine.should_rerun_recommendation(old_profile, new_profile):
    logging.info(f"Rerunning sleep scenario recommendation for {uid}")
    sleep_scenarios = RecommendationEngine.generate(new_profile)

    return sleep_scenarios

  def calc_standard_sop_reco(self, uid: str, new_profile: UserProfile, old_profile: UserProfile) -> List[SleepScenario]:
    sop_reco = old_profile.standard_sop_reco
    candidates = []
    logging.info(f"Rerunning standard SOP recommendation for {uid} with candidates={candidates}")
    sop_reco = RecommendationEngine.generate_sop_reco(new_profile, candidates)
    return sop_reco

  # -------------------- 更新路径 --------------------
  @staticmethod
  def _merge_personal_profile(old: Optional[Profile], new: Optional[Profile]) -> Optional[Profile]:
    """个人资料容器合并（个人资料同步约定.md §1）：

      - 键不出现（不在 model_fields_set）→ 保持原值
      - 键出现但值为 None（显式 null） → 保持原值
      - "" → 置空（用户主动清空）；有内容 → 覆盖
      - address_list 整体替换（键出现时，含空数组=清空地址）
      - 头像无新值时客户端整个键不出现，自然保持原值

    old 为 None（新建画像）时以 Profile() 默认值为底，显式 null 落在默认值上。
    """
    if new is None:
      return old
    merged = old.model_copy(deep=True) if old is not None else Profile()
    for fname in type(new).model_fields:
      if fname not in new.model_fields_set:
        continue
      value = getattr(new, fname)
      if value is None:
        continue
      setattr(merged, fname, value)
    return merged

  @staticmethod
  def _merge_sleep_data(old: list, new: list) -> list:
    """按 timestamp 去重合并 sleep_data（同 timestamp 新记录覆盖旧记录），按时间升序，截断保留最近 N 条。"""
    if not new:
      return old
    by_ts = {r.timestamp: r for r in old}
    for r in new:
      by_ts[r.timestamp] = r
    merged = sorted(by_ts.values(), key=lambda r: r.timestamp)
    return merged[-UserProfileServ.MAX_SLEEP_DATA_LEN:]

  @staticmethod
  def _night_window(record) -> Optional[tuple[int, int]]:
    """当夜睡眠窗口 [start_ts, end_ts]：取 sleep_status 序列的覆盖范围；无序列返回 None。"""
    if not record.sleep_status:
      return None
    start = min(e.start_time for e in record.sleep_status)
    end = max(int(e.start_time + e.duration * 60) for e in record.sleep_status)
    return start, end

  @staticmethod
  def _hr_pairs_from_points(samples) -> dict[int, float]:
    """behaviors 数值序列 → {ts: value}（非法条目跳过）。"""
    points: dict[int, float] = {}
    for item in samples or []:
      if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], (int, float)):
        try:
          points[int(item[0])] = float(item[1])
        except (TypeError, ValueError):
          continue
    return points

  def _update_night_hr_range(self, profile: UserProfile) -> None:
    """计算各条 SleepResult 的当夜心率区间 hr_min/hr_max，update_profile 时调用。

    两个口径（健康数据同步接口_0814.md §8.2）：
      - v2：客户端直接上传 sleep_heart_rate_min/max（一晚一对，时间戳同为该晚
        睡眠会话起点），按会话起点落入当夜窗口配对写入；
      - v1 fallback：从 behaviors.heart_rate 全天序列按睡眠窗口取 min/max。
        behaviors 有截断，截断前窗口数据最全；每次 update 幂等重算。
    """
    v2_min = self._hr_pairs_from_points(profile.behaviors.get("sleep_heart_rate_min"))
    v2_max = self._hr_pairs_from_points(profile.behaviors.get("sleep_heart_rate_max"))
    v2_pairs = [(ts, v2_min[ts], v2_max[ts]) for ts in v2_min.keys() & v2_max.keys()]

    v1_points = sorted(self._hr_pairs_from_points(profile.behaviors.get("heart_rate")).items())

    if not v2_pairs and not v1_points:
      return
    for record in profile.sleep_data:
      window = self._night_window(record)
      if window is None:
        continue
      matched = [(mn, mx) for ts, mn, mx in v2_pairs if window[0] <= ts <= window[1]]
      if matched:
        record.hr_min = min(mn for mn, _mx in matched)
        record.hr_max = max(mx for _mn, mx in matched)
        continue
      values = [v for ts, v in v1_points if window[0] <= ts <= window[1]]
      if values:
        record.hr_min = min(values)
        record.hr_max = max(values)

  def _apply_basic_update(
    self,
    uid: str,
    new_profile: UserProfile,
    profile: Optional[UserProfile],
    health_schema_version: Optional[int] = None,
    timezone: Optional[str] = None,
    language: Optional[str] = None,
  ) -> UserProfile:
    """Apply non-LLM profile updates. Must be called while holding self.lock.

    Returns the profile object that should be saved.
    """
    # 记录最近请求环境：每日 LLM 触发门的自然日口径 + 分析文案语言
    self._note_request_meta(new_profile, timezone, language)
    if profile is not None:
      self._note_request_meta(profile, timezone, language)

    if profile is None:
      new_profile.profile = self._merge_personal_profile(None, new_profile.profile)
      self._apply_health_schema_update(new_profile, new_profile, health_schema_version, timezone)
      # 新画像也要把本批 plays 的 sop_start 聚合进 mindora_record，
      # 否则首包场景事件丢失，most_used_scene 永远为空
      self._update_mindora_record(new_profile, new_profile)
      self._update_scene_stats(new_profile)
      self._update_best_scene_by_sleep_quality(new_profile)
      self._update_night_hr_range(new_profile)
      return new_profile

    # just replace, if need
    if len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
      profile.uid_emb = new_profile.uid_emb

    profile.profile = self._merge_personal_profile(profile.profile, new_profile.profile)
    profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)
    # 版本登记 + v1 purge 必须在 merge 前：purge 清的是存量里被覆盖天的旧口径样本
    self._apply_health_schema_update(profile, new_profile, health_schema_version, timezone)
    profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)
    profile.sleep_data = self._merge_sleep_data(profile.sleep_data, new_profile.sleep_data)

    # aggregate SOP play events into mindora_record so we can keep behaviors small
    self._update_mindora_record(profile, new_profile)
    self._update_scene_stats(profile)
    self._update_best_scene_by_sleep_quality(profile)
    self._update_night_hr_range(profile)
    return profile

  def _apply_llm_update(
    self,
    uid: str,
    profile: UserProfile,
    old_profile: UserProfile,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> None:
    """Apply LLM-generated fields to profile in place. Must be called while holding self.lock.

    推荐（sleep_reco）与睡眠分析（insight/analysis cache）是两条独立逻辑，
    由各自的 skip 开关单独控制，互不影响。
    """
    if not skip_sleep_scenarios_reco_update:
      profile.sleep_scenarios_reco = self.calc_sleep_reco(uid, profile, old_profile)
      profile.standard_sop_reco = self.calc_standard_sop_reco(uid, profile, old_profile)
    elif not profile.standard_sop_reco:
      # make sure we never leave standard_sop_reco empty just because the
      # sleep-scenarios skip flag is set
      profile.standard_sop_reco = self.calc_standard_sop_reco(uid, profile, old_profile)

    if not skip_sleep_analysis_update:
      sleep_insight = self.calc_sleep_insight(uid, profile)
      if sleep_insight:
        profile.sleep_insight = sleep_insight
      analysis_reports = self.calc_analysis_reports(uid, profile)
      if analysis_reports is not None:
        profile.analysis_reports = analysis_reports

  def update_profile(
    self,
    uid: str,
    new_profile: UserProfile,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
    health_schema_version: Optional[int] = None,
    timezone: Optional[str] = None,
    language: Optional[str] = None,
  ) -> bool:
    """写入用户行为（仅更新单个用户数据）.

    Synchronous full-update path (basic + LLM in one call, LLM runs while
    holding the lock). 生产路径请用 update_profile_basic + update_profile_llm
    两段式（LLM 不持锁）；本路径保留给直接调用方和测试。
    """
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    with self.lock:
      # 读取或创建用户画像（仅操作单个用户，避免全量加载）
      profile = self.get_profile(uid)
      old_profile = profile
      profile = self._apply_basic_update(
        uid, new_profile, profile, health_schema_version, timezone, language,
      )
      # For newly created profiles there is no old profile; use the new profile
      # object as the old-profile reference so calc_* helpers can read defaults.
      if old_profile is None:
        old_profile = profile

      self._apply_llm_update(
        uid, profile, old_profile,
        skip_sleep_scenarios_reco_update=skip_sleep_scenarios_reco_update,
        skip_sleep_analysis_update=skip_sleep_analysis_update,
      )

      # 仅保存当前用户的更新（而非全量数据）
      self.save_profile(uid, profile)
      logging.info(
        "Profile updated uid=%s summary=%s",
        uid,
        self._profile_for_log(profile),
      )
      return True

  def update_profile_basic(
    self,
    uid: str,
    new_profile: UserProfile,
    health_schema_version: Optional[int] = None,
    timezone: Optional[str] = None,
    language: Optional[str] = None,
  ) -> bool:
    """Persist basic profile changes without LLM work. Fast path for HTTP update_profile."""
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    with self.lock:
      profile = self.get_profile(uid)
      profile = self._apply_basic_update(uid, new_profile, profile, health_schema_version, timezone, language)
      self.save_profile(uid, profile)
      logging.info("Profile basic updated uid=%s", uid)
      return True

  def update_profile_llm(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> bool:
    """Run LLM work for an existing profile and persist results.

    Does not hold self.lock during LLM calls so concurrent basic updates are
    not blocked. Reloads the profile before saving to avoid overwriting
    concurrent writes.  推荐与睡眠分析由各自的 skip 开关独立控制。
    """
    if uid is None or not isinstance(uid, str):
      logging.error(f"invalid uid {uid}")
      return False

    with self.lock:
      profile = self.get_profile(uid)
      if profile is None:
        logging.warning(f"skip llm update: profile not found for uid={uid}")
        return False
      old_profile = profile.model_copy(deep=True)
      llm_profile = profile.model_copy(deep=True)

    if skip_sleep_scenarios_reco_update:
      sleep_scenarios = None
      standard_sop = None
    else:
      sleep_scenarios = self.calc_sleep_reco(uid, llm_profile, old_profile)
      standard_sop = self.calc_standard_sop_reco(uid, llm_profile, old_profile)

    if skip_sleep_analysis_update:
      sleep_insight = None
      analysis_reports = None
    else:
      sleep_insight = self.calc_sleep_insight(uid, llm_profile)
      analysis_reports = self.calc_analysis_reports(uid, llm_profile)

    with self.lock:
      profile = self.get_profile(uid)
      if profile is None:
        logging.warning(f"skip llm update: profile disappeared for uid={uid}")
        return False
      if sleep_scenarios is not None:
        profile.sleep_scenarios_reco = sleep_scenarios
      if standard_sop is not None:
        profile.standard_sop_reco = standard_sop
      elif not profile.standard_sop_reco:
        # reco 被跳过时，兜底保证 standard_sop_reco 不为空
        profile.standard_sop_reco = self.calc_standard_sop_reco(uid, profile, old_profile)
      if sleep_insight:
        profile.sleep_insight = sleep_insight
      if analysis_reports is not None:
        profile.analysis_reports = analysis_reports
      self.save_profile(uid, profile)
      logging.info("Profile llm updated uid=%s", uid)
      return True

  def close(self):
    if self.db is not None:
      self.db.close()
