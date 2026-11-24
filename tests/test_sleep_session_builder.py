"""tests/test_sleep_session_builder.py — 健康 behaviors → SleepResult 按晚合成。

覆盖：
  1. 会话聚类（跨晚切分 / 碎片过滤 / 午睡过滤）
  2. 阶段映射与单位换算（秒→分钟，light/unspecified→core）
  3. onset 口径（md §6.1：inBed 包络弃用、首段即睡统一估计、碎醒不打断、上限 240）
  4. 派生得分与窗口体征
  5. profile_service 合并策略：幂等 / 同晚修正覆盖 / 设备行优先 / 触发门打通
"""
from __future__ import annotations

import datetime
import threading
import time
from datetime import timezone

import pytest

from sleep_session_builder import (
  _score_sleep_onset_efficiency, derive_sessions, derive_sleep_results, build_sleep_result, resolve_sleep_onset_efficiency,
  SESSION_GAP_SECONDS, SOURCE_HEALTHKIT,
)
from user_profile import SleepElement, SleepResult, UserProfile
from user_server import UserProfileServ
from llm import SleepAnalysisLLM

UTC = timezone.utc
# 2026-08-17 22:50:00 UTC 作基准
BASE = int(datetime.datetime(2026, 8, 17, 22, 50, tzinfo=UTC).timestamp())


def _ts(minutes_after_base: float) -> int:
  return int(BASE + minutes_after_base * 60)


def _night_behaviors(
  in_bed_start_min=0.0,            # inBed 起点（相对 BASE 分钟）；None 表示无 inBed
  stages=((10, 10, "awake"), (20, 60, "light"), (80, 60, "deep"),
          (140, 60, "rem"), (200, 280, "light")),  # (起始分钟, 时长分钟, 轨)
  day_offset=0,
) -> dict:
  """构造一晚的 behaviors；stages 元素为 (起始分钟, 时长分钟, 阶段轨名后缀)。"""
  beh: dict = {}
  offset = day_offset * 24 * 60
  if in_bed_start_min is not None:
    last_end = max(s + d for s, d, _t in stages)
    beh["sleep_in_bed"] = [[_ts(in_bed_start_min + offset), (last_end - in_bed_start_min) * 60]]
  for start_min, dur_min, track in stages:
    beh.setdefault(f"sleep_stage_{track}", []).append([_ts(start_min + offset), dur_min * 60])
  return beh


def _merge(a: dict, b: dict) -> dict:
  out = {k: list(v) for k, v in a.items()}
  for k, v in b.items():
    out.setdefault(k, []).extend(v)
  return out


# ── 1. 会话聚类 ──────────────────────────────────────────────────────────────

class TestSessionClustering:
  def test_two_nights_split_by_gap(self):
    beh = _merge(_night_behaviors(day_offset=0), _night_behaviors(day_offset=1))
    sessions = derive_sessions(beh)
    assert len(sessions) == 2
    assert sessions[1].start - sessions[0].end > SESSION_GAP_SECONDS

  def test_fragment_below_min_asleep_dropped(self):
    beh = {"sleep_stage_light": [[_ts(0), 5 * 60]]}  # 5 分钟碎片
    assert derive_sessions(beh) == []

  def test_nap_filtered_keeping_longest_of_wake_day(self):
    # 夜晚 00:11-07:59（长）+ 当天下午 17:44-18:21 小睡（同一天醒来日）→ 只留夜晚
    night = _night_behaviors(day_offset=0)
    nap = {"sleep_stage_light": [[_ts(17 * 60 + 44), 37 * 60]]}  # 当天傍晚（BASE 是 22:50，这是次日）
    rows = derive_sleep_results(_merge(night, nap), UTC)
    assert len(rows) == 1
    assert rows[0][2].sleep_status[0].start_time == _ts(10)  # 夜晚那条

  def test_interval_over_16h_discarded(self):
    beh = {
      "sleep_stage_light": [[_ts(0), 20 * 3600]],   # 坏样本：20 小时
      "sleep_stage_deep": [[_ts(30), 2 * 3600]],    # 正常一段但总长不足 30 分钟睡眠下限？
    }
    # 20h 区间被丢弃后只剩 2h deep → 正常成会话
    sessions = derive_sessions(beh)
    assert len(sessions) == 1
    assert all(t == "deep" for _s, _e, t in sessions[0].intervals)


# ── 2. 阶段映射与单位 ────────────────────────────────────────────────────────

class TestStageMapping:
  def test_light_and_unspecified_map_to_core(self):
    beh = _night_behaviors(stages=[(0, 100, "light"), (100, 100, "unspecified"), (200, 100, "deep")])
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    types = [e.sleep_type for e in row.sleep_status]
    assert types == ["core", "core", "deep"]

  def test_duration_converted_seconds_to_minutes(self):
    beh = _night_behaviors(stages=[(0, 90, "deep"), (90, 240, "light")])
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.sleep_status[0].duration == pytest.approx(90.0)  # 90 分钟，不是 5400

  def test_wake_timestamp_is_session_end(self):
    beh = _night_behaviors()  # 最后一段 200+280=480 分钟
    _s, e, row = derive_sleep_results(beh, UTC)[0]
    assert row.timestamp == e == _ts(480)


# ── 3. onset 口径（md §6.1）──────────────────────────────────────────────────

class TestOnset:
  def test_onset_from_in_bed_start(self):
    # inBed 22:50，首段 awake 23:00-23:10，core 23:10 起 → 23:15 睡够 5 分钟
    beh = _night_behaviors()
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.onset == pytest.approx(25.0)         # 23:15 - 22:50
    assert row.first_sleep_time == "23:15"
    assert row.bed_time == "22:50"
    assert row.soe is None  # score bundle is filled after profile merge

  def test_in_bed_envelope_discarded(self):
    """inBed 起点 == 会话起点（包络写法）→ 弃用；首段即睡 → onset 估计 10 分钟。"""
    beh = _night_behaviors(
      in_bed_start_min=10,  # 与第一个阶段区间同起点
      stages=((10, 60, "light"), (70, 60, "deep"), (130, 200, "rem")),
    )
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.onset == 10
    assert row.first_sleep_time == "23:00"
    assert row.soe is None
    assert row.bed_time == "22:50"

  def test_micro_awake_does_not_interrupt(self):
    """≤1 分钟碎醒不算打断：4 分钟 core + 30 秒 awake + core → 累计 5 分钟时点入睡。"""
    beh = _night_behaviors(stages=[
      (10, 10, "awake"),
      (20, 4, "light"),           # 4 分钟
      (24, 0.5, "awake"),         # 30 秒碎醒
      (24.5, 300, "light"),       # 累计在此段内达到 5 分钟：24.5 分 + 1 分钟
    ])
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    # 睡够 300s 的时刻 = 23:00+24.5min + 60s = BASE+25.5min；lightsOut=BASE+0
    assert row.onset == pytest.approx(25.5)

  def test_long_awake_resets_accumulation(self):
    beh = _night_behaviors(stages=[
      (10, 10, "awake"),
      (20, 4, "light"),
      (24, 30, "awake"),          # 30 分钟长醒 → 重新累计
      (54, 300, "light"),         # 54+5=59 睡够 5 分钟
    ])
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.onset == pytest.approx(59.0)

  def test_onset_capped_at_240(self):
    beh = _night_behaviors(in_bed_start_min=0, stages=[(10, 240, "awake"), (250, 300, "light")])
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.onset == 240
    assert row.soe is None

  @pytest.mark.parametrize("stage", ["light", "deep", "rem"])
  def test_first_sleep_stage_uses_estimated_onset(self, stage):
    beh = _night_behaviors(
      in_bed_start_min=None,
      stages=[(10, 60, stage), (70, 300, "light")],
    )
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.onset == 10.0
    assert row.soe is None
    assert row.bed_time == "22:50"

  def test_soe_piecewise_breakpoints(self):
    assert _score_sleep_onset_efficiency(7) == 100.0
    assert _score_sleep_onset_efficiency(40) == 60.0
    assert _score_sleep_onset_efficiency(120) == 10.0
    assert _score_sleep_onset_efficiency(240) == 1.0

  def test_legacy_deep_first_record_resolves_soe_without_resynthesis(self):
    row = SleepResult(
      timestamp=_ts(100), sleep_quality=81.3, soe=None,
      sleep_status=[SleepElement(start_time=_ts(10), duration=60, sleep_type="deep")],
    )
    assert resolve_sleep_onset_efficiency(row) == pytest.approx(96.4)


# ── 4. 派生得分与体征 ────────────────────────────────────────────────────────

class TestDerivedScores:
  def test_quality_structure_fluctuation_present(self):
    beh = _night_behaviors()
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    from sleep_score_cache import refresh_score_cache
    refresh_score_cache(UserProfile(sleep_data=[row]))
    assert 0 <= row.sleep_quality <= 100
    assert row.sleep_arch_index is not None and 0 <= row.sleep_arch_index <= 100
    assert row.night_var_index is not None and 0 <= row.night_var_index <= 100

  def test_full_sleep_scores_higher_than_short(self):
    good = _night_behaviors()  # 470 分钟睡眠
    bad = _night_behaviors(stages=[(10, 10, "awake"), (20, 60, "light"),
                                   (80, 30, "awake"), (110, 40, "light")])
    from sleep_score_cache import refresh_score_cache
    good_row = derive_sleep_results(good, UTC)[0][2]
    bad_row = derive_sleep_results(bad, UTC)[0][2]
    refresh_score_cache(UserProfile(sleep_data=[good_row]))
    refresh_score_cache(UserProfile(sleep_data=[bad_row]))
    good_q, bad_q = good_row.sleep_quality, bad_row.sleep_quality
    assert good_q > bad_q

  def test_window_vitals(self):
    beh = _night_behaviors()
    beh["sleep_heart_rate_variability_sdnn"] = [[_ts(30), 40.0], [_ts(60), 50.0], [_ts(9999), 99.0]]
    beh["sleep_respiratory_rate"] = [[_ts(30), 14.0], [_ts(60), 16.0]]
    beh["sleeping_wrist_temperature"] = [[_ts(45), 34.5]]
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.hrv == pytest.approx(45.0)           # 窗口外的 99 不参与
    assert row.avg_respiratory == pytest.approx(15.0)
    assert row.respiratory_var == pytest.approx((16 - 14) / 15 * 100, abs=0.1)
    assert row.avg_temperature == pytest.approx(34.5)

  def test_awake_count_feeds_sequence_summaries(self):
    beh = _night_behaviors()  # 1 段 awake
    _s, _e, row = derive_sleep_results(beh, UTC)[0]
    assert row.sequence_summaries["night_awake_count"] == 1
    assert row.sequence_summaries["deep_sleep_duration"] == pytest.approx(60.0)


# ── 5. profile_service 合并策略 ─────────────────────────────────────────────

def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None
  return serv


def _apply(serv, uid, behaviors, existing=None, tz="UTC"):
  new_profile = UserProfile(uid_emb=[], behaviors=behaviors)
  return serv._apply_basic_update(uid, new_profile, existing, timezone=tz)


class TestSynthesizeMerge:
  def test_behaviors_produce_sleep_data(self):
    serv = _serv()
    profile = _apply(serv, "u1", _night_behaviors())
    assert len(profile.sleep_data) == 1
    row = profile.sleep_data[0]
    assert row.source == SOURCE_HEALTHKIT
    assert row.sleep_quality is not None

  def test_idempotent_second_update_no_duplicate(self):
    serv = _serv()
    profile = _apply(serv, "u1", _night_behaviors())
    # 第二次 update 带同样的 behaviors（客户端水位线重发场景）
    profile = _apply(serv, "u1", _night_behaviors(), existing=profile)
    assert len(profile.sleep_data) == 1

  def test_same_night_correction_replaces_row(self):
    """§4.2：同晚重发修正值（最后一段时长变长）→ 合成行被重算覆盖，不留重复。"""
    serv = _serv()
    profile = _apply(serv, "u1", _night_behaviors())
    old_wake = profile.sleep_data[0].timestamp
    # 修正：最后一段 light 从 280 分钟延长到 310 分钟（同起点）
    corrected = _night_behaviors(stages=((10, 10, "awake"), (20, 60, "light"), (80, 60, "deep"),
                                         (140, 60, "rem"), (200, 310, "light")))
    profile = _apply(serv, "u1", corrected, existing=profile)
    assert len(profile.sleep_data) == 1
    assert profile.sleep_data[0].timestamp > old_wake

  def test_device_row_wins_over_synthesis(self):
    """会话窗口内已有设备上报行（source=None）→ 该晚不合成。"""
    serv = _serv()
    existing = UserProfile(uid_emb=[])
    existing.sleep_data = [SleepResult(
      timestamp=_ts(300), sleep_quality=91.0, sleep_status=[],
    )]
    profile = _apply(serv, "u1", _night_behaviors(), existing=existing)
    assert len(profile.sleep_data) == 1
    assert profile.sleep_data[0].source is None
    # A score-only device row survives, but an old unversioned score cannot be
    # passed through as the new AN_SCORE without stage data.
    assert profile.sleep_data[0].sleep_quality is None

  def test_healthkit_row_kept_when_session_ages_out_of_behaviors(self):
    """behaviors 截断后老会话消失：其合成行保留（sleep_data 30 晚保留期的意义）。"""
    serv = _serv()
    profile = _apply(serv, "u1", _night_behaviors(day_offset=0))
    assert len(profile.sleep_data) == 1
    # 下一次 update 只有新一晚的 behaviors（老晚已被 MAX_BEHAVIOR 截掉是存储层的事，
    # 这里模拟"合并后 behaviors 里老晚还在"与"不在"两种情形中的后者：
    # 直接构造只有新晚 behaviors 的画像）
    profile.behaviors = _night_behaviors(day_offset=1)
    profile = _apply(serv, "u1", _night_behaviors(day_offset=1), existing=profile)
    assert len(profile.sleep_data) == 2  # 老合成行保留 + 新晚合成

  def test_hr_range_paired_into_synthesized_row(self):
    """v2 心率区间按会话起点配对进合成行（_update_night_hr_range 链路）。"""
    serv = _serv()
    beh = _night_behaviors()
    beh["sleep_heart_rate_min"] = [[_ts(10), 55.0]]   # 时间戳 = 会话起点（第一段 23:00）
    beh["sleep_heart_rate_max"] = [[_ts(10), 82.0]]
    profile = _apply(serv, "u1", beh)
    row = profile.sleep_data[0]
    assert row.hr_min == 55.0
    assert row.hr_max == 82.0

  def test_analysis_needed_after_synthesis(self):
    """合成后触发门打通：有新晚且当日无报告 → needed=True（兜底替换链起点）。"""
    import user_server as us
    serv = _serv()
    profile = _apply(serv, "u1", _night_behaviors())
    server = us.UserServer.__new__(us.UserServer)
    needed, ts = server._analysis_needed(profile)
    assert needed is True and ts == profile.sleep_data[0].timestamp


class TestStageBalanceScore:
  def test_balance_excess_and_missing_stages(self):
    from sleep_session_builder import score_sleep_structure
    assert score_sleep_structure(20, 25, 55) == 100
    assert score_sleep_structure(41, 55, 0) == 45
    assert score_sleep_structure(100, 0, 0) == 20
    assert score_sleep_structure(0, 100, 0) == 25
    assert score_sleep_structure(0, 0, 100) == 55
    assert score_sleep_structure(0, 0, 0) is None
    assert score_sleep_structure(40, 50, 110) == 100
    assert score_sleep_structure(30, 25, 45) < score_sleep_structure(20, 25, 55)

  def test_synthesis_and_legacy_read_share_score(self):
    from sleep_session_builder import resolve_sleep_structure_score
    beh = _night_behaviors(stages=((0, 60, "deep"), (60, 90, "rem")))
    row = build_sleep_result(derive_sessions(beh)[0], beh, UTC)
    assert row.sleep_arch_index is None
    row.sleep_arch_index = 100
    assert resolve_sleep_structure_score(row) == 45
    assert row.sleep_arch_index == 100
    row.source = None
    assert resolve_sleep_structure_score(row) == 100
    row.source = "healthkit"
    row.sleep_status = []
    assert resolve_sleep_structure_score(row) == 100


class TestSleepDurationCeiling:
  @pytest.mark.parametrize("minutes,ceiling", [(0,0),(240,47.2),(299.9,59),(300,59),(360,79),(420,100),(600,100)])
  def test_boundaries(self, minutes, ceiling):
    from sleep_session_builder import cap_sleep_quality
    assert cap_sleep_quality(100, minutes) <= ceiling
    assert cap_sleep_quality(20, minutes) <= 20

  def test_unknown_duration_and_missing_score(self):
    from sleep_session_builder import cap_sleep_quality, resolve_sleep_quality
    assert cap_sleep_quality(None,240) is None
    assert resolve_sleep_quality(SleepResult(timestamp=BASE,sleep_quality=85)) == 85

  def test_aggregate_caps_short_mean_even_with_some_long_nights(self):
    from sleep_session_builder import aggregate_sleep_quality, resolve_sleep_quality
    rows=[SleepResult(timestamp=BASE, sleep_quality=100, sleep_status=[
        SleepElement(start_time=BASE,duration=d,sleep_type="core")]) for d in (119,480)]
    assert sum(resolve_sleep_quality(r) for r in rows)/2 > 60
    assert aggregate_sleep_quality(rows) < 59

  def test_new_healthkit_short_night_is_capped(self):
    beh=_night_behaviors(stages=((0,48,"deep"),(48,60,"rem"),(108,132,"light")))
    row=build_sleep_result(derive_sessions(beh)[0],beh,UTC)
    from sleep_score_cache import refresh_score_cache
    refresh_score_cache(UserProfile(sleep_data=[row]))
    assert row.sleep_arch_index is not None
    assert row.sleep_quality <= 47.2
