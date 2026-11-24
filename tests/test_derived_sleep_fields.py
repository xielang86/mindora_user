"""从 sleep_data / 健康数据反推此前恒为 null 的字段：

  ① goal_achieved —— 账号级 sleep_plans 的 active 计划目标 vs 当晚 TST
  ② avg_heart_rate —— v2 只传 min/max，用区间中点补（仅服务端合成行）
  ③ time_in_bed —— 按健康数据同步接口_0814.md §10 优先用真实 inBed 样本
"""
import threading
import time

import pytest

from user_profile import (
  SleepElement, SleepResult, SleepPlanCycle, SyncedSleepPlan, UserProfile,
  active_sleep_plan, compute_recent_sleep_stats,
)
from profile_service import UserProfileServ
from sleep_session_builder import SOURCE_HEALTHKIT


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  return serv


def _night(ts: int, *, source=SOURCE_HEALTHKIT, in_bed=None, goal=None) -> SleepResult:
  """一晚：core 300 + awake 20 + deep 60 + rem 40 = 跨度 420，TST 400。"""
  return SleepResult(
    timestamp=ts,
    source=source,
    goal_achieved=goal,
    in_bed_intervals=in_bed or [],
    sleep_status=[
      SleepElement(start_time=ts - 25200, duration=300.0, sleep_type="core"),
      SleepElement(start_time=ts - 7200, duration=20.0, sleep_type="awake"),
      SleepElement(start_time=ts - 6000, duration=60.0, sleep_type="deep"),
      SleepElement(start_time=ts - 2400, duration=40.0, sleep_type="rem"),
    ],
  )


def _plan(target: int, activated_at: int, status: str = "active") -> SyncedSleepPlan:
  return SyncedSleepPlan(
    plan_id="P1", kind="custom", target_minutes=target, status=status,
    activated_at=activated_at if status == "active" else None,
    updated_at=1, created_at=1,
    cycle=SleepPlanCycle(type="weekdays", weekdays=[1, 2, 3], repeats_weekly=True),
  )


class TestSequenceSummaryDurations:
  def test_three_durations_are_distinct(self):
    """跨度含觉醒、TST 不含；没有 inBed 时 time_in_bed 退回跨度并标明来源。"""
    summ = _night(10_000).sequence_summaries
    assert summ["sleep_span"] == 420.0
    assert summ["total_sleep_duration"] == 400.0
    assert summ["time_in_bed"] == 420.0
    assert summ["time_in_bed_source"] == "stage_span"

  def test_real_in_bed_wins(self):
    """有 inBed 样本时按规范取真实卧床（含睡前清醒，比阶段跨度长）。"""
    summ = _night(10_000, in_bed=[(0, 480 * 60)]).sequence_summaries
    assert summ["time_in_bed"] == 480.0
    assert summ["time_in_bed_source"] == "in_bed"
    # TST 与跨度不受影响
    assert summ["total_sleep_duration"] == 400.0
    assert summ["sleep_span"] == 420.0

  def test_inconsistent_in_bed_falls_back(self):
    """卧床短于实测睡眠 = 数据自相矛盾（半截值），退回阶段跨度。"""
    summ = _night(10_000, in_bed=[(0, 100 * 60)]).sequence_summaries
    assert summ["time_in_bed"] == 420.0
    assert summ["time_in_bed_source"] == "stage_span"

  def test_stage_percentages_use_span_not_bed(self):
    """阶段占比分母是跨度：有没有 inBed 数据，占比都该一样。"""
    profile = UserProfile(sleep_data=[_night(10_000)])
    with_bed = UserProfile(sleep_data=[_night(10_000, in_bed=[(0, 480 * 60)])])
    assert (compute_recent_sleep_stats(profile)["avg_deep_pct"]
            == compute_recent_sleep_stats(with_bed)["avg_deep_pct"]
            == round(60 / 420 * 100, 1))
    # 卧床均值本身仍按各自口径走
    assert compute_recent_sleep_stats(with_bed)["avg_time_in_bed_min"] == 480.0
    assert compute_recent_sleep_stats(profile)["avg_total_sleep_min"] == 400.0


class TestGoalAchieved:
  def test_filled_from_active_plan(self):
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 3600)],
      sleep_plans=[_plan(target=500, activated_at=now - 86400)],
    )
    _serv()._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved == 80.0   # TST 400 / 目标 500

  def test_capped_at_100(self):
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 3600)],
      sleep_plans=[_plan(target=300, activated_at=now - 86400)],
    )
    _serv()._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved == 100.0

  def test_nights_before_activation_untouched(self):
    """计划昨天才启用，不该给上个月的夜晚打分。"""
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 30 * 86400), _night(now - 3600)],
      sleep_plans=[_plan(target=500, activated_at=now - 86400)],
    )
    _serv()._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved is None
    assert profile.sleep_data[1].goal_achieved == 80.0

  def test_no_active_plan_is_noop(self):
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 3600)],
      sleep_plans=[_plan(target=500, activated_at=now - 86400, status="not_enabled")],
    )
    assert active_sleep_plan(profile) is None
    _serv()._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved is None

  def test_device_reported_value_preserved(self):
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 3600, source=None, goal=42.0)],
      sleep_plans=[_plan(target=500, activated_at=now - 86400)],
    )
    _serv()._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved == 42.0

  def test_recompute_is_idempotent_and_follows_target_change(self):
    now = int(time.time())
    profile = UserProfile(
      sleep_data=[_night(now - 3600)],
      sleep_plans=[_plan(target=500, activated_at=now - 86400)],
    )
    serv = _serv()
    serv._update_goal_achieved(profile)
    serv._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved == 80.0
    profile.sleep_plans[0].target_minutes = 800
    serv._update_goal_achieved(profile)
    assert profile.sleep_data[0].goal_achieved == 50.0


class TestAvgHeartRate:
  def _profile_with_hr(self, source=SOURCE_HEALTHKIT) -> UserProfile:
    night = _night(10_000, source=source)
    start = min(e.start_time for e in night.sleep_status)
    return UserProfile(
      sleep_data=[night],
      behaviors={
        "sleep_heart_rate_min": [(start, 49.0)],
        "sleep_heart_rate_max": [(start, 72.0)],
      },
    )

  def test_midpoint_filled_for_synthesized_rows(self):
    profile = self._profile_with_hr()
    _serv()._update_night_hr_range(profile)
    record = profile.sleep_data[0]
    assert (record.hr_min, record.hr_max) == (49.0, 72.0)
    assert record.avg_heart_rate == 60.5

  def test_device_rows_not_overwritten(self):
    profile = self._profile_with_hr(source=None)
    profile.sleep_data[0].avg_heart_rate = 58.0
    _serv()._update_night_hr_range(profile)
    assert profile.sleep_data[0].avg_heart_rate == 58.0

  def test_reaches_weekly_stats(self):
    """补上之后 compute_recent_sleep_stats → LLM 才拿得到心率。"""
    profile = self._profile_with_hr()
    _serv()._update_night_hr_range(profile)
    assert compute_recent_sleep_stats(profile)["avg_heart_rate"] == 60.5
