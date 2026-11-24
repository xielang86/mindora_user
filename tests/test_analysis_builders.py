"""analysis_builders 对齐《服务端分析接口.md》的回归测试。

口径：
  - 数值必须来自真实睡眠数据，算不出就省略模块/字段（不允许 Sedona、82 等假值）
  - 文案字段（title/description/label）骨架默认空串，由 LLM 库存报告 deep_merge 覆盖
"""
from __future__ import annotations

import datetime
import time
import pytest
from types import SimpleNamespace

from analysis_builders import (
  build_explore,
  build_overview,
  build_sleep_day,
  build_sleep_month,
  build_sleep_week,
  qualify_day_level_text,
)
from user_profile import NightEvent, SleepElement, SleepResult, UserProfile

TODAY = datetime.date.today()


def _d(**kw):
  kw.setdefault("modules", [])
  kw.setdefault("language", "en")
  return SimpleNamespace(**kw)


def _night(ts: int, quality: float, **kw) -> SleepResult:
  kw.setdefault("sleep_status", [])
  return SleepResult(timestamp=ts, sleep_quality=quality, **kw)


def _ts(days_ago: int) -> int:
  """days_ago 天前中午 12 点的时间戳（避开时区零点边界）。"""
  d = TODAY - datetime.timedelta(days=days_ago)
  return int(datetime.datetime(d.year, d.month, d.day, 12).timestamp())


# ── build_sleep_day ──────────────────────────────────────────────

def test_sleep_day_uses_sleep_scenarios_key_without_raw_soe_score():
  p = UserProfile(uid="u", sleep_data=[_night(_ts(0), 88, soe=76)])
  out = build_sleep_day(_d(date=TODAY.isoformat()), p)
  assert "sleep_scenarios" in out
  assert "sleep_scenarios_reco" not in out
  assert "score_summary" not in out
  assert out["sleep_scenarios"]["description"]  # 规则模板直接提供兜底文案
  assert "No sleep-stage data" in out["stage_insights"]["deep"]["description"]


def test_sleep_day_omits_score_without_data():
  out = build_sleep_day(_d(date=TODAY.isoformat()), UserProfile(uid="u"))
  assert "score_summary" not in out
  assert out["stage_insights"]["deep"]["description"] == ""


def test_sleep_day_stale_night_anchors_last_night():
  """有效时间锚定：最新一夜 10 天前（停戴超期）不再降级——评分照常返回并锚定该夜；
  场景标题口径（compute_recent_sleep_stats）不在本次范围，保持原行为。"""
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(10), 88, soe=76)],
    mindora_record={"sleep.scene.rain_wash": [[now - 9 * 86400, 60]]},
  )
  out = build_sleep_day(_d(date=None), p)  # 不带 date → 默认锚定最新有效夜
  assert "score_summary" not in out
  assert out["sleep_metrics"]["date"] == (TODAY - datetime.timedelta(days=10)).isoformat()
  # 播放发生在该夜之后，不能归因给这一夜。
  assert out["sleep_scenarios"]["title"] == ""
  assert out["sleep_scenarios"]["description"]


def test_sleep_day_night_six_days_ago_still_fresh():
  """7 天窗口边界：6 天前的一夜仍新鲜，评分与场景标题照常返回。"""
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(6), 88, soe=76)],
    mindora_record={"sleep.scene.rain_wash": [[now - 5 * 86400, 60]]},
  )
  out = build_sleep_day(_d(date=TODAY.isoformat()), p)
  assert "score_summary" not in out
  assert out["sleep_scenarios"]["title"] == ""


def test_stale_day_insight_has_rule_text_without_recent_sleep_or_device_use():
  """Older nights still have dated, readable day insights when no report matches."""
  ts = _ts(3)
  night = _night(ts, 76, sleep_status=[
    SleepElement(start_time=ts - 8 * 3600, duration=210, sleep_type="core"),
    SleepElement(start_time=ts - 4 * 3600, duration=60, sleep_type="deep"),
    SleepElement(start_time=ts - 3 * 3600, duration=50, sleep_type="rem"),
  ])
  profile = UserProfile(uid="u", sleep_data=[night], mindora_record={})
  request = _d(date=TODAY.isoformat(), language="zh-Hans", timezone="Asia/Shanghai")
  overview = build_overview(request, profile)
  day = build_sleep_day(request, profile)
  explore = build_explore(request, profile)
  last_date = datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
  assert overview["sleep_insight"]["date"] == last_date
  assert overview["sleep_insight"]["description"]
  assert "昨晚" not in overview["sleep_insight"]["description"]
  assert last_date in overview["sleep_insight"]["description"]
  assert day["sleep_scenarios"]["title"] == ""
  assert day["sleep_scenarios"]["description"]
  assert all(day["stage_insights"][stage]["description"] for stage in ("awake", "rem", "core", "deep"))
  assert all("昨晚" not in day["stage_insights"][stage]["description"] for stage in ("awake", "rem", "core", "deep"))
  assert explore["data_ready"] is True
  assert explore["onset_efficiency"]["description"]


def test_day_wording_uses_actual_record_age_and_rewrites_stored_prose():
  ts = 1_790_000_000
  profile = UserProfile(sleep_data=[SleepResult(timestamp=ts, sleep_quality=80)])
  local = datetime.timezone(datetime.timedelta(hours=8))
  recent = {"sleep_insight": {"description": "昨晚睡眠较平稳。"}}
  qualify_day_level_text(recent, "analysis_overview", profile, local, "zh-Hans", ts+12*3600)
  assert recent["sleep_insight"]["description"] == "昨晚睡眠较平稳。"

  old = {"sleep_insight": {"description": "昨晚睡眠较平稳。"}}
  qualify_day_level_text(old, "analysis_overview", profile, local, "zh-Hans", ts+25*3600)
  expected_date = datetime.datetime.fromtimestamp(ts, local).date().isoformat()
  assert old["sleep_insight"]["description"] == f"最近一次睡眠记录（{expected_date}）：该夜睡眠较平稳。"
  qualify_day_level_text(old, "analysis_overview", profile, local, "zh-Hans", ts+25*3600)
  assert old["sleep_insight"]["description"].count(expected_date) == 1

  english = {"stage_insights": {"rem": {"description": "Last night's REM sleep was 80 min."}}}
  qualify_day_level_text(english, "analysis_sleep_day", profile, local, "en", ts+25*3600)
  assert english["stage_insights"]["rem"]["description"] == (
    f"Latest recorded sleep ({expected_date}): that night's REM sleep was 80 min.")


# ── build_overview ───────────────────────────────────────────────

def test_overview_weekly_best_from_real_scene_stats():
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(i), 80 + i) for i in range(7)],
    mindora_record={"sleep.scene.rain_wash": [[now - i * 86400, 60] for i in range(5)]},
    sleep_analysis={
      "best_sleep_quality_scene_7d": {"scene_id": "rain_wash", "scene_name": "Rain Wash",
                                      "avg_sleep_quality": 91.4, "nights": 3,
                                      "updated_at": now},
    },
  )
  out = build_overview(_d(date=TODAY.isoformat()), p)
  wb = out["weekly_best"]
  assert wb["audio_name"] == "Rain Wash"
  assert wb["used_times"] == 5
  assert wb["score"] == 100
  assert wb["score_type"] == "usage_match"
  assert out["overall_score"]["score"] == 80


def test_overview_omits_modules_without_data():
  out = build_overview(_d(date=TODAY.isoformat()), UserProfile(uid="u"))
  assert "overall_score" not in out
  assert "weekly_best" not in out  # 不再返回 Sedona 假值
  assert out["sleep_insight"]["title"] == ""


def test_overview_stale_scene_snapshot_not_used():
  """新鲜度门：持久化的 most_used_scene（全时段快照）窗内无记录时不回退——
  停戴超期用户的 weekly_best 整卡省略，不把过期场景漏进响应。"""
  p = UserProfile(
    uid="u",
    sleep_analysis={"most_used_scene": {"scene_id": "ocean", "scene_name": "Ocean", "count": 12}},
  )
  out = build_overview(_d(date=TODAY.isoformat()), p)
  assert "weekly_best" not in out


def test_overview_stale_best_scene_score_gated():
  """best_sleep_quality_scene_7d 快照超 7 天未刷新 → 效果分不返回。"""
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(0), 85)],
    mindora_record={"sleep.scene.rain_wash": [[now, 60]]},
    sleep_analysis={
      "best_sleep_quality_scene_7d": {"scene_id": "rain_wash", "scene_name": "Rain Wash",
                                      "avg_sleep_quality": 91.4, "nights": 3,
                                      "updated_at": now - 8 * 86400},
    },
  )
  out = build_overview(_d(date=TODAY.isoformat()), p)
  assert out["weekly_best"]["score_type"] == "usage_match"


def test_weekly_best_excludes_play_after_anchor_and_has_match_score():
  anchor = _ts(0)
  p = UserProfile(uid="u", sleep_data=[_night(anchor,80)], mindora_record={
    "sleep.scene.rain": [[anchor-3600,60]],
    "sleep.scene.wind": [[anchor+3600,60] for _ in range(4)],
  })
  weekly = build_overview(_d(date=TODAY.isoformat()),p)["weekly_best"]
  assert weekly["audio_name"] == "Rain"
  assert weekly["used_times"] == 1
  assert weekly["score"] == 100
  assert weekly["score_type"] == "usage_match"


# ── build_sleep_week ─────────────────────────────────────────────

def test_sleep_week_window_score_and_scene():
  start = (TODAY - datetime.timedelta(days=6)).isoformat()
  end = TODAY.isoformat()
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(0), 90), _night(_ts(3), 70), _night(_ts(20), 10)],  # 窗口外 10 分不计入
    mindora_record={"sleep.scene.wind": [[now - i * 3600, 60] for i in range(3)]},
  )
  out = build_sleep_week(_d(start_date=start, end_date=end), p)
  assert out["score_summary"]["score"] == 80
  assert out["score_summary"]["label"] == "Excellent"
  assert out["onset_efficiency"]["scenario_name"] == "Wind"
  assert out["onset_efficiency"]["used_times"] == 3


def test_sleep_week_old_window_anchors_last_night():
  """有效时间锚定：请求窗口相对 today 已「过期」，但与 anchor（最近有效夜，18 天前）
  周窗口有交集——不再按无数据降级，返回交集内的真实数据。"""
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(20), 90), _night(_ts(18), 80)],
    mindora_record={"sleep.scene.wind": [[_ts(19), 60]]},
  )
  out = build_sleep_week(_d(
    start_date=(TODAY - datetime.timedelta(days=20)).isoformat(),
    end_date=(TODAY - datetime.timedelta(days=14)).isoformat(),
  ), p)
  assert out["score_summary"]["score"] == 85
  assert "2 valid sleep records" in out["sleep_trends"]["description"]
  # 场景统计与日期窗口同口径锚定最近有效夜：_ts(19) 在 anchor（_ts(18)）前 1 天，
  # 落在锚定 7 天使用窗口内 → 场景模块照常返回
  assert out["onset_efficiency"]["scenario_name"] == "Wind"
  assert out["onset_efficiency"]["used_times"] == 1


def test_sleep_week_window_outside_anchor_window_returns_empty():
  """请求窗口与 anchor 周窗口（[anchor-6, anchor]）无交集仍按空处理。"""
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(1), 90)],
    mindora_record={"sleep.scene.wind": [[_ts(19), 60]]},
  )
  out = build_sleep_week(_d(
    start_date=(TODAY - datetime.timedelta(days=20)).isoformat(),
    end_date=(TODAY - datetime.timedelta(days=14)).isoformat(),
  ), p)
  assert "score_summary" not in out
  assert "onset_efficiency" not in out
  assert out["sleep_trends"]["description"] == ""


# ── build_sleep_month ────────────────────────────────────────────

def test_sleep_month_uses_30d_window_and_real_top3_scenes():
  start = (TODAY - datetime.timedelta(days=29)).isoformat()
  end = TODAY.isoformat()
  now = _ts(0)
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(15), 70), _night(_ts(0), 90), _night(_ts(40), 10)],  # 40 天前不计入（sleep_data 按时间升序存储）
    mindora_record={
      "sleep.scene.a_rain": [[now, 100], [now - 100, 100], [now - 200, 100]],
      "sleep.scene.b_wind": [[now, 100], [now - 100, 100]],
      "sleep.scene.c_wave": [[now, 100]],
      "sleep.scene.d_old": [[now - 40 * 86400, 100]],  # 30 天窗口外
    },
  )
  out = build_sleep_month(_d(start_date=start, end_date=end), p)
  assert out["score_summary"]["score"] == 80
  assert out["onset_efficiency"]["scenario_list"] == ["A Rain", "B Wind", "C Wave"]
  series = out["sleep_trends"]["score_series"]
  assert [p_["score"] for p_ in series] == [70, 90]


def test_sleep_month_empty_series_without_data():
  out = build_sleep_month(
    _d(start_date=(TODAY - datetime.timedelta(days=29)).isoformat(), end_date=TODAY.isoformat()),
    UserProfile(uid="u"),
  )
  assert "score_summary" not in out
  assert out["sleep_trends"]["score_series"] == []  # 不再有 base=62 的假增长曲线
  assert "onset_efficiency" not in out


def test_sleep_month_old_window_anchors_last_night():
  """有效时间锚定：月窗口锚定最近有效夜（40 天前）——请求窗口与 anchor 月窗口
  有交集即返回真实数据，不再因相对 today 超窗而清空。"""
  p = UserProfile(
    uid="u",
    sleep_data=[_night(_ts(45), 90), _night(_ts(40), 80)],
    mindora_record={"sleep.scene.wind": [[_ts(42), 60]]},
  )
  out = build_sleep_month(_d(
    start_date=(TODAY - datetime.timedelta(days=50)).isoformat(),
    end_date=(TODAY - datetime.timedelta(days=35)).isoformat(),
  ), p)
  assert out["score_summary"]["score"] == 85
  series = out["sleep_trends"]["score_series"]
  assert [p_["score"] for p_ in series] == [90, 80]
  # 场景统计与日期窗口同口径锚定最近有效夜：_ts(42) 在 anchor（_ts(40)）前 2 天，
  # 落在锚定 30 天使用窗口内 → top3 照常返回
  assert out["onset_efficiency"]["scenario_list"] == ["Wind"]


# ── build_explore ────────────────────────────────────────────────

def _explore_profile() -> UserProfile:
  t0 = _ts(0) - 8 * 3600  # 当夜起点
  night = _night(
    _ts(0), 88,
    soe=76, onset=14, sleep_arch_index=82, night_var_index=35,
    first_sleep_time="23:40", hr_before_sleep=62, rr_before_sleep=14,
    hr_min=54, hr_max=86, respiratory_var=12,
    sleep_status=[
      SleepElement(start_time=t0, duration=180, sleep_type="core"),
      SleepElement(start_time=t0 + 180 * 60, duration=90, sleep_type="deep"),
      SleepElement(start_time=t0 + 270 * 60, duration=20, sleep_type="awake"),
      SleepElement(start_time=t0 + 290 * 60, duration=60, sleep_type="rem"),
    ],
    night_events=[NightEvent(timestamp=t0 + 100 * 60, event_type="intervention", detail="Rain Wash")],
  )
  return UserProfile(
    uid="u",
    sleep_data=[night],
    mindora_record={"sleep.scene.rain_wash": [[_ts(0) - i * 3600, 60] for i in range(4)]},
    sleep_analysis={"most_used_scene_7d": {"scene_id": "rain_wash", "scene_name": "Rain Wash", "count": 4}},
  )


def test_explore_all_real_fields():
  out = build_explore(_d(date=TODAY.isoformat()), _explore_profile())
  assert out["data_ready"] is True

  ss = out["score_summary"]
  assert ss["score"] == 46
  assert (ss["efficiency_score"], ss["structure_score"], ss["fluctuation_score"]) == (92, 42, 74)
  assert ss["dimension_scores"]["schedule"] == ss["structure_score"]
  assert ss["dimension_scores"]["continuity"] == ss["fluctuation_score"]
  assert ss["index_version"] == "mindora-insight-v3-draft-5"

  oe = out["onset_efficiency"]
  assert oe["onset_minutes"] == 14
  assert oe["first_sleep_time"] == "23:40"
  assert oe["pre_sleep_heart_rate"] == "62bpm"

  st = out["sleep_structure"]
  # 最长连续非 awake 段 = 180+90 = 270 分钟（不是 time_in_bed）
  assert st["continuous_sleep_minutes"] == 270
  assert st["score"] == 42

  nf = out["night_fluctuation"]
  assert nf["heart_rate_range"] == "54-86bpm"  # 不再是 hr_mid±15 的编造
  assert nf["intervention"] == "Rain Wash"
  assert nf["awake_count"] == 1
  assert nf["awake_duration_minutes"] == 20
  assert nf["score"] == 74

  assert out["scene_preference"]["scene_name"] == "Rain Wash"
  assert out["sleep_advice"]["description"]


def test_explore_no_data_returns_empty_state_only():
  out = build_explore(_d(date=TODAY.isoformat()), UserProfile(uid="u"))
  assert out["data_ready"] is False
  assert set(out) == {"data_ready", "header_summary", "sleep_advice"}
  assert out["sleep_advice"]["description"]


def test_explore_stale_night_anchors_last_night():
  """有效时间锚定：最新一夜超 7 天（停戴超期）不再进空态——锚定最后一夜展示。"""
  p = UserProfile(uid="u", sleep_data=[_night(_ts(10), 88)])
  out = build_explore(_d(date=None), p)  # 不带 date → 默认锚定最新有效夜
  assert out["data_ready"] is True
  assert out["score_summary"]["score"] == 88
  assert out["score_summary"]["date"] == (TODAY - datetime.timedelta(days=10)).isoformat()


# ── 有效时间锚定（停戴用户：最新有效夜 10 天前）──────────────────────────────

STALE_NIGHT = TODAY - datetime.timedelta(days=10)


def _stale_profile() -> UserProfile:
  return UserProfile(uid="u", sleep_data=[_night(_ts(10), 88, soe=76)])


def test_stale_user_sleep_day_anchors_last_night():
  """停戴超期不再降级为无数据：sleep_day 返回最后一夜数据，date==该夜日期。"""
  out = build_sleep_day(_d(date=None), _stale_profile())
  assert "score_summary" not in out
  assert out["sleep_metrics"]["date"] == STALE_NIGHT.isoformat()


def test_stale_user_week_month_default_windows_end_at_last_night():
  """停戴用户周/月默认窗口 end==最近有效夜日期。"""
  p = _stale_profile()
  week = build_sleep_week(_d(start_date=None, end_date=None), p)
  assert week["score_summary"]["score"] == 88
  assert week["sleep_trends"]["end_date"] == STALE_NIGHT.isoformat()
  assert week["sleep_trends"]["start_date"] == (STALE_NIGHT - datetime.timedelta(days=6)).isoformat()

  month = build_sleep_month(_d(start_date=None, end_date=None), p)
  assert month["score_summary"]["score"] == 88
  assert month["sleep_trends"]["end_date"] == STALE_NIGHT.isoformat()
  assert month["sleep_trends"]["score_series"][0]["score"] == 88


def test_stale_user_overview_explore_anchor_last_night():
  """停戴用户 overview/explore 同样锚定最后一夜。"""
  p = _stale_profile()
  ov = build_overview(_d(date=None), p)
  assert ov["overall_score"]["score"] == 88
  assert ov["sleep_insight"]["date"] == STALE_NIGHT.isoformat()

  ex = build_explore(_d(date=None), p)
  assert ex["data_ready"] is True
  assert ex["score_summary"]["score"] == 88
  assert ex["score_summary"]["date"] == STALE_NIGHT.isoformat()


def test_explore_omits_missing_numeric_fields():
  night = _night(_ts(0), 70)  # 只有分数，无 soe/onset/hr 等
  out = build_explore(_d(date=TODAY.isoformat()), UserProfile(uid="u", sleep_data=[night]))
  assert out["score_summary"]["score"] == 70
  assert "onset_minutes" not in out["onset_efficiency"]
  assert "heart_rate_range" not in out["night_fluctuation"]
  assert "awake_count" not in out["night_fluctuation"]  # 无 sleep_status 序列


def test_explore_documented_schema_and_current_rule_text():
  profile = _explore_profile()
  result = build_explore(_d(date=TODAY.isoformat()), profile)
  assert set(result) == {"data_ready", "header_summary", "score_summary", "onset_efficiency",
                         "sleep_structure", "night_fluctuation", "scene_preference", "sleep_advice"}
  for key in ("onset_efficiency", "sleep_structure", "night_fluctuation", "sleep_advice"):
    assert result[key]["description"]
  assert result["header_summary"]["intro_text"]
  assert result["header_summary"]["intro_detail_text"]
  selected = build_explore(_d(date=TODAY.isoformat(), modules=["onset_efficiency"]), profile)
  assert set(selected) == {"data_ready", "onset_efficiency"}


def test_explore_legacy_healthkit_structure_and_source_date():
  profile = _explore_profile()
  row = profile.sleep_data[0]
  row.source = "healthkit"
  row.sleep_arch_index = 100
  row.sleep_status = [x for x in row.sleep_status if x.sleep_type != "core"]
  row.timestamp = _ts(3)
  before = profile.model_dump()
  out = build_explore(_d(date=TODAY.isoformat()), profile)
  assert out["sleep_structure"]["core_percent"] == "0.0%"  # 当晚已分期即可展示，不依赖个人基线
  assert "rem_percent" in out["sleep_structure"]
  assert "deep_percent" in out["sleep_structure"]
  assert out["sleep_structure"]["score"] == out["score_summary"]["structure_score"]
  assert out["sleep_structure"]["date"] == (TODAY - datetime.timedelta(days=3)).isoformat()
  assert profile.model_dump() == before


def test_explore_omits_stage_percentages_when_night_has_unspecified_stage():
  profile = _explore_profile()
  row = profile.sleep_data[0]
  row.sleep_status.append(SleepElement(
    start_time=row.sleep_status[-1].start_time + 60 * 60,
    duration=10, sleep_type="unspecified",
  ))
  structure = build_explore(_d(date=TODAY.isoformat()), profile)["sleep_structure"]
  assert structure["stage_classification"] == "contains_unspecified"
  assert all(key not in structure for key in ("rem_percent", "deep_percent", "core_percent"))


def test_explore_same_day_stale_report_and_language_are_rejected():
  from analysis_content import explore_input_fingerprint
  from user_profile import AnalysisTextReport
  profile = _explore_profile()
  report = AnalysisTextReport(request_type="analysis_explore", date=TODAY.isoformat(),
      generated_at=int(time.time()), language="en",
      input_fingerprint=explore_input_fingerprint(profile, "en"),
      modules={"onset_efficiency": {"description": "polished", "score": 999},
               "insight": {"onset": "must not leak"}, "sleep_structure": {"description": ""}})
  profile.analysis_reports["analysis_explore"] = [report]
  d = _d(date=TODAY.isoformat())
  assert build_explore(d, profile)["onset_efficiency"]["description"] == "polished"
  assert build_explore(d, profile)["onset_efficiency"]["score"] == 92
  assert build_explore(d, profile)["sleep_structure"]["description"]
  profile.sleep_data[0].onset = 10
  out = build_explore(d, profile)
  assert "10" in out["onset_efficiency"]["description"]
  assert "insight" not in out
  report.input_fingerprint = explore_input_fingerprint(profile, "en")
  out = build_explore(_d(date=TODAY.isoformat(), language="zh-Hans"), profile)
  assert "10" in out["onset_efficiency"]["description"]
  assert "polished" not in out["onset_efficiency"]["description"]


@pytest.mark.parametrize("days,builder", [(7, build_sleep_week), (30, build_sleep_month)])
def test_period_text_uses_effective_window_and_protects_numbers(days, builder):
  from user_profile import AnalysisTextReport
  end = TODAY - datetime.timedelta(days=3)
  start = end - datetime.timedelta(days=days - 1)
  profile = UserProfile(sleep_data=[_night(_ts(3), 80, onset=10)])
  rt = "analysis_sleep_week" if days == 7 else "analysis_sleep_month"
  profile.analysis_reports[rt] = [AnalysisTextReport(
    request_type=rt, date=end.isoformat(), start_date=start.isoformat(), end_date=end.isoformat(),
    language="en", generated_at=int(time.time()), modules={
      "sleep_trends": {"body": "Stored title", "description": "Stored trend", "score_series": [{"score": 999}]},
      "score_summary": {"score": 999},
    })]
  from analysis_content import explore_input_fingerprint
  profile.analysis_reports[rt][0].input_fingerprint = explore_input_fingerprint(profile, "en")
  d = _d(start_date=(TODAY - datetime.timedelta(days=days-1)).isoformat(), end_date=TODAY.isoformat())
  before = profile.model_dump()
  out = builder(d, profile)
  assert out["sleep_trends"]["description"] == "Stored trend"
  assert out["sleep_trends"]["start_date"] == start.isoformat()
  assert out["score_summary"]["score"] == 80
  if days == 30:
    assert out["sleep_trends"]["score_series"][0]["score"] == 80
    assert "10" in out["onset_efficiency"]["description"]
  d.modules = ["sleep_trends"]
  assert set(builder(d, profile)) == {"sleep_trends"}
  assert profile.model_dump() == before
  d.language = "zh-Hans"
  assert "有效睡眠记录" in builder(d, profile)["sleep_trends"]["description"]
  profile.analysis_reports[rt][0].language = "zh-Hans"
  profile.analysis_reports[rt][0].modules["sleep_trends"]["description"] = ""
  assert "有效睡眠记录" in builder(d, profile)["sleep_trends"]["description"]


@pytest.mark.parametrize("days,builder", [(7, build_sleep_week), (30, build_sleep_month)])
def test_period_fallback_uses_historical_subwindow_only(days, builder):
  profile = UserProfile(sleep_data=[_night(_ts(4), 80), _night(_ts(3), 90), _night(_ts(0), 95)])
  d = _d(start_date=(TODAY-datetime.timedelta(days=4)).isoformat(),
         end_date=(TODAY-datetime.timedelta(days=3)).isoformat())
  out = builder(d, profile)
  assert out["score_summary"]["score"] == 85
  assert "2 valid sleep records" in out["sleep_trends"]["description"]


def test_month_scene_description_uses_returned_scenes_and_onset():
  profile = UserProfile(sleep_data=[_night(_ts(0), 80, onset=12)],
                        mindora_record={"sleep.scene.rain": [[_ts(0), 60]]})
  out = build_sleep_month(_d(start_date=None, end_date=None, language="zh-Hans"), profile)
  onset = out["onset_efficiency"]
  assert onset["scenario_list"][0] in onset["description"]
  assert "12 分钟" in onset["description"]


@pytest.mark.parametrize("builder", [build_sleep_week, build_sleep_month])
def test_short_sleep_period_cannot_be_good(builder):
  rows = [_night(_ts(i), 100, sleep_status=[SleepElement(start_time=_ts(i)-252*60,
                 duration=252, sleep_type="core")]) for i in range(7)]
  out = builder(_d(start_date=None, end_date=None, language="zh-Hans"), UserProfile(sleep_data=rows))
  assert out["score_summary"]["score"] == 50
  assert out["score_summary"]["label"] == "睡眠不足"


def test_all_total_score_views_cap_short_sleep_without_mutation():
  p = UserProfile(sleep_data=[_night(_ts(0), 99, sleep_status=[
      SleepElement(start_time=_ts(0)-480*60, duration=240, sleep_type="core"),
      SleepElement(start_time=_ts(0)-240*60, duration=240, sleep_type="awake"),
  ])])
  d = _d(date=TODAY.isoformat(), start_date=None, end_date=None)
  before = p.model_dump()
  assert build_overview(d,p)["overall_score"]["score"] < 60
  assert build_explore(d,p)["score_summary"]["score"] < 60
  month = build_sleep_month(d,p)
  assert month["score_summary"]["score"] < 60
  assert month["sleep_trends"]["score_series"][0]["score"] < 60
  assert p.model_dump() == before
