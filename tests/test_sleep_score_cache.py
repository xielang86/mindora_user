import datetime

from analysis_score import SCORE_VERSION, INDEX_VERSION, daily_score
from insight_rules import compute_night_indices
from sleep_score_cache import (UNAVAILABLE_SCORE_VERSION, cached_daily_score,
                               cached_night_indices, refresh_score_cache, score_cache_ready)
from user_profile import SleepElement, SleepResult, SleepPlan, UserProfile


def _night(end, minutes=450):
  return SleepResult(
    timestamp=end, first_sleep_time="23:30", wake_time="07:00",
    sleep_status=[SleepElement(start_time=end - minutes * 60,
                               duration=minutes, sleep_type="core")],
  )


def test_refresh_persists_same_four_scores_as_analysis_calculation():
  end = int(datetime.datetime(2026, 11, 11, 7, tzinfo=datetime.timezone.utc).timestamp())
  profile = UserProfile(sleep_plan=SleepPlan(target_duration_min=450,
                                             target_bed_time="23:30", target_wake_time="07:00"),
                        sleep_data=[_night(end - 86400), _night(end)])
  assert refresh_score_cache(profile)
  assert score_cache_ready(profile)
  for row in profile.sleep_data:
    indices = compute_night_indices(profile, row)
    live = daily_score(row, profile, insight_indices=indices)
    assert row.sleep_quality == live["score"]
    assert (row.soe, row.sleep_arch_index, row.night_var_index) == (
      indices["onset_index"], indices["structure_index"], indices["stability_index"])
    assert row.score_version == SCORE_VERSION
    assert row.index_version == INDEX_VERSION
    assert cached_daily_score(row, profile) == live
    assert cached_night_indices(row, profile) == {
      "onset_index": row.soe, "structure_index": row.sleep_arch_index,
      "stability_index": row.night_var_index}
  assert not refresh_score_cache(profile)


def test_stage_correction_refreshes_cached_scores():
  end = int(datetime.datetime(2026, 11, 11, 7, tzinfo=datetime.timezone.utc).timestamp())
  row = _night(end)
  profile = UserProfile(sleep_data=[row])
  refresh_score_cache(profile)
  old_score = row.sleep_quality
  row.sleep_status = [SleepElement(start_time=end - 240 * 60, duration=240, sleep_type="core")]
  assert refresh_score_cache(profile)
  assert row.sleep_quality != old_score


def test_score_only_legacy_row_does_not_impersonate_new_score():
  row = SleepResult(timestamp=1000, sleep_quality=95, soe=95,
                    sleep_arch_index=95, night_var_index=95)
  profile = UserProfile(sleep_data=[row])
  assert refresh_score_cache(profile)
  assert (row.sleep_quality, row.soe, row.sleep_arch_index, row.night_var_index) == (None,) * 4
  assert row.score_version == UNAVAILABLE_SCORE_VERSION
  assert score_cache_ready(profile)
