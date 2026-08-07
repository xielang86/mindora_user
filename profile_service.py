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
from user_profile import UserProfile, SleepScenario

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 100

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

  def calc_analysis_reports(self, uid: str, profile: UserProfile, language: str = "en"):
    return self.content.calc_analysis_reports(uid, profile, language)

  def query_popups(self, uid: str, language: str, placement: str = "home") -> dict:
    return self.engagement.query_popups(uid, language, placement)

  def query_message_history(self, uid: str, language: str, popup_ids) -> list:
    return self.engagement.query_message_history(uid, language, popup_ids)

  def report_popup_event(self, uid: str, popup_id: str, event: str, event_at: int) -> bool:
    return self.engagement.report_popup_event(uid, popup_id, event, event_at)

  def get_survey(self, survey_id: str, language: str):
    return self.engagement.get_survey(survey_id, language)

  def submit_survey(self, uid: str, data):
    return self.engagement.submit_survey(uid, data)

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

      if len(old_behaviors[behavior_type]) > UserProfileServ.MAX_BEHAVIOR_LEN:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][-UserProfileServ.MAX_BEHAVIOR_LEN:]

    logging.info("after update behavior counts=%s", self._behavior_counts(old_behaviors))
    return old_behaviors

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
  def _apply_basic_update(self, uid: str, new_profile: UserProfile, profile: Optional[UserProfile]) -> UserProfile:
    """Apply non-LLM profile updates. Must be called while holding self.lock.

    Returns the profile object that should be saved.
    """
    if profile is None:
      self._update_scene_stats(new_profile)
      self._update_best_scene_by_sleep_quality(new_profile)
      return new_profile

    # just replace, if need
    if len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
      profile.uid_emb = new_profile.uid_emb

    profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)
    profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)

    # aggregate SOP play events into mindora_record so we can keep behaviors small
    self._update_mindora_record(profile, new_profile)
    self._update_scene_stats(profile)
    self._update_best_scene_by_sleep_quality(profile)
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
      profile = self._apply_basic_update(uid, new_profile, profile)
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

  def update_profile_basic(self, uid: str, new_profile: UserProfile) -> bool:
    """Persist basic profile changes without LLM work. Fast path for HTTP update_profile."""
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    with self.lock:
      profile = self.get_profile(uid)
      profile = self._apply_basic_update(uid, new_profile, profile)
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
