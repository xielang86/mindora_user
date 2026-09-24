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
from user_profile import (
  UserProfile, SleepScenario, Profile, SCENE_CMD_PREFIXES, short_scene_id,
  ENGAGEMENT_RESPONSE_KEYS, keep_latest_engagement, active_sleep_plan,
  bedtime_advice_due,
)
from sop_tag_profile import rebuild_sop_tag_profile

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 100
  # sleep_data 保留条数：与分析报告日级保留一致（日级 30）
  MAX_SLEEP_DATA_LEN = 30

  # 头像 base64 上限：画像每次 query/update 都会整体回吐，无上限的头像会让
  # 每轮轮询都背上几百 KB。超限不静默截断（截断后 base64 解不出图），
  # 保留原头像并告警，其余个人资料字段照常合并。
  MAX_AVATAR_BASE64_LEN = 256 * 1024

  # uid 向量维度上限：客户端直供、此前无任何校验（>16 就整体覆盖）。
  # 超限不截断——截半的向量做相似度是错的，宁可保留旧值并告警。
  MAX_UID_EMB_LEN = 128

  # 运营/陪伴四件套（弹窗状态/站内消息/问卷提交/陪伴足迹）单用户条数上限。
  # 四者此前只增不删：足迹一天一条，另外三个随运营发布/问卷次数线性增长。
  # 完整数据各有专用端点（/footprint、/popup、/survey），画像里只是副本。
  MAX_ENGAGEMENT_LEN = 500

  # 单条 SleepResult 内嵌序列上限（客户端直供、逐条无上限，30 条记录能把画像撑爆）。
  # 实测一晚 5~30 段，合成行最多受 HEALTH_V2_BEHAVIOR_CAP 约束；取宽松上限只挡异常。
  # 截断保留「开头」而不是结尾：入睡时长/卧床起点在序列头部，丢尾部只影响醒来段。
  MAX_SLEEP_RECORD_LIST_LEN = {
    "sleep_status": 2000,
    "night_events": 2000,
    "in_bed_intervals": 200,
    "self_rating_tags": 50,
    "scene_preference": 100,
  }

  # 场景使用 ↔ 睡眠记录的对齐窗口（"使用当晚"口径）：
  # sleep_data 时间戳实测为次日早晨上传（约 04:47–09:14），场景使用发生在
  # 前一晚（实测 15:00–18:30），两者间隔实测约 12.7–16 小时；取睡眠记录
  # 时间戳往前回溯该窗口内的最近一次使用作为当晚生效场景
  USAGE_SLEEP_LOOKBACK_SEC = 16 * 3600

  # ── 健康数据口径版本（健康数据同步接口_0814.md §8）─────────────────────
  # md §3 字段表全集（v2 现役口径）：一晚只有几~几十个点（已裁剪到睡眠跨度），
  # 上限对齐 md 客户端单切片上限 5000（约覆盖 100 晚），否则对账窗口稍长就被截断
  # 失真——deep/rem/light 一晚 5~30 段，100 上限连 30 天首同步窗口都装不下，
  # 截断后 §8.4 对账会谎报"这些天没数据"（天从 behaviors 现算）导致客户端无限重传，
  # 服务端合成 SleepResult 也会丢阶段。v1 废弃 key（heart_rate 等）与 plays/clicks
  # 等交互行为沿用 MAX_BEHAVIOR_LEN。
  # sleep_stage_light 是 v1/v2 共用但语义不同的字段；只在该字段首次迁移时
  # 清理 v1 样本，后续 v2 分片必须增量合并。
  HEALTH_V2_BEHAVIOR_KEYS = {
    "sleep_heart_rate_min", "sleep_heart_rate_max",
    "sleep_heart_rate_variability_sdnn", "sleep_respiratory_rate",
    "resting_heart_rate", "sleeping_wrist_temperature", "sleep_body_temperature",
    "sleep_stage_deep", "sleep_stage_rem", "sleep_stage_light",
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
  HEALTH_BEHAVIOR_KEYS = HEALTH_V2_BEHAVIOR_KEYS | HEALTH_V1_DEPRECATED_KEYS

  # -------------------- 门面：委托给拆分后的子服务 --------------------
  _INSIGHT_MODULE_KEYS = AnalysisContentService._INSIGHT_MODULE_KEYS
  _analysis_specs = staticmethod(AnalysisContentService._analysis_specs)
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

  def record_publish(self, popup: dict, operator_uid: str, operator_email: str = "") -> dict:
    return self.engagement.record_publish(popup, operator_uid, operator_email)

  def list_publish_records(self, limit: int = 200) -> list:
    return self.engagement.list_publish_records(limit)

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

  @staticmethod
  def _accept_uid_emb(candidate) -> bool:
    """uid_emb 维度校验：超过 MAX_UID_EMB_LEN 一律拒绝（保留旧值），不截断。"""
    if candidate is not None and len(candidate) > UserProfileServ.MAX_UID_EMB_LEN:
      logging.warning(
        "uid_emb rejected: dim=%d exceeds limit %d", len(candidate), UserProfileServ.MAX_UID_EMB_LEN,
      )
      return False
    return True

  @staticmethod
  def _cap_engagement_containers(profile: UserProfile) -> None:
    """四件套落盘前封顶，各自按时间戳挤掉最旧的。

    注意副作用（上限 500 远高于现实量级，正常用户碰不到）：
      - survey_submissions 被挤掉后，同一 survey_id 可以再次提交（幂等键丢失）；
        运营侧全量记录另存全局 KV（SURVEY_RECORD_PREFIX），不受影响
      - popup_states 被挤掉后，该弹窗的频控计数从零开始
    """
    for key in ENGAGEMENT_RESPONSE_KEYS:
      container = getattr(profile, key, None)
      size = len(container) if isinstance(container, (dict, list)) else 0
      if size > UserProfileServ.MAX_ENGAGEMENT_LEN:
        logging.warning("%s truncated: %d -> %d", key, size, UserProfileServ.MAX_ENGAGEMENT_LEN)
        setattr(profile, key, keep_latest_engagement(
          key, container, UserProfileServ.MAX_ENGAGEMENT_LEN))

  @staticmethod
  def _cap_sleep_record_lists(profile: UserProfile) -> None:
    """落盘前给每条 SleepResult 的内嵌序列封顶（防御异常客户端，正常一晚远不到上限）。

    sleep_data 本身有 MAX_SLEEP_DATA_LEN 条数上限，但单条里的 sleep_status /
    night_events 等由客户端直供、此前无任何上限，30 条各塞满就能把画像撑到几十 MB。
    保留序列开头（入睡段），超限只告警不报错——这里是兜底，不是业务校验。
    """
    for record in profile.sleep_data or []:
      for fname, cap in UserProfileServ.MAX_SLEEP_RECORD_LIST_LEN.items():
        values = getattr(record, fname, None)
        if isinstance(values, list) and len(values) > cap:
          logging.warning(
            "sleep_data[ts=%s].%s truncated: %d -> %d",
            record.timestamp, fname, len(values), cap,
          )
          setattr(record, fname, values[:cap])

  def save_profile(self, uid: str, profile: UserProfile):
    """将单个用户的画像写入持久化存储；落盘前 revision +1（query_revision 变更探测版本号）。"""
    with self.lock:
      # sleep_data 落盘不变式：按 timestamp 升序，[-1] 恒为最新一晚。
      # 读路径（query_profile 的 [-N:]）、_night_window、compute_recent_sleep_stats
      # 的 [-days:] 全都依赖这条，这里是所有写入路径的唯一收口，兜住工具脚本等旁路。
      if profile.sleep_data:
        profile.sleep_data.sort(key=lambda r: r.timestamp)
      self._cap_sleep_record_lists(profile)
      self._cap_engagement_containers(profile)
      profile.revision = (profile.revision or 0) + 1
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

  def _health_sample_version(self, profile: UserProfile, key: str, timestamp, fallback_tz) -> int:
    """登记时区与当前查询时区可以不同；先按登记时区找到样本的实际版本。"""
    if key in self.HEALTH_V1_DEPRECATED_KEYS - {"sleep_stage_light"}:
      return 1
    registered_tz = self._resolve_tz(profile.health_sync_timezone or profile.last_request_timezone,
                                   warn=False) if (profile.health_sync_timezone or profile.last_request_timezone) else fallback_tz
    day = self._health_day(timestamp, registered_tz)
    version = profile.health_sync_field_versions.get(day, {}).get(key, profile.health_sync_days.get(day, 1))
    return max(1, version)

  def _apply_health_schema_update(
    self,
    profile: UserProfile,
    new_profile: UserProfile,
    health_schema_version: Optional[int],
    timezone: Optional[str],
    purge: bool = True,
  ) -> None:
    """按字段迁移，避免任意 v2 分片清空同日 core 或其他尚未替换的指标。

    日级版本取现存字段的最低版本；字段级进度防止分批迁移重复清理。
    light 仅在本字段首次 v1→v2 时替换；已为 v2 时合并增量，并忽略同日 v1
    light 回传（两版语义不兼容）。改名字段等对应 v2 数据齐备后才移除旧字段。
    """
    version = health_schema_version or 1
    if version > Config.HEALTH_SCHEMA_VERSION:
      logging.warning("unknown health_schema_version=%s; keep data without migration", version)
    known_v2 = version == 2
    tz = self._resolve_tz(timezone)

    def sample_day(item):
      if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
      return self._health_day(item[0], tz)

    def fields_by_day(behaviors):
      result = {}
      for key, values in (behaviors or {}).items():
        if key not in self.HEALTH_BEHAVIOR_KEYS or not isinstance(values, list):
          continue
        for item in values:
          day = sample_day(item)
          if day:
            result.setdefault(day, set()).add(key)
      return result

    # 新建画像没有旧数据；profile 与 new_profile 可能为同一对象，不能自清理。
    state = {}
    old_only = self.HEALTH_V1_DEPRECATED_KEYS - {"sleep_stage_light"}
    if purge:
      for key in self.HEALTH_BEHAVIOR_KEYS:
        for item in profile.behaviors.get(key, []):
          day = sample_day(item)
          if day:
            fields = state.setdefault(day, {})
            old_version = self._health_sample_version(profile, key, item[0], tz)
            fields[key] = min(fields.get(key, old_version), old_version)

    # 晚到的 v1 light 不得覆盖/混入已经完成迁移的 v2 core。
    if version == 1 and purge:
      values = new_profile.behaviors.get("sleep_stage_light", [])
      kept = [item for item in values if state.get(sample_day(item), {}).get("sleep_stage_light", 1) < 2]
      if len(kept) != len(values):
        logging.warning("ignore %d legacy light samples on migrated v2 days", len(values) - len(kept))
        new_profile.behaviors["sleep_stage_light"] = kept
    incoming = fields_by_day(new_profile.behaviors)
    if not incoming:
      return

    def remove_day(key, day):
      values = profile.behaviors.get(key, [])
      kept = [item for item in values if sample_day(item) != day or (
        key == "sleep_stage_light" and self._health_sample_version(profile, key, item[0], tz) >= 2
      )]
      removed = len(values) - len(kept)
      if removed:
        profile.behaviors[key] = kept
        logging.info("health migration: removed %d legacy %s samples on %s", removed, key, day)
      state.get(day, {}).pop(key, None)

    replacements = {
      "heart_rate": ({"sleep_heart_rate_min", "sleep_heart_rate_max"},),
      "heart_rate_variability_sdnn": ({"sleep_heart_rate_variability_sdnn"},),
      "respiratory_rate": ({"sleep_respiratory_rate"},),
      "body_temperature": ({"sleep_body_temperature"}, {"sleeping_wrist_temperature"}),
    }
    for day, keys in incoming.items():
      fields = state.setdefault(day, {})
      if known_v2 and purge and "sleep_stage_light" in keys and fields.get("sleep_stage_light", 2) < 2:
        remove_day("sleep_stage_light", day)
      elif known_v2 and purge and "sleep_stage_unspecified" in keys:
        # v1 light 混入过 unspecified：迁移时按明确重传的起点剔除对应旧样本，
        # 避免两条轨同时计为 core。未被替换的 v1 light 仍保留并报告版本 1。
        replaced = {item[0] for item in new_profile.behaviors["sleep_stage_unspecified"] if sample_day(item) == day}
        values = profile.behaviors.get("sleep_stage_light", [])
        kept = [item for item in values if item[0] not in replaced or
                self._health_sample_version(profile, "sleep_stage_light", item[0], tz) >= 2]
        if len(kept) != len(values):
          profile.behaviors["sleep_stage_light"] = kept
          logging.info("health migration: moved %d legacy light samples to unspecified on %s", len(values)-len(kept), day)
          if not any(sample_day(item) == day for item in kept):
            fields.pop("sleep_stage_light", None)
      if known_v2:
        # deep/rem 等未改语义的旧字段可直接兼容 v2，不需要破坏性清理。
        for key in fields.keys() - self.HEALTH_V1_DEPRECATED_KEYS:
          fields[key] = 2
      for key in keys:
        if key in old_only:
          fields[key] = 1  # 改名后的旧字段仍是 v1，不能因信封标 v2 就伪报已迁移。
        elif known_v2:
          fields[key] = 2
        elif version == 1:
          fields[key] = min(fields.get(key, 1), 1)
        else:
          fields[key] = 1  # 未知版本保留数据但不宣称已完成已知迁移。
      if known_v2 and purge:
        for old_key, alternatives in replacements.items():
          # 心率 min/max 可以分包抵达，齐备之前保留全天心率，日级版本维持 1。
          if old_key in keys:
            continue
          if any(group & keys and all(fields.get(key) == 2 for key in group) for group in alternatives):
            if old_key == "heart_rate":
              pairs = []
              for key in ("sleep_heart_rate_min", "sleep_heart_rate_max"):
                samples = list(profile.behaviors.get(key, [])) + list(new_profile.behaviors.get(key, []))
                pairs.append({item[0] for item in samples if sample_day(item) == day})
              if not pairs[0] & pairs[1]:
                continue  # 必须是同一会话起点的一对 min/max，不是当天任意两个点。
            remove_day(old_key, day)

    profile.health_sync_field_versions = state
    profile.health_sync_timezone = str(tz)
    profile.health_sync_days = {day: min(versions.values()) for day, versions in state.items() if versions}

  def backfill_sleep_data_inplace(self, profile: UserProfile) -> dict:
    """存量画像回填（tool/backfill_sleep_data.py 调用）：从 behaviors 合成
    sleep_data（device 上报行优先，见 _synthesize_sleep_data）并配对当夜心率区间。
    不动 behaviors、不动版本登记。幂等；返回统计供调用方决定是否落盘。
    """
    stats: dict = {"synthesized": self._synthesize_sleep_data(profile)}
    self._update_night_hr_range(profile)
    return stats

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
          version = self._health_sample_version(profile, key, item[0], tz)
          days[day] = min(days.get(day, version), version)
    return [
      {"date": day, "health_schema_version": days[day]}
      for day in sorted(days)
    ]

  @staticmethod
  def _extract_sop_start_events(plays: list) -> list[tuple[str, int, dict]]:
    """Extract SOP start events from a plays list.

    引导式场景（sleep.scene.）与纯音乐（sleep.pure_music. / 裸 pure_music.）都计入；
    纯音乐不进推荐候选（reco.py 另有过滤），但播放统计与场景同口径。

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
      if isinstance(cmd, str) and cmd.startswith(SCENE_CMD_PREFIXES) and event_type == "sop_start":
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
      # Retried start events must not inflate usage/tag evidence.
      existing = next((i for i, item in enumerate(record) if str(item[0]) == str(ts)), None)
      if existing is None:
        record.append((ts, duration))
      elif duration is not None:
        record[existing] = (ts, duration)
      # keep the list sorted by timestamp and cap the length
      record.sort(key=lambda x: x[0])
      if len(record) > UserProfileServ.MAX_BEHAVIOR_LEN:
        record[:] = record[-UserProfileServ.MAX_BEHAVIOR_LEN:]

  # -------------------- 场景统计 --------------------
  @staticmethod
  def _calc_scene_stats(mindora_record: dict, days: int | None = None, end_ts: int | None = None) -> dict:
    """Compute usage counts and total duration per scene from mindora_record.

    If ``days`` is given, only entries whose timestamp is within the last
    ``days`` days are counted. 窗口终点默认当前时刻；读路径传 ``end_ts``
    锚定最近有效夜（与 analysis_builders 的 anchor 口径一致）。
    """
    cutoff_ts = (int(end_ts) if end_ts else int(time.time())) - days * 86400 if days else 0
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

  @staticmethod
  def _pick_recent_scene(mindora_record: dict) -> Optional[tuple[str, int]]:
    """最近一次使用的场景：全量记录中时间戳最新的一条，返回 (scene_id, ts)。

    与 compute_recent_sleep_stats 的 recent_scene_title 同口径（/analysis 的
    sleep_scenarios 骨架卡用它），这里落地到 profile 供设备轮询展示。
    """
    best_id, best_ts = None, 0
    for scene_id, records in (mindora_record or {}).items():
      if not isinstance(records, list) or not records:
        continue
      for entry in records:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
          try:
            ts = int(entry[0])
          except (TypeError, ValueError):
            continue
          if ts > best_ts:
            best_id, best_ts = scene_id, ts
    return (best_id, best_ts) if best_id else None

  def _update_scene_stats(self, profile: UserProfile):
    """Pre-compute most-used scene (all-time and last 7 days) and persist them in sleep_analysis."""
    now = int(time.time())

    # All-time most used scene
    most_used = self._pick_most_used_scene(profile.mindora_record)
    if most_used is None:
      profile.sleep_analysis.pop("most_used_scene", None)
    else:
      scene_id, scene_stats = most_used
      short_id = short_scene_id(scene_id)
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
      short_id = short_scene_id(scene_id)
      profile.sleep_analysis["most_used_scene_7d"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "count": scene_stats["count"],
        "total_duration": scene_stats["total_duration"],
        "updated_at": now,
      }

    # Recently used scene：最近一次使用的场景（used_at 为使用时间）。
    # 设备端定期拉 query_profile 即可展示，无需单独请求 analysis 接口
    recent = self._pick_recent_scene(profile.mindora_record)
    if recent is None:
      profile.sleep_analysis.pop("recent_scene", None)
    else:
      scene_id, used_at = recent
      short_id = short_scene_id(scene_id)
      profile.sleep_analysis["recent_scene"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "used_at": used_at,
        "updated_at": now,
      }

  @staticmethod
  def _pick_best_sleep_quality_scene(profile: UserProfile, days: int = 7) -> Optional[dict]:
    """Return the scene whose usage before sleep produced the highest avg sleep_quality.

    For each sleep night in the last ``days`` days:
      - Look at scene usages within USAGE_SLEEP_LOOKBACK_SEC before the sleep
        record's timestamp (real data: sleep records are uploaded the next
        morning ~04:47–09:14, scene usage happens the previous evening
        ~15:00–18:30, an observed gap of ~12.7–16 h).
      - Pick the scene usage closest to (but not after) the record timestamp
        as the "effective" scene for that night.
      - Attribute that night's sleep_quality to that scene.

    Note: first_sleep_time is NOT used for alignment — in real data it is None
    for most records, which previously caused every night to be skipped and
    best_sleep_quality_scene_7d to never be produced (weekly_best.score missing).

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
      short_id = short_scene_id(scene_id)
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
    # 不按记录条数切片（一晚可能多条记录，条数切片会缩短实际天数窗口），
    # 窗口完全由上面的 cutoff_ts（自然时间）控制
    scene_qualities: dict[str, list[float]] = {}
    for record in profile.sleep_data:
      if record.timestamp < cutoff_ts:
        continue
      if record.sleep_quality is None:
        continue

      # Pick the scene usage closest to (but not after) the record timestamp,
      # within the lookback window — i.e. the usage from "that night".
      best_usage = None
      best_delta = None
      for usage in usages:
        delta = record.timestamp - usage["timestamp"]
        if delta < 0 or delta > UserProfileServ.USAGE_SLEEP_LOOKBACK_SEC:
          continue
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
      if fname == "avatar_base64" and len(value) > UserProfileServ.MAX_AVATAR_BASE64_LEN:
        logging.warning(
          "avatar_base64 rejected: len=%d exceeds limit %d, keeping previous avatar",
          len(value), UserProfileServ.MAX_AVATAR_BASE64_LEN,
        )
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

    口径（健康数据同步接口_0814.md §3/§8.2）：客户端直接上传 sleep_heart_rate_min/max
    （一晚一对，时间戳同为该晚睡眠会话起点），按会话起点落入当夜窗口配对写入；
    每次 update 幂等重算。v1 的 heart_rate 全天序列不再兜底。
    """
    from sleep_session_builder import SOURCE_HEALTHKIT
    v2_min = self._hr_pairs_from_points(profile.behaviors.get("sleep_heart_rate_min"))
    v2_max = self._hr_pairs_from_points(profile.behaviors.get("sleep_heart_rate_max"))
    v2_pairs = [(ts, v2_min[ts], v2_max[ts]) for ts in v2_min.keys() & v2_max.keys()]

    if not v2_pairs:
      return
    for record in profile.sleep_data:
      window = self._night_window(record)
      if window is None:
        continue
      matched = [(mn, mx) for ts, mn, mx in v2_pairs if window[0] <= ts <= window[1]]
      if matched:
        record.hr_min = min(mn for mn, _mx in matched)
        record.hr_max = max(mx for _mn, mx in matched)
        # v2 只传当晚心率 min/max（md §3），没有心率序列，真均值算不出来。
        # md §9 的周/月视图口径本就是「按晚等权的平均值」，这里用区间中点补上，
        # 否则 avg_heart_rate 对纯 HealthKit 用户恒为 null，日视图 Vitals 与
        # compute_recent_sleep_stats → LLM 全都拿不到心率。
        # ⚠️ 是区间中点，不是时间加权均值；只补服务端合成行，设备上报的真实均值不覆盖。
        if record.source == SOURCE_HEALTHKIT:
          record.avg_heart_rate = round((record.hr_min + record.hr_max) / 2, 1)

  def _synthesize_sleep_data(self, profile: UserProfile) -> int:
    """从 v2 健康 behaviors 合成每晚 SleepResult（sleep_session_builder，source="healthkit"）。
    返回本次新增（替换）的行数。

    iOS 健康同步只传 behaviors 阶段/体征序列、从不上报 sleep_data；分析链路全读
    sleep_data，不合成的话纯 HealthKit 用户分析页永远空。规则：
      - 合成行时间戳落在某当前会话窗口内 → 丢弃，由本次重算的新行替换
        （§4.2 同晚修正值时间戳不变、时长变长，重算自然覆盖）；
      - 会话窗口内已有设备上报行（source 非 healthkit）→ 跳过该晚，设备数据优先；
      - behaviors 截断导致老会话消失时，其合成行保留（sleep_data 保留 30 晚，
        比 behaviors 窗口长是特性不是泄漏）。
    必须在 _update_night_hr_range 之前调用（它给合成行配对当夜心率区间）。
    """
    from sleep_session_builder import derive_sleep_results, SOURCE_HEALTHKIT
    derived = derive_sleep_results(
      profile.behaviors or {},
      self._resolve_tz(getattr(profile, "last_request_timezone", None), warn=False),
    )
    if not derived:
      return 0
    kept = []
    for row in profile.sleep_data or []:
      if row.source == SOURCE_HEALTHKIT and any(s <= row.timestamp <= e for s, e, _r in derived):
        continue  # 同晚合成行：被本次重算替换
      kept.append(row)
    added = 0
    for s, e, row in derived:
      if any(r.source != SOURCE_HEALTHKIT and s <= r.timestamp <= e for r in kept):
        continue  # 该晚已有设备上报行，设备数据优先
      kept.append(row)
      added += 1
    if added:
      logging.info("synthesized %d healthkit sleep_data rows (total %d)", added, len(kept))
    profile.sleep_data = sorted(kept, key=lambda r: r.timestamp)[-UserProfileServ.MAX_SLEEP_DATA_LEN:]
    return added

  # 新建画像时允许客户端写入的字段（与合并路径实际采信的字段保持一致）：
  # 合并路径只动 uid_emb / profile / sleep_health / sleep_mode / behaviors / sleep_data，
  # long_term_profile 走 _merge_profile（恒返回旧值，即不采信），其余全是服务端独占。
  CLIENT_OWNED_PROFILE_FIELDS = (
    "uid_emb", "profile", "sleep_health", "sleep_mode", "behaviors", "sleep_data",
  )

  @staticmethod
  def _new_profile_from_client(new_profile: UserProfile) -> UserProfile:
    """客户端首包 → 新画像：只拷贝 CLIENT_OWNED_PROFILE_FIELDS，其余取服务端默认值。"""
    profile = UserProfile()
    dropped = []
    for fname in type(new_profile).model_fields:
      if fname in UserProfileServ.CLIENT_OWNED_PROFILE_FIELDS:
        value = getattr(new_profile, fname)
        if fname == "uid_emb" and not UserProfileServ._accept_uid_emb(value):
          continue  # 保留默认空向量
        if fname == "sleep_data":
          # 与后续每次更新同一套归一：按 timestamp 升序、同 ts 去重、保留最近 30 晚。
          # 首包直接照抄客户端数组的话，乱序上报会让「最新一晚」落在数组中间，
          # 读路径的 [-N:] 就会取错夜。
          value = UserProfileServ._merge_sleep_data([], value)
        setattr(profile, fname, value)
      elif fname in new_profile.model_fields_set:
        dropped.append(fname)
    if dropped:
      logging.warning("new profile: dropped server-owned fields from client payload: %s", dropped)
    return profile

  def _update_goal_achieved(self, profile: UserProfile) -> None:
    """按当前生效的睡眠计划回填每晚 goal_achieved（本晚睡眠目标完成度，0-100）。

    口径：TST（sequence_summaries.total_sleep_duration）/ 计划 target_minutes × 100，
    上限 100。只算落在计划生效窗口 [activated_at, now] 内的夜晚——计划昨天才启用，
    不该给上个月的夜晚打分；无 active 计划时不动任何记录（保持既有值）。

    设备上报行（source 非 healthkit）若已自带值则保留：设备侧按自己的计划算过。
    每次 update 幂等重算，计划改目标后旧夜晚会跟着刷新。
    """
    from sleep_session_builder import SOURCE_HEALTHKIT
    plan = active_sleep_plan(profile)
    if plan is None or plan.activated_at is None:
      return
    target = plan.target_minutes
    now = int(time.time())
    for record in profile.sleep_data or []:
      if not (plan.activated_at <= record.timestamp <= now):
        continue
      if record.source != SOURCE_HEALTHKIT and record.goal_achieved is not None:
        continue
      summ = record.sequence_summaries if record.sleep_status else None
      if not summ:
        continue
      tst = summ.get("total_sleep_duration") or 0
      record.goal_achieved = round(min(tst / target * 100, 100.0), 1)

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
    # 版本登记时区独立保存，切换请求时区不能把已迁移数据重新误认为 v1。
    if profile is not None and not profile.health_sync_timezone:
      profile.health_sync_timezone = profile.last_request_timezone or timezone or "UTC"
    # 记录最近请求环境：每日 LLM 触发门的自然日口径 + 分析文案语言
    self._note_request_meta(new_profile, timezone, language)
    if profile is not None:
      self._note_request_meta(profile, timezone, language)

    if profile is None:
      # 新建画像：只采信客户端自有字段（与合并路径同一张白名单），其余一律取默认值。
      # 此前这里直接落库整个 new_profile，等于把 analysis_reports / sop_tag_profile /
      # mindora_record / inbox_messages / sleep_plans 等服务端独占字段在「首次 update」
      # 这一刻开放给客户端写入，且不过任何条数上限（sleep_plans 还能绕过额度与校验）。
      # revision 一律作废，从 0 起（save 时 +1 变 1）。
      new_profile = self._new_profile_from_client(new_profile)
      new_profile.profile = self._merge_personal_profile(None, new_profile.profile)
      self._apply_health_schema_update(new_profile, new_profile, health_schema_version, timezone, purge=False)
      # 新画像也要把本批 plays 的 sop_start 聚合进 mindora_record，
      # 否则首包场景事件丢失，most_used_scene 永远为空
      self._update_mindora_record(new_profile, new_profile)
      self._update_scene_stats(new_profile)
      self._update_best_scene_by_sleep_quality(new_profile)
      self._synthesize_sleep_data(new_profile)
      self._update_night_hr_range(new_profile)
      self._update_goal_achieved(new_profile)
      rebuild_sop_tag_profile(new_profile)
      return new_profile

    # just replace, if need
    if (len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0) \
        and self._accept_uid_emb(new_profile.uid_emb):
      profile.uid_emb = new_profile.uid_emb

    profile.profile = self._merge_personal_profile(profile.profile, new_profile.profile)
    # These are ranking inputs; sparse updates must preserve existing answers.
    for field in ("sleep_health", "sleep_mode"):
      if field in new_profile.model_fields_set:
        incoming = getattr(new_profile, field)
        previous = getattr(profile, field)
        if incoming is not None and previous is not None:
          incoming = type(incoming).model_validate({**previous.model_dump(), **incoming.model_dump(exclude_unset=True)})
        setattr(profile, field, incoming)
    profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)
    # 版本登记 + v1 purge 必须在 merge 前：purge 清的是存量里被覆盖天的旧口径样本
    self._apply_health_schema_update(profile, new_profile, health_schema_version, timezone)
    profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)
    profile.sleep_data = self._merge_sleep_data(profile.sleep_data, new_profile.sleep_data)
    self._synthesize_sleep_data(profile)

    # aggregate SOP play events into mindora_record so we can keep behaviors small
    self._update_mindora_record(profile, new_profile)
    self._update_scene_stats(profile)
    self._update_best_scene_by_sleep_quality(profile)
    self._update_night_hr_range(profile)
    self._update_goal_achieved(profile)
    rebuild_sop_tag_profile(profile)
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
    bedtime_advice = self.content.calc_bedtime_advice(uid, profile)
    if bedtime_advice is not None:
      profile.bedtime_advice = bedtime_advice

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
    bedtime_advice = self.content.calc_bedtime_advice(uid, llm_profile)

    with self.lock:
      profile = self.get_profile(uid)
      if profile is None:
        logging.warning(f"skip llm update: profile disappeared for uid={uid}")
        return False
      if sleep_scenarios is not None:
        profile.sleep_scenarios_reco = sleep_scenarios
      if standard_sop is not None:
        profile.standard_sop_reco = standard_sop
        profile.sop_recommendation_details = llm_profile.sop_recommendation_details
      elif not profile.standard_sop_reco:
        # reco 被跳过时，兜底保证 standard_sop_reco 不为空
        profile.standard_sop_reco = self.calc_standard_sop_reco(uid, profile, old_profile)
      if sleep_insight:
        profile.sleep_insight = sleep_insight
      if analysis_reports is not None:
        profile.analysis_reports = analysis_reports
      if bedtime_advice is not None and bedtime_advice_due(profile):
        profile.bedtime_advice = bedtime_advice
      self.save_profile(uid, profile)
      logging.info("Profile llm updated uid=%s", uid)
      return True

  def close(self):
    if self.db is not None:
      self.db.close()
