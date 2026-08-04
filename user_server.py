import asyncio,copy,datetime,json,logging,os,threading,time,uuid
from typing import Any, Optional, List
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import jwt
from pydantic import BaseModel, ValidationError
from aiohttp import ClientResponseError, ClientSession, web
from sleep_reco import RecommendationEngine
try:
  import plyvel
except ImportError:
  plyvel = None
from user_profile import UserProfile, SleepScenario
from config import Config
from common import util
from common.jwt_keys import verify_token
from user_profile import (
  UserProfile, ProfileRequest, ProfileResponse, ProfileData,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp, BaseResponse,
  AnalysisRequest, AnalysisResponse,
  SleepInsightReport, AnalysisTextReport,
  ANALYSIS_REPORT_KEYS, ANALYSIS_REPORT_RETENTION,
  compute_recent_sleep_stats,
  PopupState, InboxMessage, PopupRequest,
  SurveySubmission, SurveyRequest,
  FootprintDay, FootprintRequest,
)
from auth import AuthRequest
from uid.uuid import get_or_create_uuid
from llm_service import SleepAnalysisLLM, extract_sleep_context, deep_merge
import logger
import copy

load_dotenv()
run_dir = os.getenv("RUN_DIR")
logger.init_log(f"{run_dir}/user_server_logs")
# JWT 验签改用 RS256 公钥（见 common/jwt_keys.py），不再需要本地保存签名密钥
REMOTE_SYNC_HEADER = "X-Mindora-Remote-Sync"


# -------------------------- 弹窗 / 问卷运营配置（tanchuang_suvey.md） --------------------------
# 配置存于 JSON 文件（默认 data/popup_survey_config.json，可用 POPUP_SURVEY_CONFIG_PATH
# 环境变量覆盖），由运营后台线上写入；user_server 每次请求按文件 mtime 检查更新并热加载，
# 解析失败时沿用上一份可用配置。
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
    return popups, surveys, next_query_after


# 陪伴足迹里程碑规则（peibanzuji.md ③）：连续 N 天 plan_completed=true → 一条已完成里程碑
FOOTPRINT_MILESTONE_STREAK = 5


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 100
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

  _INSIGHT_MODULE_KEYS = [
    ("greeting", 0),
    ("onset", 1),
    ("architecture", 2),
    ("intervention", 3),
    ("scene_preference", 4),
    ("micro_education", 5),
  ]

  def calc_sleep_insight(self, uid: str, profile: UserProfile) -> Optional[SleepInsightReport]:
    """Generate the 6-module insight report (mindora_advice.md 模块0-5) via LLM
    and return it for storage in ``profile.sleep_insight``.

    If the stored report is less than 7 days old, reuse it instead of calling
    the LLM again.  Returns None when there is nothing to store (LLM disabled
    and no existing report).
    """
    existing = profile.sleep_insight
    now = int(time.time())
    if existing and existing.generated_at and now - existing.generated_at < 7 * 86400:
      logging.info(f"sleep_insight still fresh for uid={uid}, skipping LLM")
      return existing

    if not self.llm or not self.llm.enabled:
      return existing

    class _FakeData:
      date = datetime.date.today().isoformat()
      start_date = None
      end_date = None
      language = "en"

    ctx = extract_sleep_context(profile, _FakeData())
    llm_result = self.llm.generate_sync("sleep_insight_report", ctx, "en", [])
    if not llm_result:
      return existing

    report_data: dict[str, Any] = {
      "date": datetime.date.today().isoformat(),
      "language": "en",
      "generated_at": now,
      "llm_used": True,
    }
    for key, module_id in self._INSIGHT_MODULE_KEYS:
      m = llm_result.get(key) or {}
      report_data[key] = {
        "module_id": module_id,
        "title": m.get("title", "") or "",
        "content": m.get("content", "") or "",
        "evidence": m.get("evidence", []) or [],
        "action": m.get("action", "") or "",
      }

    try:
      report = SleepInsightReport(**report_data)
    except ValidationError as e:
      logging.error(f"invalid insight report from LLM for uid={uid}: {e}")
      return existing

    # 模块3 展示条件（mindora_advice.md）：近7日存在短暂觉醒才展示，否则前端隐藏
    stats = compute_recent_sleep_stats(profile, days=7)
    if not stats.get("avg_awake_count"):
      report.intervention.visible = False
    return report

  @staticmethod
  def _analysis_specs_for_today() -> list:
    """5 个分析能力的当前周期定义：(request_type, start_date, end_date, date, modules)。

    日级能力 start_date/end_date 为 None、date=今日；周/月带起止日期。
    """
    today = datetime.date.today()
    today_str = today.isoformat()
    week_start = (today - datetime.timedelta(days=6)).isoformat()
    month_start = (today - datetime.timedelta(days=29)).isoformat()
    return [
      ("analysis_overview", None, None, today_str, []),
      ("analysis_sleep_day", None, None, today_str, []),
      ("analysis_explore", None, None, today_str, [
        "header_summary", "score_summary", "onset_efficiency",
        "sleep_structure", "night_fluctuation", "scene_preference", "sleep_advice",
      ]),
      ("analysis_sleep_week", week_start, today_str, today_str, []),
      ("analysis_sleep_month", month_start, today_str, today_str, []),
    ]

  @staticmethod
  def _upsert_analysis_report(reports: list, report: AnalysisTextReport, retention: int) -> list:
    """按周期 upsert（同周期替换），按日期排序并裁剪到保留条数。"""
    def same_period(r: AnalysisTextReport) -> bool:
      if report.start_date is not None:
        return r.start_date == report.start_date and r.end_date == report.end_date
      return r.date == report.date and r.start_date is None

    kept = [r for r in reports if not same_period(r)]
    kept.append(report)
    kept.sort(key=lambda r: (r.end_date or r.date, r.generated_at))
    return kept[-retention:]

  def calc_analysis_reports(self, uid: str, profile: UserProfile, language: str = "en") -> Optional[dict]:
    """异步生成 5 个分析能力的当前周期文案报告，返回更新后的 analysis_reports。

    在 update_profile 的后台 LLM 更新中调用；/analysis 请求时只读库。
    当前周期已有报告则复用（每周期每能力至多一次 LLM 调用）。
    LLM 不可用或全部失败时返回 None（调用方保留旧数据）。
    """
    if not self.llm or not self.llm.enabled:
      return None

    existing = profile.analysis_reports or {}
    reports: dict = {key: list(existing.get(key) or []) for key in ANALYSIS_REPORT_KEYS}
    now = int(time.time())
    changed = False

    for request_type, start_date, end_date, date, modules in self._analysis_specs_for_today():
      # 当前周期已有报告 → 复用，不重复调 LLM
      def _is_current(r: AnalysisTextReport) -> bool:
        if start_date is not None:
          return r.start_date == start_date and r.end_date == end_date
        return r.date == date and r.start_date is None

      if any(_is_current(r) for r in reports[request_type]):
        continue

      class _FakeData:
        pass

      fake = _FakeData()
      fake.date = date
      fake.start_date = start_date
      fake.end_date = end_date
      fake.language = language
      fake.modules = modules

      ctx = extract_sleep_context(profile, fake)
      try:
        llm_result = self.llm.generate_sync(request_type, ctx, language, modules)
      except Exception as e:
        logging.error(f"analysis report generation failed for {request_type}: {e}")
        continue

      if not llm_result:
        continue

      report = AnalysisTextReport(
        request_type=request_type,
        date=date,
        start_date=start_date,
        end_date=end_date,
        language=language,
        generated_at=now,
        llm_used=True,
        modules=llm_result,
      )
      reports[request_type] = self._upsert_analysis_report(
        reports[request_type], report, ANALYSIS_REPORT_RETENTION[request_type],
      )
      changed = True

    return reports if changed else None

  @staticmethod
  def _find_analysis_report(
    profile: Optional[UserProfile],
    request_type: str,
    date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
  ) -> Optional[AnalysisTextReport]:
    """按周期精确查找库存报告；未命中时，若最新一条仍属当前周期则回退到它。"""
    if not profile or not profile.analysis_reports:
      return None
    reports = profile.analysis_reports.get(request_type) or []
    if not reports:
      return None

    for r in reversed(reports):
      if start_date is not None or end_date is not None:
        if r.start_date == start_date and r.end_date == end_date:
          return r
      elif date is not None and r.date == date and r.start_date is None:
        return r

    # 回退：请求的是当前周期（与生成时口径一致），直接用最新一条
    current = {rt: (s, e, d) for rt, s, e, d, _m in UserProfileServ._analysis_specs_for_today()}
    if request_type in current:
      c_start, c_end, c_date = current[request_type]
      is_current_period = (
        (start_date is not None and start_date == c_start and end_date == c_end)
        or (start_date is None and end_date is None and (date is None or date == c_date))
      )
      if is_current_period:
        return reports[-1]
    return None

  @staticmethod
  def _visible_insight_dict(profile: Optional[UserProfile]) -> Optional[dict]:
    """返回过滤掉 visible=False 模块后的 6 模块洞察报告 dict；无报告返回 None。"""
    report = profile.sleep_insight if profile else None
    if report is None:
      return None
    data = report.model_dump(mode="json")
    for key, _mid in UserProfileServ._INSIGHT_MODULE_KEYS:
      module = data.get(key)
      if isinstance(module, dict) and module.get("visible") is False:
        data.pop(key)
    return data

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

    Synchronous full-update path. Preserved for backward compatibility with
    fetch_profile_from_remote and direct callers/tests.
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

  # -------------------- 弹窗 / 问卷 / 陪伴足迹（tanchuang_suvey.md, peibanzuji.md） --------------------

  def _get_or_create_profile_unlocked(self, uid: str) -> UserProfile:
    """读取画像，不存在则返回一个新画像对象（调用方负责 save_profile）。"""
    profile = self.get_profile(uid)
    return profile if profile is not None else UserProfile()

  def query_popups(self, uid: str, language: str, placement: str = "home") -> dict:
    """拉取当前应展示的弹窗列表：按时间窗/展示位/用户频控过滤，按 priority 降序。

    返回 {"popups": [...], "next_query_after": Optional[int]}；
    next_query_after 为 None 表示服务端不下发（客户端用默认 300s）。

    同时对时间窗内 push_message=true（survey 类恒落）的弹窗落地站内消息，
    按 popup_id 去重、每条只落一次，不受频控影响。
    """
    now = int(time.time())
    popups_catalog, _, next_query_after = _load_ops_config()
    with self.lock:
      profile = self._get_or_create_profile_unlocked(uid)
      inbox_changed = False
      result: list[dict] = []

      for popup in popups_catalog:
        if popup.get("placement", "home") != placement:
          continue
        start_at = popup.get("start_at")
        end_at = popup.get("end_at")
        in_window = (start_at is None or start_at <= now) and (end_at is None or now <= end_at)
        if not in_window:
          continue

        # 站内消息落地（不看频控/优先级；survey 类恒落）
        if popup.get("push_message") or popup.get("type") == "survey":
          if not any(m.popup_id == popup["popup_id"] for m in profile.inbox_messages):
            text = _i18n(popup, language)
            profile.inbox_messages.append(InboxMessage(
              message_id=f"msg_{uuid.uuid4().hex[:12]}",
              popup_id=popup["popup_id"],
              title=text.get("title", ""),
              subtitle=text.get("subtitle", ""),
              action_type=popup.get("action_type", "dismiss"),
              action_payload=popup.get("action_payload") or {},
              created_at=now,
            ))
            inbox_changed = True

        # 频控过滤（display_rule 是客户端本地频控的权威参数，服务端同样执行）
        rule = popup.get("display_rule") or {}
        state = profile.popup_states.get(popup["popup_id"])
        if state is not None:
          if state.dismissed and rule.get("dismiss_stops", True):
            continue
          max_show = rule.get("max_show_count")
          if max_show is not None and state.show_count >= max_show:
            continue
          cooldown = rule.get("cooldown_seconds")
          if cooldown and state.last_impression_at and now - state.last_impression_at < cooldown:
            continue

        # survey 类弹窗：该问卷已提交则不再下发（与客户端去重口径一致）
        action_payload = popup.get("action_payload") or {}
        if popup.get("type") == "survey" and action_payload.get("survey_id") in profile.survey_submissions:
          continue

        text = _i18n(popup, language)
        result.append({
          "popup_id": popup["popup_id"],
          "type": popup["type"],
          "badge": text.get("badge", ""),
          "badge_style": text.get("badge_style", "purple"),
          "title": text.get("title", ""),
          "subtitle": text.get("subtitle", ""),
          "image_url": popup.get("image_url", ""),
          "action_text": text.get("action_text", ""),
          "action_type": popup.get("action_type", "dismiss"),
          "action_payload": action_payload,
          "push_message": bool(popup.get("push_message")),
          "start_at": start_at,
          "end_at": end_at,
          "priority": popup.get("priority", 0),
          "display_rule": rule,
        })

      if inbox_changed or profile.popup_states:
        self.save_profile(uid, profile)

      result.sort(key=lambda p: p.get("priority", 0), reverse=True)
      return {"popups": result, "next_query_after": next_query_after}

  def report_popup_event(self, uid: str, popup_id: str, event: str, event_at: int) -> bool:
    """回传弹窗曝光/点击/关闭事件，更新该用户的弹窗状态。"""
    popups_catalog, _, _ = _load_ops_config()
    if not any(p["popup_id"] == popup_id for p in popups_catalog):
      logging.warning("report_popup for unknown popup_id=%s uid=%s", popup_id, uid)
      return False
    with self.lock:
      profile = self._get_or_create_profile_unlocked(uid)
      state = profile.popup_states.get(popup_id) or PopupState()
      if event == "impression":
        state.show_count += 1
        state.last_impression_at = event_at
      elif event == "click":
        state.click_count += 1
      elif event == "dismiss":
        state.dismissed = True
      profile.popup_states[popup_id] = state
      self.save_profile(uid, profile)
      return True

  def get_survey(self, survey_id: str, language: str) -> Optional[dict]:
    """拉取问卷题目（按语言）；未知 survey_id 返回 None。"""
    _, surveys, _ = _load_ops_config()
    survey = surveys.get(survey_id)
    if survey is None:
      return None
    text = _i18n(survey, language)
    return {
      "survey_id": survey_id,
      "title": text.get("title", ""),
      "questions": text.get("questions", []),
      "reward": text.get("reward"),
    }

  def submit_survey(self, uid: str, data) -> tuple[Optional[dict], int]:
    """提交问卷。返回 (响应 data, code)；同一 uid+survey_id 幂等：
    重复提交返回既有 submission_id 且 reward_granted=False（code=0）。"""
    _, surveys, _ = _load_ops_config()
    survey = surveys.get(data.survey_id)
    if survey is None:
      return None, 404
    text = _i18n(survey, data.language)
    questions = text.get("questions", [])

    # 答案必须覆盖全部题目；必答题（缺省选择题 true、文本题 false）须已作答
    answers_by_qid = {a.question_id: a for a in data.answers}
    for q in questions:
      answer = answers_by_qid.get(q["question_id"])
      if answer is None:
        return None, 400
      required = q.get("required", q["type"] != "text")
      if required:
        if q["type"] in ("single_choice", "multi_choice") and not answer.option_ids:
          return None, 400
        if q["type"] == "text" and not answer.text.strip():
          return None, 400

    reward = text.get("reward") or {}
    gift_type = reward.get("gift_type", "none")
    if gift_type in ("physical", "virtual") and data.gift_delivery is None:
      return None, 400

    with self.lock:
      profile = self._get_or_create_profile_unlocked(uid)
      existing = profile.survey_submissions.get(data.survey_id)
      if existing is not None:
        return {
          "submission_id": existing.submission_id,
          "reward_granted": False,
          "reward_title": text.get("reward_title", ""),
          "reward_desc": reward.get("desc", ""),
        }, 0

      submission = SurveySubmission(
        submission_id=f"sub_{uuid.uuid4().hex[:12]}",
        survey_id=data.survey_id,
        submitted_at=data.submitted_at or int(time.time()),
        duration_seconds=data.duration_seconds,
        answers=data.answers,
        gift_delivery=data.gift_delivery,
        reward_granted=gift_type != "none",
      )
      profile.survey_submissions[data.survey_id] = submission
      self.save_profile(uid, profile)

    return {
      "submission_id": submission.submission_id,
      "reward_granted": submission.reward_granted,
      "reward_title": text.get("reward_title", ""),
      "reward_desc": reward.get("desc", ""),
    }, 0

  def merge_footprint_days(self, uid: str, days: List[FootprintDay]) -> int:
    """上传陪伴足迹：按 uid+date 幂等合并（布尔取 OR、计数取大、首活跃取小）。返回接受的天数。"""
    with self.lock:
      profile = self._get_or_create_profile_unlocked(uid)
      for day in days:
        existing = profile.footprint_days.get(day.date)
        if existing is None:
          profile.footprint_days[day.date] = day
          continue
        existing.app_active = existing.app_active or day.app_active
        existing.sleep_companion = existing.sleep_companion or day.sleep_companion
        existing.plan_completed = existing.plan_completed or day.plan_completed
        existing.app_open_count = max(existing.app_open_count, day.app_open_count)
        existing.companion_minutes = max(existing.companion_minutes, day.companion_minutes)
        if day.first_active_at is not None:
          if existing.first_active_at is None or day.first_active_at < existing.first_active_at:
            existing.first_active_at = day.first_active_at
      self.save_profile(uid, profile)
    return len(days)

  @staticmethod
  def _footprint_milestones(days_map: dict[str, FootprintDay]) -> list[dict]:
    """按运营规则扫描日记录生成里程碑：连续 N 天 plan_completed=true → 一条已完成里程碑；
    最近一段未达 N 天的连续记录作为进行中里程碑返回。"""
    plan_dates = sorted(
      datetime.date.fromisoformat(d.date)
      for d in days_map.values()
      if d.plan_completed
    )
    if not plan_dates:
      return []

    streaks: list[list[datetime.date]] = [[plan_dates[0]]]
    for d in plan_dates[1:]:
      if (d - streaks[-1][-1]).days == 1:
        streaks[-1].append(d)
      else:
        streaks.append([d])

    def _fmt(d: datetime.date) -> str:
      return f"{d.year}.{d.month}.{d.day}"

    milestones: list[dict] = []
    for streak in streaks:
      if len(streak) >= FOOTPRINT_MILESTONE_STREAK:
        milestones.append({
          "milestone_id": f"ms_plan_{streak[0].isoformat()}",
          "title": "睡眠计划",
          "date_range": f"{_fmt(streak[0])}-{_fmt(streak[-1])}",
          "desc": f"{len(streak)} 日睡眠目标·已完成",
          "completed": True,
        })
    current = streaks[-1]
    if len(current) < FOOTPRINT_MILESTONE_STREAK:
      milestones.append({
        "milestone_id": f"ms_plan_{current[0].isoformat()}",
        "title": "睡眠计划",
        "date_range": f"{_fmt(current[0])}-{_fmt(current[-1])}",
        "desc": f"{FOOTPRINT_MILESTONE_STREAK} 日睡眠目标·已完成 {len(current)}/{FOOTPRINT_MILESTONE_STREAK}",
        "completed": False,
      })
    return milestones

  @staticmethod
  def _footprint_day_entry(day: FootprintDay) -> dict:
    return {
      "day": int(day.date.split("-")[2]),
      "app_active": day.app_active,
      "sleep_companion": day.sleep_companion,
      "plan_completed": day.plan_completed,
    }

  def query_footprint(self, uid: str, scope: str, year: int, month: Optional[int], timezone: str) -> dict:
    """拉取陪伴足迹汇总：计数统计、锚定日期、日记录与里程碑。"""
    try:
      tz = ZoneInfo(timezone)
    except Exception:
      logging.warning("unknown timezone=%s, fallback UTC", timezone)
      tz = ZoneInfo("UTC")
    today = datetime.datetime.now(tz).date()

    with self.lock:
      profile = self.get_profile(uid)
      days_map = dict(profile.footprint_days) if profile else {}

    # 锚定日期：最近一条有数据（≤今天）的日期
    marked = sorted(
      d.date for d in days_map.values()
      if (d.app_active or d.sleep_companion or d.plan_completed)
      and datetime.date.fromisoformat(d.date) <= today
    )
    anchor_date = marked[-1] if marked else today.isoformat()
    weekday = datetime.date.fromisoformat(anchor_date).isoweekday()

    # 连续使用：该自然年内有任意使用记录的天数累计（非连续 streak）
    year_prefix = f"{year:04d}-"
    continuous_use_year = sum(
      1 for d in days_map.values()
      if d.date.startswith(year_prefix) and (d.app_active or d.sleep_companion)
    )

    if scope == "year":
      months = []
      for m in range(1, 13):
        prefix = f"{year:04d}-{m:02d}-"
        month_days = [
          self._footprint_day_entry(d) for d in sorted(days_map.values(), key=lambda x: x.date)
          if d.date.startswith(prefix) and (d.app_active or d.sleep_companion or d.plan_completed)
        ]
        months.append({"month": m, "days": month_days})
      return {
        "year": year,
        "stats": {"continuous_use_year": continuous_use_year},
        "months": months,
      }

    prefix = f"{year:04d}-{month:02d}-"
    month_records = [d for d in days_map.values() if d.date.startswith(prefix)]
    marked_days = [
      self._footprint_day_entry(d) for d in sorted(month_records, key=lambda x: x.date)
      if d.app_active or d.sleep_companion or d.plan_completed
    ]
    return {
      "anchor_date": anchor_date,
      "weekday": weekday,
      "stats": {
        "sleep_companion_month": sum(1 for d in month_records if d.sleep_companion),
        "app_active_month": sum(1 for d in month_records if d.app_active),
        "continuous_use_year": continuous_use_year,
      },
      "year": year,
      "month": month,
      "days": marked_days,
      "milestones": self._footprint_milestones(days_map),
    }

  def close(self):
    if self.db is not None:
      self.db.close()

def get_http_status(resp: BaseResponse):
  status = 200
  if resp.code != 0:
    status = resp.code
  return status


async def query_profile(jwt_token: str, server_uri: str) :
  query_endpoint = f"{server_uri}/user_profile"
  async with ClientSession() as session:
    try:
      req = ProfileRequest(request_type="query_profile", timestamp=int(time.time()), version="1.0", data=ProfileData(jwt_token = jwt_token))
      # 构造请求数据
      async with session.post(
        query_endpoint,
        json=req.model_dump(),
        timeout=2  # 10秒超时
      ) as response:
        response.raise_for_status()  # 触发HTTP错误（如4xx、5xx）
        data = await response.json()
        return ProfileResponse.model_validate(data)
            
    except ClientResponseError as e:
      # 处理HTTP错误响应
      error_msg = f"查询失败 [HTTP {e.status}]: {e}"
      raise Exception(error_msg) from e
    except Exception as e:
      raise Exception(f"查询用户画像失败: {str(e)}") from e

class UserServer:
  @staticmethod
  def _request_for_log(req_or_data) -> Any:
    """Return a log-safe copy with behaviors summarized by count."""
    if isinstance(req_or_data, BaseModel):
      data = req_or_data.model_dump(mode="json", exclude_none=True)
    elif isinstance(req_or_data, dict):
      data = copy.deepcopy(req_or_data)
    else:
      return req_or_data

    up = ((data.get("data") or {}).get("user_profile") or {})
    if isinstance(up.get("behaviors"), dict):
      up["behaviors"] = {
        k: len(v) if isinstance(v, list) else v for k, v in up["behaviors"].items()
      }
    return data

  def __init__(self):
    self.server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
    self.host = Config.HOST
    self.port = Config.PORT
    self.llm = SleepAnalysisLLM()
    self.user_serv = UserProfileServ(llm=self.llm)
    self.update_task = None
    self.app = web.Application()
    self.active_uid = ""
    self.system_uid = get_or_create_uuid()
    self.debug_uid_set = {"mindora_test_uid1", "mindora_test_uid2", "mindora_test_uid3", "test_debug_user_001"}
    # Per-user LLM background-update rate limiting and task tracking.
    self._llm_tracker_lock = asyncio.Lock()
    self._llm_update_tracker: dict[str, float] = {}
    self._llm_tasks: set[asyncio.Task] = set()
    self._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)
    self.setup_routes()

  def close(self):
    self.user_serv.close()
    if self.update_task:
      self.update_task.cancel()
    # Cancel pending LLM background tasks.
    for task in list(self._llm_tasks):
      task.cancel()
    self._llm_tasks.clear()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/user_profile', self.handle_profile_request_http)
    self.app.router.add_post('/login', self.handle_login_http)
    self.app.router.add_post('/analysis', self.handle_analysis_http)
    self.app.router.add_post('/popup', self.handle_popup_http)
    self.app.router.add_post('/survey', self.handle_survey_http)
    self.app.router.add_post('/companion_footprint', self.handle_footprint_http)

  def _check_token(self, jwt_token: str)-> dict | None:
    try:
      payload = verify_token(jwt_token)
    except jwt.ExpiredSignatureError:
      logging.error("login token expired")
      return None
    except jwt.InvalidTokenError:
      logging.error("login token invalid")
      return None


    logging.info(f"payload: {payload}")
    return payload

  def _parse_for_uid(self, data: Any):
    uid = None
    if data.jwt_token is not None:
      payload = self._check_token(data.jwt_token)
      if payload is None:
        return None
      uid = payload.get("uid")
    elif Config.IS_DEBUG and data.uid is not None and len(data.uid) > 3 and data.uid in self.debug_uid_set:
      uid = data.uid

    return uid

  def handle_query_profile(self, request: ProfileRequest) -> BaseResponse:
    logging.info("handle query_profile request=%s", self._request_for_log(request))
    """查询用户画像（从LevelDB按需读取）"""
    if request.data is None:
      logging.error("query request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"get uid: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    if uid == "active_uid":
      uid = self.active_uid

    profile = self.user_serv.get_profile(uid)
    if profile:
      logging.info("profile found uid=%s summary=%s", uid, self.user_serv._profile_for_log(profile))
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data={"user_profile": profile.model_dump()})
    else:
      logging.warning("uid=%s query not found request=%s", uid, self._request_for_log(request))
      return ProfileResponse(code=0, msg=f"User with uid '{request.data}' not found", request_type=request.request_type, data=None)

    # incr update the behaviors by time, and update long term weight
  async def handle_update_profile(self, request: ProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    if request.data is None:
      logging.error("update request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"uid for update: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    async with self.server_semaphore:
      succ = await asyncio.to_thread(
        self.user_serv.update_profile_basic,
        uid,
        request.data.user_profile,
      )
    if not succ:
      return ProfileResponse(code=500, msg=f"update profile failed", request_type=request.request_type, data=None)

    skip_reco = request.data.skip_sleep_scenarios_reco_update
    skip_analysis = request.data.skip_sleep_analysis_update
    if not (skip_reco and skip_analysis):
      await self._schedule_llm_update_if_needed(uid, skip_reco, skip_analysis)

    return ProfileResponse(code=0, msg=f"update profile for '{request.timestamp}' succ", request_type=request.request_type, data=None)

  async def _schedule_llm_update_if_needed(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> None:
    """Schedule a background LLM update for uid unless one ran recently."""
    async with self._llm_tracker_lock:
      now = time.monotonic()
      last = self._llm_update_tracker.get(uid)
      if last is not None and now - last < Config.LLM_UPDATE_COOLDOWN_SECONDS:
        logging.info("skip background llm update for uid=%s (last %.1fs ago)", uid, now - last)
        return
      self._llm_update_tracker[uid] = now

    task = asyncio.create_task(
      self._run_llm_update(uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update)
    )
    self._llm_tasks.add(task)
    task.add_done_callback(self._llm_tasks.discard)

  async def _run_llm_update(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> None:
    """Run LLM work in a background task."""
    logging.info(
      "start background llm update for uid=%s (skip_reco=%s skip_analysis=%s)",
      uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update,
    )
    try:
      async with self._llm_semaphore:
        await asyncio.to_thread(
          self.user_serv.update_profile_llm,
          uid,
          skip_sleep_scenarios_reco_update,
          skip_sleep_analysis_update,
        )
      logging.info("background llm update done for uid=%s", uid)
    except asyncio.CancelledError:
      logging.info("background llm update cancelled for uid=%s", uid)
      raise
    except Exception as e:
      logging.error("background llm update failed for uid=%s: %s", uid, e)

  async def sync_profile_to_remote(self, uid: str, request: ProfileRequest) -> bool:
    if not Config.RemoteHost or len(Config.RemoteHost) < 10:
      return False

    profile = self.user_serv.get_profile(uid)
    if profile is None:
      logging.warning(f"skip remote sync because local profile missing for uid={uid}")
      return False

    remote_endpoint = f"{Config.RemoteHost.rstrip('/')}/user_profile"
    sync_data = {"user_profile": profile.model_dump()}
    if request.data.jwt_token is not None:
      sync_data["jwt_token"] = request.data.jwt_token
    else:
      sync_data["uid"] = uid
    if request.data.skip_sleep_scenarios_reco_update:
      sync_data["skip_sleep_scenarios_reco_update"] = True

    payload = ProfileRequest(
      request_type="update_profile",
      timestamp=int(time.time()),
      version=request.version,
      data=ProfileData.model_validate(sync_data),
    ).model_dump()

    try:
      async with ClientSession() as session:
        async with session.post(
          remote_endpoint,
          json=payload,
          headers={REMOTE_SYNC_HEADER: "1"},
          timeout=10,
        ) as response:
          resp_data = await response.json()
          if response.status >= 400:
            logging.error(f"remote profile sync failed status={response.status}, body={resp_data}")
            return False
          logging.info(f"remote profile sync succ for uid={uid}, body={resp_data}")
          return True
    except Exception as e:
      logging.error(f"remote profile sync error for uid={uid}: {e}")
      return False

  def handle_login(self, request: AuthRequest) -> BaseResponse:
    if request.data is None or request.data.jwt_token is None:
      return InvalidReqFormatResp()

    payload = self._check_token(request.data.jwt_token)
    if payload is None:
      return InvalidOrExpiredTokenResp()

    uid = payload.get("uid")
    self.active_uid = uid
    self.jwt_token = request.data.jwt_token
    return BaseResponse(code=0, msg="user ativated successufully")

  # -------------------- /popup endpoint（tanchuang_suvey.md） --------------------

  async def handle_popup_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = PopupRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "query_popups":
        result = await asyncio.to_thread(
          self.user_serv.query_popups, uid, req.data.language, req.data.placement,
        )
        # next_query_after 未配置时不下发该字段，客户端用默认 300s
        data = {"popups": result["popups"]}
        if result.get("next_query_after") is not None:
          data["next_query_after"] = result["next_query_after"]
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": data})

      # report_popup
      event_at = req.data.event_at or req.timestamp
      ok = await asyncio.to_thread(
        self.user_serv.report_popup_event, uid, req.data.popup_id, req.data.event, event_at,
      )
      if not ok:
        return web.json_response(BaseResponse(code=400, msg="unknown popup_id").model_dump(), status=400)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {}})

    except ValidationError as e:
      logging.error(f"popup validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("popup error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /survey endpoint（tanchuang_suvey.md） --------------------

  async def handle_survey_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = SurveyRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "query_survey":
        survey = self.user_serv.get_survey(req.data.survey_id, req.data.language)
        if survey is None:
          return web.json_response(BaseResponse(code=404, msg="survey not found").model_dump(), status=404)
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": survey})

      # submit_survey
      data, code = await asyncio.to_thread(self.user_serv.submit_survey, uid, req.data)
      if data is None:
        msg = "survey not found" if code == 404 else "invalid survey answers"
        return web.json_response(BaseResponse(code=code, msg=msg).model_dump(), status=code)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})

    except ValidationError as e:
      logging.error(f"survey validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("survey error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /companion_footprint endpoint（peibanzuji.md） --------------------

  async def handle_footprint_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = FootprintRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "upload_footprint":
        accepted = await asyncio.to_thread(self.user_serv.merge_footprint_days, uid, req.data.days)
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": {"accepted_days": accepted}})

      # query_footprint
      data = await asyncio.to_thread(
        self.user_serv.query_footprint,
        uid, req.data.scope, req.data.year, req.data.month, req.data.timezone,
      )
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})

    except ValidationError as e:
      logging.error(f"footprint validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("footprint error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  def get_overall_score(self, profile: UserProfile) -> Optional[float]:
    """计算用户最近7天的平均睡眠质量得分（0-100）"""
    if not profile.sleep_data:
      return None
    recent = profile.sleep_data[-7:]
    scores = [s.sleep_quality for s in recent if s.sleep_quality is not None]
    return round(sum(scores) / len(scores), 2) if scores else None

  async def handle_profile_request_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      req = ProfileRequest.model_validate(data)
      # logging.info("request %s", self._request_for_log(req))

      if req.request_type == "query_profile":
        response_obj = self.handle_query_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "update_profile":
        response_obj = await self.handle_update_profile(req)
        if (
          response_obj.code == 0
          and Config.RemoteHost is not None and len(Config.RemoteHost) > 8
          and req.data is not None
        ):
          uid = self._parse_for_uid(req.data)
          if isinstance(uid, str) and uid:
            remote_succ = await self.sync_profile_to_remote(uid, req)
            if not remote_succ:
              response_obj.msg = f"{response_obj.msg}, remote sync failed"
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      # client_request.md：/user_profile 只承载 query_profile / update_profile，
      # 分析类请求统一走 /analysis，洞察报告由 analysis_explore 出口
      else:
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)

    except ValidationError as e:
        logging.error(f"Validation error: {e}")
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)
      
  # -------------------- /analysis endpoint --------------------

  async def handle_analysis_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = AnalysisRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)
      if isinstance(uid, BaseResponse):
        return web.json_response(uid.model_dump(), status=uid.code)

      profile = self.user_serv.get_profile(uid)
      response_data = self._build_analysis_data(req, profile)

      # LLM 文案全部来自 update_profile 时异步生成、按日/周/月序列存储的报告，
      # 请求时纯查库，不再同步调用 LLM；未命中则只回数值骨架（文案为空/默认）
      report = UserProfileServ._find_analysis_report(
        profile, req.request_type, req.data.date, req.data.start_date, req.data.end_date,
      )
      if report:
        # 只合并客户端请求的模块，保持 modules 分字段查询语义不被库存报告击穿
        updates = report.modules
        if req.data.modules:
          updates = {k: v for k, v in updates.items() if k in req.data.modules}
        deep_merge(response_data, updates)

      resp = AnalysisResponse(code=0, msg="success", request_type=req.request_type, data=response_data)
      return web.json_response(resp.model_dump())

    except ValidationError as e:
      logging.error(f"analysis validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.error(f"analysis error: {e}")
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  def _build_analysis_data(self, req: AnalysisRequest, profile: Optional[UserProfile]) -> dict:
    d = req.data
    rt = req.request_type
    if rt == "analysis_overview":
      return self._build_overview(d, profile)
    elif rt == "analysis_sleep_day":
      return self._build_sleep_day(d, profile)
    elif rt == "analysis_sleep_week":
      return self._build_sleep_week(d, profile)
    elif rt == "analysis_sleep_month":
      return self._build_sleep_month(d, profile)
    elif rt == "analysis_explore":
      return self._build_explore(d, profile)
    raise ValueError(f"Unknown request_type: {rt}")

  def _filter_modules(self, data: dict, modules: list) -> dict:
    return {k: v for k, v in data.items() if k in modules} if modules else data

  def _build_overview(self, d, profile: Optional[UserProfile]) -> dict:
    date = d.date or datetime.date.today().isoformat()
    score = self.get_overall_score(profile) if profile else None
    if score is None:
      score = 82

    weekly_best = None
    most_used = (profile.sleep_analysis or {}).get("most_used_scene") if profile else None
    if most_used:
      weekly_best = {
        "audio_name": most_used["scene_name"],
        "used_times": most_used["count"],
        "score": int(score),
        "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
        "end_date": date,
      }
    if weekly_best is None:
      weekly_best = {
        "audio_name": "Sedona Red Rocks",
        "used_times": 5,
        "score": 92,
        "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
        "end_date": date,
      }

    result = {
      "overall_score": {"score": int(score), "date": date},
      "weekly_best": weekly_best,
      "sleep_insight": {
        "title": "Excellent Deep Sleep Performance",
        "description": "Your deep sleep accounts for a healthy proportion of total sleep. Keep maintaining a regular sleep schedule.",
        "date": date,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_day(self, d, profile: Optional[UserProfile]) -> dict:
    date = d.date or datetime.date.today().isoformat()
    latest = profile.sleep_data[-1] if profile and profile.sleep_data else None
    score = int(latest.sleep_quality) if latest and latest.sleep_quality else 70

    result = {
      "score_summary": {"score": score, "date": date},
      "sleep_scenarios_reco": {
        "title": "Sedona Desert Calm",
        "description": "You fell asleep quickly and maintained a stable sleep rhythm after the scenario started.",
        "date": date,
      },
      "stage_insights": {
        "awake": {"description": "A brief awakening was detected and you returned to sleep quickly.", "date": date},
        "rem":   {"description": "REM sleep was sustained and supports emotional processing.", "date": date},
        "core":  {"description": "Core sleep remained stable across most of the night.", "date": date},
        "deep":  {"description": "Deep sleep contributed strongly to physical recovery.", "date": date},
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_week(self, d, profile: Optional[UserProfile]) -> dict:
    today = datetime.date.today()
    start = d.start_date or (today - datetime.timedelta(days=6)).isoformat()
    end   = d.end_date   or today.isoformat()

    score = self.get_overall_score(profile) if profile else None
    score = int(score) if score else 86
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

    result = {
      "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
      "sleep_trends": {
        "body": "Excellent Deep Sleep Performance",
        "description": "Your deep sleep accounted for a healthy proportion of total sleep this week.",
        "start_date": start,
        "end_date": end,
      },
      "onset_efficiency": {
        "scenario_name": "Sedona Desert Calm",
        "used_times": 5,
        "score": score,
        "start_date": start,
        "end_date": end,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_month(self, d, profile: Optional[UserProfile]) -> dict:
    today = datetime.date.today()
    start = d.start_date or (today - datetime.timedelta(days=29)).isoformat()
    end   = d.end_date   or today.isoformat()

    score = self.get_overall_score(profile) if profile else None
    score = int(score) if score else 89
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

    # Build score_series from real data, fall back to mock trend
    score_series: list = []
    if profile and profile.sleep_data:
      for sr in profile.sleep_data[-30:]:
        if sr.sleep_quality is not None:
          score_series.append({
            "date": datetime.date.fromtimestamp(sr.timestamp).isoformat(),
            "score": int(sr.sleep_quality),
          })
    if not score_series:
      cur = datetime.date.fromisoformat(start)
      end_d = datetime.date.fromisoformat(end)
      base = 62
      while cur <= end_d:
        score_series.append({"date": cur.isoformat(), "score": min(100, base)})
        base += 1
        cur += datetime.timedelta(days=1)

    result = {
      "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
      "sleep_trends": {
        "body": "This month, you maintained a consistent amount of sleep.",
        "description": "Deep sleep remained above the standard level and your bedtime trended earlier.",
        "score_series": score_series,
        "start_date": start,
        "end_date": end,
      },
      "onset_efficiency": {
        "scenario_list": ["Sedona Desert Calm", "Maldives Drift Sleep", "Canadian Forest Solace"],
        "description": "Sedona Desert Calm was your most frequently used sleep scenario this month and showed the best onset performance.",
        "start_date": start,
        "end_date": end,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_explore(self, d, profile: Optional[UserProfile]) -> dict:
    date  = d.date or datetime.date.today().isoformat()
    start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

    has_data = profile is not None and bool(profile.sleep_data)
    latest   = profile.sleep_data[-1] if has_data else None
    summaries = latest.sequence_summaries if (latest and latest.sleep_status) else {}

    overall_score    = int(latest.sleep_quality) if latest and latest.sleep_quality else 82
    onset_score      = int(latest.soe)           if latest and latest.soe           else 82
    structure_score  = 49
    fluctuation_score = 34

    tb = summaries.get("time_in_bed") or 1
    rem_pct  = f"{round(summaries.get('rem_sleep_duration',  0) / tb * 100, 1)}%" if summaries else "22%"
    deep_pct = f"{round(summaries.get('deep_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "29.8%"
    core_pct = f"{round(summaries.get('core_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "48.2%"

    hr_mid = int(latest.avg_heart_rate) if latest and latest.avg_heart_rate else 70
    hr_range = f"{hr_mid - 15}-{hr_mid + 15}bpm"
    resp_fluct = f"{int(latest.respiratory_var or 25)}%" if latest else "25%"

    scene_id   = "cocos_island_moonlight"
    scene_name = "Cocos Island Moonlight"
    most_used = (profile.sleep_analysis or {}).get("most_used_scene") if profile else None
    if most_used:
      scene_id   = most_used["scene_id"]
      scene_name = most_used["scene_name"]

    awake_count = summaries.get("night_awake_count", 2)
    result = {
      "data_ready": has_data,
      "header_summary": {
        "intro_text": "Last night your body entered a stable, relaxed, and highly restorative sleep state.",
        "intro_detail_text": "What happened last night, what helped you most, and how Mindora adjusted for you.",
        "date": date,
      },
      "score_summary": {
        "score": overall_score,
        "title": "Sleep Score",
        "efficiency_score":   onset_score,
        "structure_score":    structure_score,
        "fluctuation_score":  fluctuation_score,
        "date": date,
      },
      "onset_efficiency": {
        "score": onset_score,
        "label": "Healthy Range",
        "onset_minutes": 12,
        "first_sleep_time":           latest.first_sleep_time if latest else "23:45",
        "pre_sleep_heart_rate":       f"{int(latest.hr_before_sleep)}bpm"  if latest and latest.hr_before_sleep  else "68bpm",
        "pre_sleep_respiratory_rate": f"{int(latest.rr_before_sleep)}brpm" if latest and latest.rr_before_sleep else "15brpm",
        "description": "You fell asleep faster than your recent average and your pre-sleep physiology stayed calm.",
        "date": date,
      },
      "sleep_structure": {
        "score": structure_score,
        "label": "Average",
        "continuous_sleep_minutes": int(tb),
        "rem_percent":  rem_pct,
        "deep_percent": deep_pct,
        "core_percent": core_pct,
        "description": "Your sleep structure remained relatively balanced, with deep sleep contributing strongly to recovery.",
        "date": date,
      },
      "night_fluctuation": {
        "score": fluctuation_score,
        "label": "High Fluctuation" if awake_count > 3 else "Normal",
        "intervention": "Rain Wash",
        "awake_count":            awake_count,
        "awake_duration_minutes": int(summaries.get("night_awake_duration", 5)),
        "awake_type":             summaries.get("night_awake_type") or "Brief awakening",
        "heart_rate_range":       hr_range,
        "respiratory_fluctuation": resp_fluct,
        "description": "You had a small number of brief interruptions and the system applied a suitable intervention.",
        "date": date,
      },
      "scene_preference": {
        "scene_id":   scene_id,
        "scene_name": scene_name,
        "scene_type": "Ocean wind with slow percussion",
        "description": "This scene has recently matched your sleep onset rhythm most consistently.",
        "start_date": start,
        "end_date":   date,
      },
      "sleep_advice": {
        "description": "Keep your current bedtime and continue using the same wind-down scene for the next few nights.",
        "date": date,
      },
      # 洞察页 6 模块报告（mindora_advice.md 模块0-5，update_profile 时异步生成，
      # 已过滤 visible=False 模块；无报告则为 None）。/analysis 是其唯一客户端出口。
      "insight": UserProfileServ._visible_insight_dict(profile),
    }
    return self._filter_modules(result, d.modules)

  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      request = AuthRequest.model_validate(data)
      response_obj = self.handle_login(request)
    except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as e:
      logging.error(f"login error: {e}")
      response_obj = InvalidReqFormatResp()

    logging.info(f"login resp: {response_obj}")
    if response_obj.code == 0 and len(Config.RemoteHost) > 10 and (self.update_task is None or self.update_task.done()):
      self.update_task = asyncio.create_task(self.fetch_profile_from_remote(f"{Config.RemoteHost}")) 
    else:
      logging.info("update task has started already")

    return web.json_response(status=get_http_status(response_obj), data=response_obj.model_dump())
  
  async def fetch_profile_from_remote(self, url):
    """Periodically pull the active user's profile from the remote server.

    Runs in a background task so it must not block the asyncio event loop.
    """
    start_min = int(time.time()) / 60
    logging.info(f"begin to loop update for activeuid : {self.active_uid}")
    while True:
      cur_min = int(time.time()) / 60
      if cur_min - start_min > 60:
        logging.info("break because of time")
        break

      await asyncio.sleep(60)

      resp = await query_profile(self.jwt_token, Config.RemoteHost)
      if resp is None or resp.code != 0 or resp.data is None:
        logging.warning(f"none or invalid resp from remote server: {Config.RemoteHost}")
        continue

      profile_data = resp.data.get("user_profile")
      if profile_data is None:
        logging.warning(f"remote resp missing user_profile for activeuid={self.active_uid}")
        continue

      try:
        new_profile = UserProfile.model_validate(profile_data)
      except ValidationError as e:
        logging.error(f"remote profile validation failed: {e}")
        continue

      # Offload the synchronous update_profile (which may call LLMs) to a
      # thread so the event loop stays responsive for local HTTP requests.
      succ = await asyncio.to_thread(
        self.user_serv.update_profile,
        self.active_uid,
        new_profile,
      )
      if not succ:
        logging.warning(f"erro in update profile for {self.active_uid}")
      else:
        logging.info(f"succ update profile for {self.active_uid}")

  async def start_http(self):
    """启动HTTP服务器"""
    runner = web.AppRunner(self.app)
    await runner.setup()
    site = web.TCPSite(runner, self.host, self.port)
    await site.start()
    logging.info(f"UserServer (LevelDB) started on http://{self.host}:{self.port}")
    # 保持服务运行
    await asyncio.Event().wait()


if __name__ == "__main__":
  server = UserServer()
  try:
    asyncio.run(server.start_http())
  except KeyboardInterrupt:
    logging.warning("Shutting down UserServer.")
    server.close()  # 关闭LevelDB连接
