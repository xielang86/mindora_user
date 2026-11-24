"""健康数据口径版本（健康数据同步接口_0814.md §8）的服务端落地测试：

- merge_two_sorted_dedup：同 ts 新值覆盖旧值（§4.2 修正值重发）
- update_profile_basic：按 timezone 归自然日登记 health_sync_days（缺省版本 1）
- v2 批次 purge 当天 v1 废弃 key 旧样本（版本收敛）
- per-key 容量：v2 key 5000，旧 key 仍 100
- query_health_sync_state：有数据的天按实际时间戳现算，版本查登记缺省 1
- hr_range：v2 sleep_heart_rate_min/max 配对优先，v1 heart_rate 窗口兜底
"""
import asyncio
import datetime
import threading
import time

import pytest

from common import util
from config import Config
from user_profile import ProfileData, ProfileRequest, SleepResult, UserProfile
from user_server import UserProfileServ, UserServer


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  return serv


def _night(timestamp: int, quality: float = 80.0) -> SleepResult:
  from user_profile import SleepElement
  status = [SleepElement(start_time=timestamp - 8 * 3600, duration=480, sleep_type="core")]
  return SleepResult(timestamp=timestamp, sleep_quality=quality, sleep_status=status)


UTC = datetime.timezone.utc


def _ts(day: str, hour: int = 12) -> int:
  """UTC 某日某时的 Unix 秒。"""
  d = datetime.date.fromisoformat(day)
  return int(datetime.datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp())


# ── 合并语义（§4.2）────────────────────────────────────────────

def test_merge_two_sorted_dedup_new_value_overwrites():
  a = [(100, 60.0), (200, 70.0)]
  b = [(200, 75.5), (300, 80.0)]
  merged = util.merge_two_sorted_dedup(a, b)
  assert merged == [(100, 60.0), (200, 75.5), (300, 80.0)]


def test_merge_behavior_correction_overwrites_same_ts():
  serv = _serv()
  old = {"sleep_in_bed": [[_ts("2026-08-13"), 21600.0]]}
  new = {"sleep_in_bed": [[_ts("2026-08-13"), 23200.0]]}  # 同 ts 修正值（会话还在增长）
  merged = serv._merge_behavior(old, new)
  assert merged["sleep_in_bed"] == [[_ts("2026-08-13"), 23200.0]]


# ── 版本登记（§8.3）────────────────────────────────────────────

def test_health_days_registered_with_version_and_timezone():
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], behaviors={
    "sleep_stage_deep": [[_ts("2026-08-13", 22), 1800.0]],
    "sleep_heart_rate_min": [[_ts("2026-08-13", 23), 56.0]],
  })
  merged = serv._apply_basic_update(
    "u", new_profile, None, health_schema_version=2, timezone="UTC",
  )
  assert merged.health_sync_days == {"2026-08-13": 2}


def test_health_days_default_version_1_when_absent():
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], behaviors={
    "heart_rate": [[_ts("2026-08-13"), 78.0]],
  })
  merged = serv._apply_basic_update("u", new_profile, None, timezone="UTC")
  assert merged.health_sync_days == {"2026-08-13": 1}


def test_health_days_respect_timezone_day_boundary():
  # 2026-08-14 00:30 Asia/Shanghai = 2026-08-13 16:30 UTC
  ts = int(datetime.datetime(2026, 8, 13, 16, 30, tzinfo=UTC).timestamp())
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], behaviors={"sleep_in_bed": [[ts, 100.0]]})
  merged = serv._apply_basic_update(
    "u", new_profile, None, health_schema_version=2, timezone="Asia/Shanghai",
  )
  assert list(merged.health_sync_days) == ["2026-08-14"]


def test_interaction_keys_do_not_register_health_days():
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], behaviors={
    "plays": [[_ts("2026-08-13"), {"cmd": "sleep.scene.kyoto_forest", "event": "sop_start"}]],
    "clicks": [[_ts("2026-08-13"), "play"]],
  })
  merged = serv._apply_basic_update(
    "u", new_profile, None, health_schema_version=2, timezone="UTC",
  )
  assert merged.health_sync_days == {}


# ── v1 purge 与版本收敛（§8.4 同天多版本取最低的闭环）────────────────

def test_v2_partial_batch_keeps_unreplaced_legacy_fields():
  serv = _serv()
  existing = UserProfile(uid_emb=[], behaviors={
    "heart_rate": [[_ts("2026-08-12"), 70.0], [_ts("2026-08-13"), 78.0]],
    "sleep_stage_light": [[_ts("2026-08-13"), 3600.0]],
    "sleep_stage_deep": [[_ts("2026-08-13"), 1800.0]],  # 语义未变，不动
  })
  new_profile = UserProfile(uid_emb=[], behaviors={
    "sleep_heart_rate_min": [[_ts("2026-08-13", 22), 56.0]],
  })
  merged = serv._apply_basic_update(
    "u", new_profile, existing, health_schema_version=2, timezone="UTC",
  )
  # 仅 min 不足以替换旧全天心率；无 core 分片，不能清理旧 light。
  assert len(merged.behaviors["heart_rate"]) == 2
  assert len(merged.behaviors["sleep_stage_light"]) == 1
  assert len(merged.behaviors["sleep_stage_deep"]) == 1
  assert merged.health_sync_days["2026-08-13"] == 1


def test_v1_batch_after_v2_downgrades_day_back_to_1():
  serv = _serv()
  existing = UserProfile(
    uid_emb=[],
    behaviors={"sleep_in_bed": [[_ts("2026-08-13"), 21600.0]]},
    health_sync_days={"2026-08-13": 2},
  )
  new_profile = UserProfile(uid_emb=[], behaviors={
    "heart_rate": [[_ts("2026-08-13"), 78.0]],  # 老设备/老客户端补发 v1 数据
  })
  merged = serv._apply_basic_update("u", new_profile, existing, timezone="UTC")
  assert merged.health_sync_days["2026-08-13"] == 1  # 混合后回落最低版本
  assert len(merged.behaviors["heart_rate"]) == 1     # v1 数据正常入库


# ── per-key 容量 ────────────────────────────────────────────────

def test_v2_keys_use_larger_cap():
  serv = _serv()
  old = {"sleep_stage_awake": [[i, 60.0] for i in range(80)]}
  new = {"sleep_stage_awake": [[i, 60.0] for i in range(80, 200)]}
  merged = serv._merge_behavior(old, new)
  assert len(merged["sleep_stage_awake"]) == 200  # 不再按 100 截断

  old = {"heart_rate": [[i, 60.0] for i in range(60)]}
  new = {"heart_rate": [[i, 100 + i] for i in range(60, 160)]}
  merged = serv._merge_behavior(old, new)
  assert len(merged["heart_rate"]) == UserProfileServ.MAX_BEHAVIOR_LEN


def test_all_v2_field_table_keys_use_larger_cap():
  """md §3 字段表全集都要 5000 上限：deep/rem/light 一晚 5~30 段，100 上限连
  30 天首同步窗口都装不下——截断后 §8.4 对账天丢失（客户端无限重传），
  服务端合成 SleepResult 也丢阶段。"""
  serv = _serv()
  for key in ("sleep_stage_deep", "sleep_stage_rem", "sleep_stage_light",
              "resting_heart_rate", "sleeping_wrist_temperature"):
    old = {key: [[i, 60.0] for i in range(80)]}
    new = {key: [[i, 60.0] for i in range(80, 200)]}
    merged = serv._merge_behavior(old, new)
    assert len(merged[key]) == 200, f"{key} still capped at 100"


# ── 首次上传自清洗回归 ───────────────────────────────────────────

def test_first_upload_keeps_own_v2_light_samples():
  """全新画像的首次 v2 上传：本批 sleep_stage_light 是新口径数据，
  不能被自己触发的 v1 purge 清掉（purge 只针对存量）。"""
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], behaviors={
    "sleep_stage_light": [[_ts("2026-08-13", 1), 3600.0]],
    "sleep_stage_deep": [[_ts("2026-08-13", 2), 1800.0]],
  })
  merged = serv._apply_basic_update(
    "u", new_profile, None, health_schema_version=2, timezone="UTC",
  )
  assert len(merged.behaviors["sleep_stage_light"]) == 1
  assert len(merged.behaviors["sleep_stage_deep"]) == 1
  assert merged.health_sync_days["2026-08-13"] == 2


# ── 对账查询（§8.4）────────────────────────────────────────────

def test_query_health_sync_days_legacy_profile_defaults_v1():
  """老画像（无 health_sync_days）：天从实际数据现算，版本缺省 1。"""
  serv = _serv()
  profile = UserProfile(uid_emb=[], behaviors={
    "heart_rate": [[_ts("2026-08-12"), 70.0], [_ts("2026-08-13"), 78.0]],
    "sleep_stage_rem": [[_ts("2026-08-13", 22), 1200.0]],
    "plays": [[_ts("2026-08-11"), {"cmd": "x"}]],  # 交互行为不算
  })
  serv.get_profile = lambda uid: profile
  days = serv.query_health_sync_days("u", "2026-08-01", "2026-08-31", "UTC")
  assert days == [
    {"date": "2026-08-12", "health_schema_version": 1},
    {"date": "2026-08-13", "health_schema_version": 1},
  ]


def test_query_health_sync_days_uses_registered_version_and_window():
  serv = _serv()
  profile = UserProfile(
    uid_emb=[],
    behaviors={
      "sleep_in_bed": [[_ts("2026-08-13"), 21600.0]],
      "heart_rate": [[_ts("2026-08-12"), 70.0]],
      "sleep_stage_deep": [[_ts("2026-07-01"), 1800.0]],  # 窗口外
    },
    health_sync_days={"2026-08-13": 2},
  )
  serv.get_profile = lambda uid: profile
  days = serv.query_health_sync_days("u", "2026-08-10", "2026-08-14", "UTC")
  assert days == [
    {"date": "2026-08-12", "health_schema_version": 1},  # 未登记 → 缺省 1
    {"date": "2026-08-13", "health_schema_version": 2},
  ]


def test_query_health_sync_days_no_profile_returns_none():
  serv = _serv()
  serv.get_profile = lambda uid: None
  assert serv.query_health_sync_days("u", "2026-08-01", "2026-08-31", "UTC") is None


# ── hr_range v2 路径 ───────────────────────────────────────────

def test_hr_range_v2_pairs_preferred_over_v1():
  serv = _serv()
  night_ts = _ts("2026-08-13", 7)  # 当夜窗口 [23:00 前一天, 07:00]
  session_start = night_ts - 8 * 3600
  profile = UserProfile(
    uid_emb=[],
    behaviors={
      "sleep_heart_rate_min": [[session_start, 54.0]],
      "sleep_heart_rate_max": [[session_start, 88.0]],
      "heart_rate": [[session_start + 60, 130.0]],  # v1 噪声不应盖过 v2
    },
    sleep_data=[_night(night_ts)],
  )
  serv._update_night_hr_range(profile)
  assert profile.sleep_data[0].hr_min == 54.0
  assert profile.sleep_data[0].hr_max == 88.0


def test_hr_range_not_computed_without_v2_pairs():
  """没有 v2 配对时不再用 v1 heart_rate 全天序列兜底，保持 None。"""
  serv = _serv()
  night_ts = _ts("2026-08-13", 7)
  profile = UserProfile(
    uid_emb=[],
    behaviors={"heart_rate": [[night_ts - 4 * 3600, 62.0], [night_ts - 3600, 91.0]]},
    sleep_data=[_night(night_ts)],
  )
  serv._update_night_hr_range(profile)
  assert profile.sleep_data[0].hr_min is None
  assert profile.sleep_data[0].hr_max is None


# ── HTTP handler ───────────────────────────────────────────────

def _server_with(days):
  server = UserServer.__new__(UserServer)
  server.debug_uid_set = {"test_debug_user_001"}

  class FakeUserServ:
    def query_health_sync_days(self, uid, start_date, end_date, timezone):
      return days

  server.user_serv = FakeUserServ()
  return server


def _sync_state_request(data: dict) -> ProfileRequest:
  return ProfileRequest(
    request_type="query_health_sync_state",
    timestamp=int(time.time()),
    version="1.0",
    data=ProfileData.model_validate({"uid": "test_debug_user_001", **data}),
  )


def test_handle_query_health_sync_state_returns_days():
  days = [{"date": "2026-08-13", "health_schema_version": 2}]
  server = _server_with(days)
  resp = server.handle_query_health_sync_state(_sync_state_request({
    "timezone": "Asia/Shanghai", "start_date": "2026-07-16", "end_date": "2026-08-14",
  }))
  assert resp.code == 0
  assert resp.data == {"days": days}


def test_handle_query_health_sync_state_requires_dates():
  server = _server_with([])
  resp = server.handle_query_health_sync_state(_sync_state_request({"timezone": "UTC"}))
  assert resp.code == 400


def test_handle_query_health_sync_state_rejects_inverted_window():
  server = _server_with([])
  resp = server.handle_query_health_sync_state(_sync_state_request({
    "start_date": "2026-08-14", "end_date": "2026-07-16",
  }))
  assert resp.code == 400


def test_handle_query_health_sync_state_unknown_user_returns_empty_days():
  server = _server_with(None)
  resp = server.handle_query_health_sync_state(_sync_state_request({
    "start_date": "2026-07-16", "end_date": "2026-08-14",
  }))
  assert resp.code == 0
  assert resp.data == {"days": []}


def test_profile_data_accepts_health_schema_fields():
  data = ProfileData.model_validate({
    "uid": "test_debug_user_001",
    "language": "en",
    "timezone": "Asia/Shanghai",
    "health_schema_version": 2,
  })
  assert data.health_schema_version == 2
  assert data.timezone == "Asia/Shanghai"


# ── 存量回填（tool/backfill_sleep_data.py 的核心逻辑）────────────────

def _v2_night_behaviors(day: str):
  """一晚 v2 behaviors：23:00 上床，23:10-07:00 多段睡眠 + 当夜心率区间。"""
  in_bed = _ts(day, 0) - 3600  # 前一天 23:00
  first = in_bed + 600         # 23:10 首段
  return {
    "sleep_in_bed": [[in_bed, 8 * 3600]],
    "sleep_stage_light": [[first, 3600.0], [first + 7200, 5 * 3600]],
    "sleep_stage_deep": [[first + 3600, 3600.0]],
    "sleep_stage_rem": [[first + 5400, 1800.0]],
    "sleep_heart_rate_min": [[first, 55.0]],
    "sleep_heart_rate_max": [[first, 82.0]],
  }, first


def test_backfill_sleep_data_from_behaviors():
  """存量画像（behaviors 有、sleep_data 空）→ 合成回填 + 心率配对；
  behaviors 与版本登记不动。"""
  serv = _serv()
  beh, session_start = _v2_night_behaviors("2026-08-14")
  days = {"2026-08-13": 2, "2026-08-14": 2}
  profile = UserProfile(uid_emb=[], behaviors=beh, health_sync_days=days)
  profile.last_request_timezone = "UTC"

  stats = serv.backfill_sleep_data_inplace(profile)

  assert stats["synthesized"] == 1
  row = profile.sleep_data[0]
  assert row.source == "healthkit"
  assert row.sleep_quality is not None
  assert len(row.sleep_status) == 4          # light 两段 + deep + rem
  assert row.hr_min == 55.0 and row.hr_max == 82.0
  assert len(profile.behaviors["sleep_stage_light"]) == 2  # behaviors 不动
  assert profile.health_sync_days == days                  # 登记不动


def test_backfill_sleep_data_idempotent():
  serv = _serv()
  beh, _s = _v2_night_behaviors("2026-08-14")
  profile = UserProfile(uid_emb=[], behaviors=beh,
                        health_sync_days={"2026-08-13": 2, "2026-08-14": 2})
  profile.last_request_timezone = "UTC"
  serv.backfill_sleep_data_inplace(profile)
  stats = serv.backfill_sleep_data_inplace(profile)
  assert stats["synthesized"] == 1  # 重算替换同窗口合成行（幂等，行数不变）
  assert len(profile.sleep_data) == 1
