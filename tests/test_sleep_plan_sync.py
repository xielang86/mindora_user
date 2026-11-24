"""睡眠计划同步（睡眠计划同步接口.md）测试。

覆盖：
  1. 合并引擎 sleep_plan_service：LWW/墓碑/额度/唯一 active/官方去重/完成判定/字段校验
  2. /sleep_plan 端点：sync_plans 落库、query_plans 拉全量、401、quota/tier 下发
"""
import time
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import sleep_plan_service as sps
from user_profile import SyncedSleepPlan, SleepPlanCycle, UserProfile

NOW = int(time.time())
DAY = 86400


def make_plan(plan_id="PLAN-0001", *, now=NOW, **overrides) -> SyncedSleepPlan:
  base = dict(
    plan_id=plan_id,
    kind="custom",
    custom_name="工作日早睡",
    target_minutes=480,
    sleep_time=1350,   # 22:30
    wake_time=390,     # 06:30
    cycle=SleepPlanCycle(type="weekdays", weekdays=[1, 2, 3, 4, 5], repeats_weekly=True),
    reminder_enabled=True,
    reminder_minutes_before=30,
    alarm_sound_id="sunlit_coast",
    sleep_aid_category="smart",
    status="not_enabled",
    timezone="Asia/Shanghai",
    created_at=now - 100,
    updated_at=now,
    activated_at=None,
    deleted=False,
  )
  base.update(overrides)
  return SyncedSleepPlan(**base)


def make_official(plan_id="PLAN-O1", kind="habit_formation", **overrides):
  return make_plan(
    plan_id, kind=kind, custom_name="",
    cycle=SleepPlanCycle(type="fixed_daily", days_count=sps.OFFICIAL_DAYS_COUNT[kind]),
    **overrides,
  )


def tombstone(plan_id="PLAN-0001", *, ts=NOW):
  return SyncedSleepPlan(plan_id=plan_id, deleted=True, deleted_at=ts, updated_at=ts)


# ── ① 冲突解决：updated_at 后写胜出 ──────────────────────────────────────────

class TestLastWriteWins:
  def test_incoming_newer_wins(self):
    old = make_plan(updated_at=NOW - 10, custom_name="旧名字")
    new = make_plan(updated_at=NOW, custom_name="新名字")
    result = sps.sync_plans([old], [new], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.plans[0].custom_name == "新名字"
    assert result.changed

  def test_equal_updated_at_server_wins(self):
    old = make_plan(updated_at=NOW, custom_name="服务端")
    same = make_plan(updated_at=NOW, custom_name="客户端")
    result = sps.sync_plans([old], [same], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.plans[0].custom_name == "服务端"
    assert not result.changed

  def test_incoming_older_ignored(self):
    old = make_plan(updated_at=NOW, custom_name="新")
    stale = make_plan(updated_at=NOW - 50, custom_name="旧")
    result = sps.sync_plans([old], [stale], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.plans[0].custom_name == "新"
    assert not result.changed

  def test_tombstone_deletes_when_newer(self):
    old = make_plan(updated_at=NOW - 10)
    result = sps.sync_plans([old], [tombstone(ts=NOW)], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.plans[0].deleted is True
    assert result.visible_plans == []
    assert result.changed

  def test_tombstone_older_than_server_ignored(self):
    old = make_plan(updated_at=NOW)
    result = sps.sync_plans([old], [tombstone(ts=NOW - 10)], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.plans[0].deleted is False

  def test_tombstone_requires_deleted_at(self):
    bad = SyncedSleepPlan(plan_id="P1", deleted=True, updated_at=NOW)
    result = sps.sync_plans([], [bad], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == [{"plan_id": "P1", "reason": "invalid_plan"}]

  def test_recreate_after_delete_consumes_quota(self):
    """删后重建视为新增（占额度）。"""
    ts = tombstone(ts=NOW - 10)
    new = make_plan(updated_at=NOW)
    result = sps.sync_plans([ts], [new], effective_level="pro", tz_name="Asia/Shanghai")
    assert result.rejected == []
    assert len(result.visible_plans) == 1


# ── ② 额度校验 ───────────────────────────────────────────────────────────────

class TestQuota:
  def test_free_tier_rejects_new_plan(self):
    result = sps.sync_plans([], [make_plan()], effective_level="free", tz_name="Asia/Shanghai")
    assert result.rejected == [{"plan_id": "PLAN-0001", "reason": "tier_not_allowed"}]
    assert result.visible_plans == []

  def test_basic_allows_one_rejects_second(self):
    existing = make_plan("PLAN-A")
    new = make_plan("PLAN-B", updated_at=NOW)
    result = sps.sync_plans([existing], [new], effective_level="pro", tz_name="Asia/Shanghai")
    assert result.rejected == [{"plan_id": "PLAN-B", "reason": "quota_exceeded"}]
    assert len(result.visible_plans) == 1

  def test_premium_allows_five(self):
    batch = [make_plan(f"PLAN-{i}", updated_at=NOW) for i in range(5)]
    result = sps.sync_plans([], batch, effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == []
    assert len(result.visible_plans) == 5
    # 第 6 条超额
    result2 = sps.sync_plans(result.plans, [make_plan("PLAN-6", updated_at=NOW)], effective_level="premium", tz_name="Asia/Shanghai")
    assert result2.rejected == [{"plan_id": "PLAN-6", "reason": "quota_exceeded"}]

  def test_update_existing_does_not_consume_quota(self):
    """basic 用户更新已有计划不受额度限制。"""
    existing = make_plan("PLAN-A", updated_at=NOW - 10)
    update = make_plan("PLAN-A", updated_at=NOW, custom_name="改名")
    result = sps.sync_plans([existing], [update], effective_level="pro", tz_name="Asia/Shanghai")
    assert result.rejected == []
    assert result.plans[0].custom_name == "改名"

  def test_completed_and_stopped_still_occupy_quota(self):
    """not_enabled/active/stopped/completed 都占额度（§6②）。"""
    existing = make_plan("PLAN-A", status="completed")
    new = make_plan("PLAN-B", updated_at=NOW)
    result = sps.sync_plans([existing], [new], effective_level="pro", tz_name="Asia/Shanghai")
    assert result.rejected == [{"plan_id": "PLAN-B", "reason": "quota_exceeded"}]

  def test_tombstone_frees_quota(self):
    existing = make_plan("PLAN-A", updated_at=NOW - 20)
    batch = [tombstone("PLAN-A", ts=NOW - 10), make_plan("PLAN-B", updated_at=NOW)]
    result = sps.sync_plans([existing], batch, effective_level="pro", tz_name="Asia/Shanghai")
    assert result.rejected == []
    assert [p.plan_id for p in result.visible_plans] == ["PLAN-B"]


# ── ③ 唯一开启约束 ───────────────────────────────────────────────────────────

class TestSingleActive:
  def test_batch_with_two_active_keeps_latest_activated(self):
    a = make_plan("PLAN-A", status="active", activated_at=NOW - 100, updated_at=NOW)
    b = make_plan("PLAN-B", status="active", activated_at=NOW - 10, updated_at=NOW)
    result = sps.sync_plans([], [a, b], effective_level="premium", tz_name="Asia/Shanghai")
    by_id = {p.plan_id: p for p in result.visible_plans}
    assert by_id["PLAN-B"].status == "active"
    assert by_id["PLAN-A"].status == "not_enabled"
    assert by_id["PLAN-A"].activated_at is None

  def test_stored_active_demoted_by_newer_active(self):
    stored = make_plan("PLAN-A", status="active", activated_at=NOW - 500)
    new = make_plan("PLAN-B", status="active", activated_at=NOW, updated_at=NOW)
    result = sps.sync_plans([stored], [new], effective_level="premium", tz_name="Asia/Shanghai")
    by_id = {p.plan_id: p for p in result.visible_plans}
    assert by_id["PLAN-B"].status == "active"
    assert by_id["PLAN-A"].status == "not_enabled"

  def test_non_active_plan_activated_at_cleared(self):
    """不变式：status != active 时 activated_at 必须为 null。"""
    plan = make_plan(status="not_enabled", activated_at=NOW - 10, updated_at=NOW)
    result = sps.sync_plans([], [plan], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.visible_plans[0].activated_at is None


# ── ④ 官方预设去重 ───────────────────────────────────────────────────────────

class TestOfficialDedup:
  def test_same_kind_different_id_rejected_and_merged(self):
    existing = make_official("PLAN-O1", updated_at=NOW - 100)
    dup = make_official("PLAN-O2", updated_at=NOW)
    result = sps.sync_plans([existing], [dup], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == [{"plan_id": "PLAN-O2", "reason": "duplicate_official_kind"}]
    visible = result.visible_plans
    assert len(visible) == 1
    assert visible[0].plan_id == "PLAN-O1"  # 合并到已有那条
    assert visible[0].updated_at == NOW     # 内容已刷新

  def test_same_kind_update_same_id_ok(self):
    existing = make_official("PLAN-O1", updated_at=NOW - 100)
    update = make_official("PLAN-O1", updated_at=NOW)
    result = sps.sync_plans([existing], [update], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == []

  def test_different_official_kinds_coexist(self):
    batch = [make_official("PLAN-O1", "habit_formation", updated_at=NOW),
             make_official("PLAN-O2", "jet_lag", updated_at=NOW)]
    result = sps.sync_plans([], batch, effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == []
    assert len(result.visible_plans) == 2


# ── ⑤ 完成判定 ───────────────────────────────────────────────────────────────

class TestCompletion:
  TZ = "Asia/Shanghai"

  def test_official_completed_after_days_count(self):
    activated = NOW - 21 * DAY - 60
    plan = make_official(status="active", activated_at=activated, updated_at=NOW - 22 * DAY)
    result = sps.sync_plans([plan], [], effective_level="premium", tz_name=self.TZ, now=NOW)
    assert result.visible_plans[0].status == "completed"
    assert result.visible_plans[0].activated_at is None
    assert result.changed

  def test_official_not_completed_before_deadline(self):
    activated = NOW - 20 * DAY
    plan = make_official(status="active", activated_at=activated)
    result = sps.sync_plans([plan], [], effective_level="premium", tz_name=self.TZ, now=NOW)
    assert result.visible_plans[0].status == "active"

  def test_calendar_days_not_fixed_seconds(self):
    """跨夏令时时区：完成时刻按日历日（墙钟不变），不是 activated_at + days×86400。"""
    tz = ZoneInfo("America/New_York")
    # 2026-03-01 10:00 EST 激活，7 天 → 2026-03-08 10:00 EDT（中间 03-08 02:00 拨快 1 小时）
    activated = int(datetime(2026, 3, 1, 10, 0, tzinfo=tz).timestamp())
    plan = make_official("P1", "jet_lag", status="active", activated_at=activated)
    done = sps.completion_ts(plan, tz)
    assert done == int(datetime(2026, 3, 8, 10, 0, tzinfo=tz).timestamp())
    assert done != activated + 7 * DAY  # 固定秒数会晚 1 小时

  def test_one_round_completion(self):
    """repeats_weekly=false：一轮最后执行日的 sleep_time + target_minutes 后 completed。"""
    tz = ZoneInfo(self.TZ)
    # 2026-08-10 周一 23:00 激活（当天 22:30 的 sleep_time 已过，当天仍计入，§6⑤b）
    activated = int(datetime(2026, 8, 10, 23, 0, tzinfo=tz).timestamp())
    plan = make_plan(
      status="active", activated_at=activated,
      cycle=SleepPlanCycle(type="weekdays", weekdays=[1, 2, 3, 4, 5], repeats_weekly=False),
    )
    done = sps.completion_ts(plan, tz)
    # 最后一个执行日 = 周五 08-14，22:30 + 480min = 08-15 06:30
    expected = int(datetime(2026, 8, 15, 6, 30, tzinfo=tz).timestamp())
    assert done == expected

    before = expected - 60
    r1 = sps.sync_plans([plan], [], effective_level="premium", tz_name=self.TZ, now=before)
    assert r1.visible_plans[0].status == "active"
    r2 = sps.sync_plans([plan], [], effective_level="premium", tz_name=self.TZ, now=expected)
    assert r2.visible_plans[0].status == "completed"

  def test_repeats_weekly_true_never_completes(self):
    activated = NOW - 365 * DAY
    plan = make_plan(status="active", activated_at=activated)
    result = sps.sync_plans([plan], [], effective_level="premium", tz_name=self.TZ, now=NOW)
    assert result.visible_plans[0].status == "active"


# ── 字段校验 / rejected reasons ─────────────────────────────────────────────

class TestValidation:
  def _reject(self, **overrides):
    result = sps.sync_plans([], [make_plan(**overrides)], effective_level="premium", tz_name="Asia/Shanghai")
    assert len(result.rejected) == 1
    return result.rejected[0]["reason"]

  def test_invalid_duration(self):
    assert self._reject(target_minutes=30) == "invalid_duration"

  def test_invalid_name(self):
    assert self._reject(custom_name="a" * 29) == "invalid_name"
    assert self._reject(custom_name="中" * 13) == "invalid_name"
    assert self._reject(custom_name="中" * 11 + "abc") == "invalid_name"

  def test_custom_name_chinese_12_english_28(self):
    for name in ("Mom off Duty", "a" * 28, "中" * 12, "中" * 11 + "ab"):
      result = sps.sync_plans([], [make_plan(custom_name=name)], effective_level="premium", tz_name="Asia/Shanghai")
      assert result.rejected == []

  def test_invalid_cycle_not_consecutive(self):
    assert self._reject(cycle=SleepPlanCycle(type="weekdays", weekdays=[1, 3, 5], repeats_weekly=True)) == "invalid_cycle"

  def test_circular_consecutive_ok(self):
    """周六/周日/周一按实际日期视为连续。"""
    plan = make_plan(cycle=SleepPlanCycle(type="weekdays", weekdays=[6, 7, 1], repeats_weekly=True))
    result = sps.sync_plans([], [plan], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected == []

  def test_consecutive_check(self):
    assert sps.has_3_consecutive_weekdays([5, 6, 7])
    assert sps.has_3_consecutive_weekdays([7, 1, 2])
    assert not sps.has_3_consecutive_weekdays([1, 2, 4])
    assert not sps.has_3_consecutive_weekdays([1, 3])

  def test_official_wrong_days_count_rejected(self):
    plan = make_official().model_copy(update={
      "cycle": SleepPlanCycle(type="fixed_daily", days_count=10),
    })
    result = sps.sync_plans([], [plan], effective_level="premium", tz_name="Asia/Shanghai")
    assert result.rejected[0]["reason"] == "invalid_cycle"

  def test_invalid_reminder_minutes(self):
    assert self._reject(reminder_minutes_before=45) == "invalid_plan"

  def test_invalid_sound_and_category(self):
    assert self._reject(alarm_sound_id="unknown") == "invalid_plan"
    assert self._reject(sleep_aid_category="random") == "invalid_plan"

  def test_rejected_does_not_affect_siblings(self):
    """单条被拒不影响同批次其他计划。"""
    good = make_plan("PLAN-GOOD", updated_at=NOW)
    bad = make_plan("PLAN-BAD", target_minutes=10, updated_at=NOW)
    result = sps.sync_plans([], [good, bad], effective_level="premium", tz_name="Asia/Shanghai")
    assert [p.plan_id for p in result.visible_plans] == ["PLAN-GOOD"]
    assert result.rejected == [{"plan_id": "PLAN-BAD", "reason": "invalid_duration"}]


# ── /sleep_plan 端点 ─────────────────────────────────────────────────────────

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import user_server as us
from profile_service import UserProfileServ


def _build_sleep_plan_app(profile=None, tier="pro"):
  """最小 aiohttp app：真实 handle_sleep_plan_http + 假存储/假等级。"""
  saved = {}

  class FakeUserServ:
    def get_profile(self, uid):
      return profile

    def save_profile(self, uid, p):
      saved[uid] = p

    _note_request_meta = staticmethod(UserProfileServ._note_request_meta)

  class FakeSrv:
    def __init__(self):
      self.user_serv = FakeUserServ()
      self.debug_uid_set = {"mindora_test_uid1"}
      self._tier = tier
      self._tier_cache = {}

    def _parse_for_uid(self, data):
      if data.jwt_token:
        return "jwt_uid"
      if data.uid in self.debug_uid_set:
        return data.uid
      return None

    async def _get_effective_level(self, uid, jwt_token):
      if self._tier is None:
        return await us.UserServer._get_effective_level(self, uid, jwt_token)
      return self._tier

  srv = FakeSrv()
  srv.handle_sleep_plan_http = types.MethodType(us.UserServer.handle_sleep_plan_http, srv)
  app = web.Application()
  app.router.add_post("/sleep_plan", srv.handle_sleep_plan_http)
  app.saved = saved
  return app


def _sync_body(request_type="sync_plans", plans=None, **data_overrides):
  data = {"uid": "mindora_test_uid1", "language": "zh-Hans",
          "timezone": "Asia/Shanghai", "device_id": "dev-1"}
  data.update(data_overrides)
  if plans is not None:
    data["plans"] = plans
  return {"request_type": request_type, "version": "1.0",
          "timestamp": int(time.time()), "data": data}


class TestSleepPlanEndpoint:
  @pytest.mark.asyncio
  async def test_sync_new_plan_persisted(self):
    app = _build_sleep_plan_app(profile=UserProfile(), tier="pro")
    plan = make_plan(updated_at=NOW).model_dump(mode="json")
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=_sync_body(plans=[plan]))
      assert resp.status == 200
      data = (await resp.json())["data"]
      assert data["membership_tier"] == "basic"
      assert data["quota"] == {"used": 1, "limit": 1}
      assert len(data["plans"]) == 1
      assert data["plans"][0]["plan_id"] == "PLAN-0001"
      assert data["rejected"] == []
      assert data["server_time"] > 0
    # 已落库 + 环境信息已记录
    saved = app.saved["mindora_test_uid1"]
    assert saved.sleep_plans[0].plan_id == "PLAN-0001"
    assert saved.last_request_timezone == "Asia/Shanghai"
    assert saved.last_request_language == "zh-Hans"

  @pytest.mark.asyncio
  async def test_free_tier_rejected(self):
    app = _build_sleep_plan_app(profile=UserProfile(), tier="free")
    plan = make_plan(updated_at=NOW).model_dump(mode="json")
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=_sync_body(plans=[plan]))
      data = (await resp.json())["data"]
      assert data["quota"]["limit"] == 0
      assert data["rejected"] == [{"plan_id": "PLAN-0001", "reason": "tier_not_allowed"}]
      assert data["plans"] == []

  @pytest.mark.asyncio
  async def test_query_plans_returns_stored(self):
    profile = UserProfile(sleep_plans=[make_plan(updated_at=NOW - 10)])
    app = _build_sleep_plan_app(profile=profile, tier="premium")
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=_sync_body(request_type="query_plans"))
      data = (await resp.json())["data"]
      assert data["membership_tier"] == "premium"
      assert data["quota"] == {"used": 1, "limit": 5}
      assert len(data["plans"]) == 1
      assert data["rejected"] == []

  @pytest.mark.asyncio
  async def test_query_plans_empty_for_new_user(self):
    app = _build_sleep_plan_app(profile=None, tier="free")
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=_sync_body(request_type="query_plans"))
      data = (await resp.json())["data"]
      assert data["plans"] == []
      assert data["quota"]["used"] == 0

  @pytest.mark.asyncio
  async def test_unauthorized(self):
    app = _build_sleep_plan_app(profile=UserProfile())
    body = _sync_body(request_type="query_plans")
    body["data"] = {"uid": "someone_else"}
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=body)
      assert resp.status == 401

  @pytest.mark.asyncio
  async def test_tombstone_sync_removes_plan(self):
    profile = UserProfile(sleep_plans=[make_plan(updated_at=NOW - 100)])
    app = _build_sleep_plan_app(profile=profile, tier="pro")
    ts = tombstone(ts=NOW).model_dump(mode="json")
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/sleep_plan", json=_sync_body(plans=[ts]))
      data = (await resp.json())["data"]
      assert data["plans"] == []
      assert data["quota"]["used"] == 0
    # 墓碑保留在存储里（多端同步需要）
    assert app.saved["mindora_test_uid1"].sleep_plans[0].deleted is True
