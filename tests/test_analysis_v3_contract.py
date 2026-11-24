import datetime

import pytest

from analysis_score import SCORE_VERSION, daily_score, period_score
from insight_rules import compute_baselines, compute_insight_indices
from analysis_content import AnalysisContentService, explore_input_fingerprint
from llm.analysis import polish_output_ok
from insight_rules import RuleConclusion
from sleep_metrics import build_sleep_metrics
from user_profile import AnalysisTextReport, SleepElement, SleepPlan, SleepResult, SyncedSleepPlan, UserProfile
from user_server import UserServer


def _night(ts, minutes=450):
  return SleepResult(timestamp=ts, sleep_quality=83, first_sleep_time="23:30", wake_time="07:00",
                     in_bed_intervals=[(ts - 480 * 60, ts)],
                     sleep_status=[SleepElement(start_time=ts - minutes * 60,
                                                duration=minutes, sleep_type="core")])


def test_v3_score_needs_three_dimensions_and_keeps_legacy_version_separate():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  profile = UserProfile(sleep_plan=SleepPlan(target_duration_min=450,
                                             target_bed_time="23:30", target_wake_time="07:00"))
  measured = _night(ts)
  result = daily_score(measured, profile)
  assert result["score_version"] == SCORE_VERSION
  assert result["dimension_count"] == 4
  assert result["dimension_scores"] == {"continuity": 100, "duration": 100,
                                          "efficiency": 100, "schedule": 100}
  assert result["score"] == 100
  legacy = SleepResult(timestamp=ts - 86400, sleep_quality=70)
  assert daily_score(legacy, profile)["score_version"] == "mindora-score-legacy-v1"
  assert period_score([legacy, measured], profile)["score_version"] == SCORE_VERSION
  assert period_score([legacy, measured], profile)["valid_nights"] == 1


def test_future_plan_does_not_change_old_healthkit_night_target():
  ts = int(datetime.datetime(2026, 9, 24, 0, tzinfo=datetime.timezone.utc).timestamp())
  night = SleepResult(timestamp=ts, source="healthkit", sleep_quality=90, onset=10,
                      sleep_status=[SleepElement(start_time=ts - 8 * 3600, duration=420, sleep_type="core"),
                                    SleepElement(start_time=ts - 3600, duration=10, sleep_type="awake")])
  plan = SyncedSleepPlan(plan_id="P1", updated_at=ts + 86400, status="active",
                         activated_at=ts + 86400, target_minutes=660,
                         sleep_time=1230, wake_time=450)
  profile = UserProfile(sleep_data=[night], sleep_plans=[plan])
  assert daily_score(night, profile)["duration_target_source"] == "default_8h"
  assert period_score([night], profile)["score_version"] == SCORE_VERSION
  indices = compute_insight_indices(profile, compute_baselines(profile, datetime.timezone.utc))
  assert indices["onset_index"] == 85  # estimated 10 min is capped, not perfect
  assert indices["structure_index"] == 78  # duration uses the existing default 8h target
  assert indices["stability_index"] == 100


def test_overall_uses_each_nights_own_structure_and_stability_indices():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  first = SleepResult(timestamp=ts, sleep_status=[
    SleepElement(start_time=ts - 480 * 60, duration=480, sleep_type="core")])
  second = SleepResult(timestamp=ts + 86400, sleep_status=[
    SleepElement(start_time=ts + 86400 - 480 * 60, duration=400, sleep_type="core"),
    SleepElement(start_time=ts + 86400 - 80 * 60, duration=80, sleep_type="awake")])
  profile = UserProfile(sleep_data=[first, second], last_request_timezone="UTC")
  first_view = UserProfile(sleep_data=[first], last_request_timezone="UTC")
  first_indices = compute_insight_indices(first_view, compute_baselines(first_view, datetime.timezone.utc))
  second_indices = compute_insight_indices(profile, compute_baselines(profile, datetime.timezone.utc))

  first_score = daily_score(first, profile)
  second_score = daily_score(second, profile)
  assert first_score["dimension_scores"]["schedule"] == first_indices["structure_index"]
  assert first_score["dimension_scores"]["continuity"] == first_indices["stability_index"]
  assert second_score["dimension_scores"]["schedule"] == second_indices["structure_index"]
  assert second_score["dimension_scores"]["continuity"] == second_indices["stability_index"]
  assert first_score["score"] != second_score["score"]


@pytest.mark.parametrize("awake_minutes,continuity,structure", [
  (0, 100, 70), (3, 100, 71), (50, 50, 67), (97, 0, 50),
])
def test_structure_keeps_documented_weights_with_awake_share_continuity(awake_minutes, continuity, structure):
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  first_sleep = (100 - awake_minutes) / 2
  night = SleepResult(timestamp=ts, sleep_status=[
    SleepElement(start_time=ts - 100 * 60, duration=first_sleep, sleep_type="core"),
    SleepElement(start_time=ts - int((100 - first_sleep) * 60), duration=awake_minutes, sleep_type="awake"),
    SleepElement(start_time=ts - int(first_sleep * 60), duration=first_sleep, sleep_type="core"),
  ])
  # The goal changes the duration subscore; the stability index exposes the
  # continuity subscore when no heart/respiratory subscores are available.
  profile = UserProfile(sleep_data=[night], sleep_plan=SleepPlan(
    target_duration_min=20, target_bed_time="23:30", target_wake_time="07:00"))
  indices = compute_insight_indices(profile, compute_baselines(profile, datetime.timezone.utc))
  assert indices["structure_index"] == structure
  assert indices["stability_index"] == continuity


def test_short_night_with_four_dimensions_stays_below_sixty():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  profile = UserProfile(sleep_plan=SleepPlan(target_duration_min=240,
                                             target_bed_time="23:30", target_wake_time="07:00"))
  night = _night(ts, minutes=240)
  night.in_bed_intervals = [(ts - 260 * 60, ts)]
  result = daily_score(night, profile)
  assert result["dimension_count"] == 4
  assert result["score"] < 60


def test_unspecified_and_inferred_onset_are_marked_without_changing_stages():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  night = SleepResult(timestamp=ts, source="healthkit", onset=10,
                      sleep_status=[SleepElement(start_time=ts - 3600, duration=60, sleep_type="core")])
  profile = UserProfile(sleep_data=[night], behaviors={"sleep_stage_unspecified": [[ts - 3600, 3600]]})
  metrics = build_sleep_metrics(night, profile, datetime.timezone.utc)
  assert metrics["sleep_onset_source"] == "estimated"
  assert metrics["stage_classification"] == "contains_unspecified"
  assert night.sleep_status[0].sleep_type == "core"


def test_polish_rejects_new_claims_missing_numbers_and_moved_numbers():
  conclusions = [RuleConclusion(key="onset", theme="onset", title="入睡洞察", text="昨晚入睡用时 20 分钟。")]
  assert polish_output_ok({"onset": {"title": "入睡洞察", "text": "昨晚入睡用时 20 分钟！"}}, conclusions, "zh-Hans")
  assert not polish_output_ok({"onset": {"title": "入睡洞察", "text": "Mindora 让你的睡眠变得完美。"}}, conclusions, "zh-Hans")
  assert not polish_output_ok({"onset": {"title": "入睡洞察", "text": "昨晚入睡用时 30 分钟。"}}, conclusions, "zh-Hans")
  negative = [RuleConclusion(key="onset", theme="onset", title="变化", text="变化 -3 分钟。")]
  assert not polish_output_ok({"onset": {"title": "变化", "text": "变化 3 分钟。"}}, negative, "zh-Hans")


def test_same_day_report_is_invalidated_after_record_correction():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  profile = UserProfile(sleep_data=[_night(ts)], last_request_timezone="UTC")
  date = "2026-10-12"
  report = AnalysisTextReport(request_type="analysis_sleep_day", date=date, language="en",
                              generated_at=ts + 10, llm_used=True,
                              input_fingerprint=explore_input_fingerprint(profile, "en"),
                              modules={"sleep_scenarios": {"description": "old"}})
  profile.analysis_reports["analysis_sleep_day"] = [report]
  assert AnalysisContentService._find_analysis_report(profile, "analysis_sleep_day", date, None, None) is report
  assert UserServer.__new__(UserServer)._analysis_needed(profile)[0] is False
  profile.sleep_data[0].sleep_quality = 55
  assert AnalysisContentService._find_analysis_report(profile, "analysis_sleep_day", date, None, None) is None
  assert UserServer.__new__(UserServer)._analysis_needed(profile)[0] is True
  profile.sleep_data[0].sleep_quality = 83
  profile.behaviors["sleep_stage_unspecified"] = [[ts - 3600, 3600]]
  assert AnalysisContentService._find_analysis_report(profile, "analysis_sleep_day", date, None, None) is None


def test_valid_polish_text_is_written_to_legacy_insight_content():
  ts = int(datetime.datetime(2026, 10, 12, 7, tzinfo=datetime.timezone.utc).timestamp())
  profile = UserProfile(sleep_data=[_night(ts)], last_request_timezone="UTC", last_request_language="zh-Hans")

  class FakeLLM:
    enabled = True

    def generate_sync(self, request_type, ctx, language, modules):
      assert request_type == "sleep_insight_polish"
      return {c["key"]: {"title": c["title"], "text": c["text"].rstrip("。") + "！"}
              for c in ctx["conclusions"]}

  report = AnalysisContentService(lambda: FakeLLM()).calc_sleep_insight("u", profile)
  assert report.llm_used is True
  assert report.greeting.content.endswith("！")
