import asyncio
import datetime
import threading
import time

from user_profile import (
  Address, AnalysisTextReport, Profile, ProfileData, ProfileRequest, SleepElement, SleepInsightReport, SleepScenario, SleepResult, UserProfile,
)
from user_server import UserProfileServ, UserServer
from llm import SleepAnalysisLLM
from config import Config


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None
  return serv


def _insight_report(greeting_title: str = "Hello", generated_at: int | None = None) -> SleepInsightReport:
  return SleepInsightReport(
    date="2026-06-24",
    language="en",
    generated_at=generated_at if generated_at is not None else int(time.time()),
    greeting={"module_id": 0, "title": greeting_title, "content": "welcome"},
  )


def _scenario(name: str) -> SleepScenario:
  return SleepScenario.model_validate(
    {
      "scenario_name": name,
      "stages": [{"cmd_name": name}],
    }
  )


def _profile() -> UserProfile:
  return UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [], "sleep_status": [], "clicks": [], "plays": []},
    sleep_scenarios_reco=[_scenario("keep_me")],
    standard_sop_reco=[_scenario("old_standard")],
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )


def test_sleep_health_sparse_updates_preserve_ranking_inputs(monkeypatch):
  from user_profile import SleepHealthProfile
  serv = _serv()
  existing = _profile()
  existing.sleep_health = SleepHealthProfile(improvement_goal="减少夜醒", stress_type="工作压力")
  saved = {}
  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update(profile=profile))
  assert serv.update_profile_basic("test", UserProfile(sleep_health=SleepHealthProfile(improvement_goal="缩短入睡时间")))
  assert saved["profile"].sleep_health.improvement_goal == "缩短入睡时间"
  assert saved["profile"].sleep_health.stress_type == "工作压力"


def test_sleep_health_and_mode_null_do_not_clear_existing(monkeypatch):
  from user_profile import SleepHealthProfile, SleepModeProfile
  serv = _serv()
  existing = _profile()
  existing.sleep_health = SleepHealthProfile(improvement_goal="减少夜醒", stress_type="工作压力")
  existing.sleep_mode = SleepModeProfile(start_time="22:00", end_time="07:00")
  saved = {}
  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update(profile=profile))
  payload = UserProfile.model_validate({
    "sleep_health": {"improvement_goal": None, "stress_type": "家庭"},
    "sleep_mode": None,
  })
  serv.update_profile_basic("test", payload)
  assert saved["profile"].sleep_health.improvement_goal == "减少夜醒"
  assert saved["profile"].sleep_health.stress_type == "家庭"
  assert saved["profile"].sleep_mode.start_time == "22:00"
  assert saved["profile"].sleep_mode.end_time == "07:00"

  payload = UserProfile.model_validate({"sleep_health": None, "sleep_mode": {"start_time": None}})
  serv.update_profile_basic("test", payload)
  assert saved["profile"].sleep_health.improvement_goal == "减少夜醒"
  assert saved["profile"].sleep_mode.start_time == "22:00"


def test_sleep_data_same_timestamp_sparse_and_null_keep_existing_fields():
  old = [SleepResult(timestamp=100, sleep_quality=82, onset=12, avg_heart_rate=61)]
  new = [SleepResult.model_validate({"timestamp": 100, "sleep_quality": None, "onset": 10})]
  merged = UserProfileServ._merge_sleep_data(old, new)
  assert merged[0].sleep_quality == 82
  assert merged[0].onset == 10
  assert merged[0].avg_heart_rate == 61


def test_llm_update_saves_sop_ranking_details(monkeypatch):
  serv = _serv()
  existing = _profile()
  saved = {}
  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update(profile=profile))
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda *args: [])
  def recommend(uid, profile, old):
    profile.sop_recommendation_details = {"rules_version": "test", "llm_route": "volc_ark"}
    return [_scenario("sleep.scene.cocos_island_moonlight")]
  monkeypatch.setattr(serv, "calc_standard_sop_reco", recommend)
  assert serv.update_profile_llm("test", skip_sleep_analysis_update=True)
  assert saved["profile"].sop_recommendation_details["llm_route"] == "volc_ark"


def test_profile_data_accepts_skip_sleep_scenarios_reco_update():
  data = ProfileData.model_validate(
    {
      "uid": "test_debug_user_001",
      "skip_sleep_scenarios_reco_update": True,
    }
  )
  assert data.skip_sleep_scenarios_reco_update is True


def test_update_profile_skips_sleep_scenarios_reco_regeneration(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(
    serv,
    "calc_standard_sop_reco",
    lambda uid, new_profile, old_profile: [_scenario("new_standard")],
  )

  def fail_calc_sleep_reco(uid, new_profile, old_profile):
    raise AssertionError("sleep_scenarios_reco should not be recalculated when skip flag is set")

  monkeypatch.setattr(serv, "calc_sleep_reco", fail_calc_sleep_reco)

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-1", new_profile, skip_sleep_scenarios_reco_update=True)

  assert succ is True
  assert saved["uid"] == "user-1"
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["keep_me"]
  # standard_sop_reco already existed, so skip flag should keep the old value
  assert [item.scenario_name for item in saved["profile"].standard_sop_reco] == ["old_standard"]


def test_update_profile_regenerates_sleep_scenarios_reco_by_default(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda uid, new_profile, old_profile: old_profile.standard_sop_reco)
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda uid, new_profile, old_profile: [_scenario("fresh_sleep_reco")])

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(2, 68)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-2", new_profile)

  assert succ is True
  assert saved["uid"] == "user-2"
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["fresh_sleep_reco"]


def test_update_profile_fills_empty_standard_sop_reco_even_when_skipped(monkeypatch):
  serv = _serv()
  saved = {}
  existing = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [], "sleep_status": [], "clicks": [], "plays": []},
    sleep_scenarios_reco=[_scenario("keep_me")],
    standard_sop_reco=[],
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(
    serv,
    "calc_standard_sop_reco",
    lambda uid, new_profile, old_profile: [_scenario("filled_standard")],
  )

  def fail_calc_sleep_reco(uid, new_profile, old_profile):
    raise AssertionError("sleep_scenarios_reco should not be recalculated when skip flag is set")

  monkeypatch.setattr(serv, "calc_sleep_reco", fail_calc_sleep_reco)

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-3", new_profile, skip_sleep_scenarios_reco_update=True)

  assert succ is True
  assert saved["uid"] == "user-3"
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["keep_me"]
  assert [item.scenario_name for item in saved["profile"].standard_sop_reco] == ["filled_standard"]


def test_merge_behavior_truncates_to_max_length():
  serv = _serv()
  old = {"heart_rate": [[i, 60 + i] for i in range(60)]}
  new = {"heart_rate": [[i, 100 + i] for i in range(60, 160)]}
  merged = serv._merge_behavior(old, new)
  assert len(merged["heart_rate"]) == UserProfileServ.MAX_BEHAVIOR_LEN
  ts = [x[0] for x in merged["heart_rate"]]
  assert ts == sorted(ts)
  assert ts[0] == 60


def _night(timestamp: int, quality: float = 80.0, with_status: bool = True) -> SleepResult:
  from user_profile import SleepElement
  status = []
  if with_status:
    # 当夜窗口：[timestamp-8h, timestamp]（覆盖整晚）
    status = [SleepElement(start_time=timestamp - 8 * 3600, duration=480, sleep_type="core")]
  return SleepResult(timestamp=timestamp, sleep_quality=quality, sleep_status=status)


def test_merge_sleep_data_dedups_sorts_and_truncates():
  old = [_night(ts, quality=70.0) for ts in (100, 200, 300)]
  new = [_night(200, quality=95.0), _night(50)]  # 200 覆盖旧记录，50 乱序插入
  merged = UserProfileServ._merge_sleep_data(old, new)
  assert [r.timestamp for r in merged] == [50, 100, 200, 300]
  assert merged[2].sleep_quality == 95.0  # ts=200 被新记录覆盖

  # 超过 MAX_SLEEP_DATA_LEN 截断保留最近 N 条（new 为空时直接返回 old，不做截断）
  many = [_night(ts) for ts in range(UserProfileServ.MAX_SLEEP_DATA_LEN + 5)]
  merged = UserProfileServ._merge_sleep_data(many, [_night(10_000)])
  assert len(merged) == UserProfileServ.MAX_SLEEP_DATA_LEN
  assert merged[-1].timestamp == 10_000


def test_apply_basic_update_merges_sleep_data_for_existing_profile():
  serv = _serv()
  existing = _profile()
  existing.sleep_data = [_night(100)]
  new_profile = UserProfile(uid_emb=[], long_term_profile=[], sleep_data=[_night(200)])
  merged = serv._apply_basic_update("u", new_profile, existing)
  assert [r.timestamp for r in merged.sleep_data] == [100, 200]


def test_update_night_hr_range_from_behaviors():
  serv = _serv()
  t0 = 1_000_000
  profile = _profile()
  # v2 口径：一晚一对 min/max，时间戳 = 该晚会话起点，落在窗口 [t0-8h, t0] 内才配对
  profile.behaviors = {
    "sleep_heart_rate_min": [[t0 - 8 * 3600, 55], [t0 + 3600, 40]],
    "sleep_heart_rate_max": [[t0 - 8 * 3600, 88], [t0 + 3600, 200]],
  }
  profile.sleep_data = [_night(t0)]
  serv._update_night_hr_range(profile)
  # 窗口内的对：55 / 88；窗口外（t0+3600）的对不计入
  assert profile.sleep_data[0].hr_min == 55
  assert profile.sleep_data[0].hr_max == 88


def test_update_night_hr_range_skips_nights_without_window_or_samples():
  serv = _serv()
  profile = _profile()
  profile.behaviors = {"heart_rate": [[1, 70]]}
  profile.sleep_data = [_night(100, with_status=False)]  # 无 sleep_status → 无窗口
  serv._update_night_hr_range(profile)
  assert profile.sleep_data[0].hr_min is None
  assert profile.sleep_data[0].hr_max is None

  # 有窗口但 behaviors 无心率样本 → 保持 None
  profile.behaviors = {"heart_rate": []}
  profile.sleep_data = [_night(100)]
  serv._update_night_hr_range(profile)
  assert profile.sleep_data[0].hr_min is None


def test_apply_basic_update_computes_hr_range_on_new_profile():
  serv = _serv()
  t0 = 1_000_000
  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={
      "sleep_heart_rate_min": [[t0 - 8 * 3600, 60]],
      "sleep_heart_rate_max": [[t0 - 8 * 3600, 90]],
    },
    sleep_data=[_night(t0)],
  )
  result = serv._apply_basic_update("u", new_profile, None)
  assert result.sleep_data[0].hr_min == 60
  assert result.sleep_data[0].hr_max == 90


def test_update_mindora_record_counts_sop_starts():
  serv = _serv()
  profile = _profile()
  profile.mindora_record = {
    "sleep.scene.amalfi_breeze": [],
    "sleep.scene.cocos_island_moonlight": [],
  }
  profile.sleep_data = [SleepResult(timestamp=7300, sleep_status=[
    SleepElement(start_time=200, duration=120, sleep_type="core")])]

  new_profile = UserProfile(
    behaviors={
      "plays": [
        [100, {"event": "sop_start", "cmd": "sleep.scene.amalfi_breeze", "duration": 300}],
        [101, {"event": "sop_stage_start_play", "cmd": "sleep.scene.amalfi_breeze"}],
        [102, {"event": "sop_start", "cmd": "sleep.scene.cocos_island_moonlight", "duration": 300}],
      ]
    }
  )

  profile.behaviors["plays"] = new_profile.behaviors["plays"]
  serv._update_mindora_record(profile)

  assert len(profile.mindora_record["sleep.scene.amalfi_breeze"]) == 1
  assert profile.mindora_record["sleep.scene.amalfi_breeze"][0][0] == 100
  assert len(profile.mindora_record["sleep.scene.cocos_island_moonlight"]) == 1
  assert profile.mindora_record["sleep.scene.cocos_island_moonlight"][0][0] == 102


def test_profile_for_log_summarizes_behaviors():
  serv = _serv()
  profile = _profile()
  profile.behaviors["heart_rate"] = [[1, 70], [2, 72]]
  data = serv._profile_for_log(profile)
  assert data["behaviors"]["heart_rate"] == 2
  assert "sleep_scenarios_reco" in data
  assert "standard_sop_reco" in data


def test_apply_basic_update_aggregates_plays_for_new_profile():
  """回归：全新画像（profile=None 分支）也要把首包 plays 聚合进 mindora_record，
  否则 most_used_scene 永远为空、weekly_best/scene_preference 出不来。"""
  serv = _serv()
  new_profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=7300, sleep_status=[
      SleepElement(start_time=250, duration=120, sleep_type="core")])],
    behaviors={
      "plays": [
        [100, {"event": "sop_start", "cmd": "sleep.scene.cocos_island_moonlight", "duration": 300}],
        [200, {"event": "sop_start", "cmd": "sleep.scene.cocos_island_moonlight", "duration": 300}],
      ]
    },
  )
  merged = serv._apply_basic_update("u", new_profile, None)
  records = merged.mindora_record.get("sleep.scene.cocos_island_moonlight") or []
  assert len(records) == 2
  assert merged.sleep_analysis["most_used_scene"]["count"] == 2


def test_update_scene_stats_persists_recent_scene():
  """recent_scene：最近一次使用的场景落地 sleep_analysis，设备轮询 query_profile 即可展示。"""
  serv = _serv()
  profile = _profile()
  profile.mindora_record = {
    "sleep.scene.amalfi_breeze": [(100, 60), (300, 30)],
    "sleep.scene.cocos_island_moonlight": [(200, 45)],
  }
  serv._update_scene_stats(profile)
  recent = profile.sleep_analysis["recent_scene"]
  assert recent["scene_id"] == "amalfi_breeze"          # ts=300 最新
  assert recent["scene_name"] == "Amalfi Breeze"
  assert recent["used_at"] == 300
  assert recent["updated_at"] > 0


def test_update_scene_stats_recent_scene_cleared_when_no_records():
  serv = _serv()
  profile = _profile()
  profile.sleep_analysis["recent_scene"] = {"scene_id": "old"}
  profile.mindora_record = {"sleep.scene.amalfi_breeze": []}
  serv._update_scene_stats(profile)
  assert "recent_scene" not in profile.sleep_analysis


def test_apply_basic_update_recent_scene_from_plays():
  """端到端：plays 事件聚合进 mindora_record 后，recent_scene 同步落地。"""
  serv = _serv()
  new_profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=7300, sleep_status=[
      SleepElement(start_time=250, duration=120, sleep_type="core")])],
    behaviors={
      "plays": [
        [100, {"event": "sop_start", "cmd": "sleep.scene.cocos_island_moonlight", "duration": 300}],
        [200, {"event": "sop_start", "cmd": "sleep.scene.amalfi_breeze", "duration": 300}],
      ]
    },
  )
  merged = serv._apply_basic_update("u", new_profile, None)
  recent = merged.sleep_analysis["recent_scene"]
  assert recent["scene_id"] == "amalfi_breeze"
  assert recent["used_at"] == 200


def test_calc_sleep_insight_fallback_when_no_sleep_data():
  """零睡眠记录：不烧 LLM，返回模板兜底（llm_used=False），引导用户使用 Mindora。"""
  serv = _serv()
  profile = _profile()
  result = serv.calc_sleep_insight("user-1", profile)
  assert result is not None
  assert result.llm_used is False
  assert result.greeting.module_id == 0
  assert "Mindora" in result.greeting.content


def test_update_profile_persists_sleep_insight(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda uid, new, old: old.sleep_scenarios_reco)
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda uid, new, old: old.standard_sop_reco)
  monkeypatch.setattr(serv, "calc_sleep_insight", lambda uid, profile: _insight_report("Test greeting"))

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-1", new_profile, skip_sleep_scenarios_reco_update=False)

  assert succ is True
  assert saved["profile"].sleep_insight is not None
  assert saved["profile"].sleep_insight.greeting.title == "Test greeting"
  assert saved["profile"].sleep_insight.greeting.module_id == 0


def test_update_profile_skips_sleep_insight_when_skip_flag_set(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()
  existing.sleep_insight = _insight_report("Old greeting")

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda uid, new, old: old.sleep_scenarios_reco)
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda uid, new, old: old.standard_sop_reco)

  def fail_calc_sleep_insight(uid, profile):
    raise AssertionError("sleep_insight should not be regenerated when skip flag is set")

  monkeypatch.setattr(serv, "calc_sleep_insight", fail_calc_sleep_insight)

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile(
    "user-1", new_profile,
    skip_sleep_scenarios_reco_update=True,
    skip_sleep_analysis_update=True,
  )

  assert succ is True
  assert saved["profile"].sleep_insight.greeting.title == "Old greeting"


def test_skip_reco_does_not_skip_analysis(monkeypatch):
  """sleep_reco 与睡眠分析是独立逻辑：只跳过推荐时，insight 仍然生成。"""
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))

  def fail_calc_sleep_reco(uid, new_profile, old_profile):
    raise AssertionError("sleep reco should be skipped")

  monkeypatch.setattr(serv, "calc_sleep_reco", fail_calc_sleep_reco)
  monkeypatch.setattr(serv, "calc_sleep_insight", lambda uid, profile: _insight_report("Analysis still runs"))

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile(
    "user-1", new_profile,
    skip_sleep_scenarios_reco_update=True,
    skip_sleep_analysis_update=False,
  )

  assert succ is True
  # reco 保持旧值，insight 已更新
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["keep_me"]
  assert saved["profile"].sleep_insight.greeting.title == "Analysis still runs"


def test_skip_analysis_does_not_skip_reco(monkeypatch):
  """只跳过睡眠分析时，推荐仍然重算。"""
  from llm import RecommendationEngine

  serv = _serv()
  saved = {}
  existing = _profile()
  existing.sleep_insight = _insight_report("Keep insight")

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(RecommendationEngine, "generate", lambda profile: [_scenario("reco_still_runs")])
  monkeypatch.setattr(RecommendationEngine, "generate_sop_reco", lambda profile, *, uid: [_scenario("new_sop")])

  def fail_calc_sleep_insight(uid, profile):
    raise AssertionError("sleep insight should be skipped")

  monkeypatch.setattr(serv, "calc_sleep_insight", fail_calc_sleep_insight)

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile(
    "user-1", new_profile,
    skip_sleep_scenarios_reco_update=False,
    skip_sleep_analysis_update=True,
  )

  assert succ is True
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["reco_still_runs"]
  assert saved["profile"].sleep_insight.greeting.title == "Keep insight"


def test_update_profile_generates_sleep_insight_for_new_profile(monkeypatch):
  from llm import RecommendationEngine

  serv = _serv()
  saved = {}

  monkeypatch.setattr(serv, "get_profile", lambda uid: None)
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_sleep_insight", lambda uid, profile: _insight_report("New user greeting"))
  monkeypatch.setattr(RecommendationEngine, "generate", lambda profile: [_scenario("new_scenario")])
  monkeypatch.setattr(RecommendationEngine, "generate_sop_reco", lambda profile, *, uid: [_scenario("new_sop")])

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-new", new_profile, skip_sleep_scenarios_reco_update=False)

  assert succ is True
  assert saved["profile"].sleep_insight.greeting.title == "New user greeting"


def test_calc_sleep_insight_reuses_fresh_report(monkeypatch):
  serv = _serv()
  profile = _profile()
  profile.sleep_insight = _insight_report("Fresh greeting", generated_at=int(time.time()) - 100)

  def fail_llm(*args, **kwargs):
    raise AssertionError("LLM should not be called when insight report is fresh")

  monkeypatch.setattr(serv.llm, "generate_sync", fail_llm)

  result = serv.calc_sleep_insight("user-1", profile)
  assert result.greeting.title == "Fresh greeting"


def test_calc_sleep_insight_regenerates_stale_report(monkeypatch):
  serv = _serv()
  profile = _profile()
  # 有数据（夜晚早于报告生成时刻，不算新夜晚），但报告已超 7 天新鲜期 → 重算
  profile.sleep_data = [SleepResult(timestamp=int(time.time()) - 9 * 86400, sleep_quality=80.0)]
  profile.sleep_insight = _insight_report("Stale greeting", generated_at=int(time.time()) - 8 * 86400)

  def fake_generate(request_type, ctx, language, modules):
    # 润色校验器要求输出 key 集合与规则结论一一对应（见 polish_output_ok）
    return {
      c["key"]: {"title": "New greeting", "content": "New content", "evidence": [], "action": ""}
      for c in ctx.get("conclusions", [])
    }

  monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
  monkeypatch.setattr(serv.llm, "enabled", True)
  monkeypatch.setattr("analysis_content._polish_output_ok", lambda *_: True)

  result = serv.calc_sleep_insight("user-1", profile)
  assert result.greeting.title == "New greeting"
  assert result.llm_used is True


def test_calc_analysis_reports_populated(monkeypatch):
  serv = _serv()
  profile = _profile()
  profile.sleep_data = [SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)]

  captured = {}

  def fake_generate_sync(request_type, ctx, language, modules):
    captured["called"] = captured.get("called", 0) + 1
    return {"description": f"LLM text for {request_type}"}

  monkeypatch.setattr(serv.llm, "generate_sync", fake_generate_sync)
  monkeypatch.setattr(serv.llm, "enabled", True)

  reports = serv.calc_analysis_reports("user-1", profile)

  today = datetime.date.today().isoformat()
  assert reports is not None
  day_reports = reports["analysis_sleep_day"]
  assert len(day_reports) == 1
  assert day_reports[0].date == today
  assert day_reports[0].llm_used is False
  assert day_reports[0].modules["sleep_scenarios"]["description"] == "No sleep-scene usage was recorded last night."
  assert captured["called"] == 4  # 日级文案由规则模板生成，其余 4 个能力可润色


def test_calc_analysis_reports_skips_current_period(monkeypatch):
  serv = _serv()
  profile = _profile()
  profile.sleep_data = [SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)]
  today = datetime.date.today().isoformat()
  profile.analysis_reports["analysis_sleep_day"] = [
    AnalysisTextReport(
      request_type="analysis_sleep_day",
      date=today,
      generated_at=int(time.time()) - 100,
      modules={"description": "Cached day text"},
    )
  ]
  from analysis_content import explore_input_fingerprint
  profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")

  calls = []

  def fake_generate_sync(request_type, ctx, language, modules):
    calls.append(request_type)
    return {"description": "new"}

  monkeypatch.setattr(serv.llm, "generate_sync", fake_generate_sync)
  monkeypatch.setattr(serv.llm, "enabled", True)

  reports = serv.calc_analysis_reports("user-1", profile)
  # 今日已有报告 → 该能力不再调 LLM，其余 4 个正常生成
  assert "analysis_sleep_day" not in calls
  assert len(calls) == 4
  day = reports["analysis_sleep_day"][0]
  assert day.modules["description"] == "Cached day text"


def test_analysis_report_upsert_retention():
  serv = _serv()
  reports = []
  base = datetime.date(2026, 1, 1)
  for i in range(35):
    r = AnalysisTextReport(
      request_type="analysis_sleep_day",
      date=(base + datetime.timedelta(days=i)).isoformat(),
      generated_at=1000 + i,
      modules={},
    )
    reports = UserProfileServ._upsert_analysis_report(reports, r, 30)
  # 保留最近 30 条，最早的被裁掉
  assert len(reports) == 30
  assert reports[0].date == "2026-01-06"
  assert reports[-1].date == "2026-02-04"

  # 同周期 upsert 是替换而非追加
  dup = AnalysisTextReport(
    request_type="analysis_sleep_day",
    date="2026-02-04",
    generated_at=9999,
    modules={"description": "replaced"},
  )
  reports = UserProfileServ._upsert_analysis_report(reports, dup, 30)
  assert len(reports) == 30
  assert reports[-1].modules["description"] == "replaced"


def test_profile_for_log_compresses_large_fields():
  serv = _serv()
  profile = _profile()
  profile.sleep_scenarios_reco = [
    SleepScenario.model_validate({
      "scenario_id": "s1",
      "scenario_name": "Scene One",
      "stages": [{"cmd_name": "s1", "audio_file": "a.mp3", "guide_file": "g.mp3", "light_scene": "l", "aroma_mode": "m"}],
    })
  ]
  profile.standard_sop_reco = [
    SleepScenario.model_validate({
      "scenario_id": "std1",
      "scenario_name": "Standard One",
      "stages": [{"cmd_name": "std1"}],
    })
  ]
  profile.mindora_record = {"sleep.scene.cocos_island_moonlight": [[1, 60], [2, 90]]}
  profile.sleep_data = [SleepResult(timestamp=1, sleep_quality=80.0)]

  data = serv._profile_for_log(profile)

  # Large nested structures should be summarized, not dumped in full.
  assert data["sleep_scenarios_reco"] == [{"scenario_id": "s1", "scenario_name": "Scene One"}]
  assert data["standard_sop_reco"] == [{"scenario_id": "std1", "scenario_name": "Standard One"}]
  assert data["mindora_record"] == {"sleep.scene.cocos_island_moonlight": 2}
  assert data["sleep_data"] == 1


def test_update_mindora_record_stores_lightweight_entries():
  serv = _serv()
  profile = _profile()
  profile.mindora_record = {"sleep.scene.amalfi_breeze": []}
  profile.sleep_data = [SleepResult(timestamp=7300, sleep_status=[
    SleepElement(start_time=250, duration=120, sleep_type="core")])]

  new_profile = UserProfile(
    behaviors={
      "plays": [
        [100, {"event": "sop_start", "cmd": "sleep.scene.amalfi_breeze", "duration": 600, "extra": "x" * 1000}],
      ]
    }
  )

  profile.behaviors["plays"] = new_profile.behaviors["plays"]
  serv._update_mindora_record(profile)

  assert len(profile.mindora_record["sleep.scene.amalfi_breeze"]) == 1
  ts, duration = profile.mindora_record["sleep.scene.amalfi_breeze"][0]
  assert ts == 100
  assert duration == 600
  # Full event dict should NOT be stored.
  assert not isinstance(duration, dict)


def test_short_sop_play_stays_in_behaviors_without_counting_as_usage():
  serv = _serv()
  incoming = UserProfile(uid_emb=[], behaviors={"plays": [
    [100, {"event": "sop_start", "cmd": "sleep.scene.amalfi_breeze", "duration": 299}],
    [200, {"event": "sop_start", "cmd": "sleep.scene.amalfi_breeze", "duration": 300}],
  ]}, sleep_data=[SleepResult(timestamp=7300, sleep_status=[
    SleepElement(start_time=250, duration=120, sleep_type="core")])])
  merged = serv._apply_basic_update("u", incoming, None)
  assert len(merged.behaviors["plays"]) == 2
  assert merged.mindora_record["sleep.scene.amalfi_breeze"] == [(200, 300)]
  assert merged.sleep_analysis["most_used_scene"]["count"] == 1


def test_update_scene_stats_persists_most_used_scene():
  serv = _serv()
  profile = _profile()
  profile.mindora_record = {
    "sleep.scene.amalfi_breeze": [[1, 600], [2, 300]],
    "sleep.scene.cocos_island_moonlight": [[3, 100]],
  }

  serv._update_scene_stats(profile)

  most_used = profile.sleep_analysis["most_used_scene"]
  assert most_used["scene_id"] == "amalfi_breeze"
  assert most_used["scene_name"] == "Amalfi Breeze"
  assert most_used["count"] == 2
  assert most_used["total_duration"] == 900
  assert "updated_at" in most_used


def test_update_scene_stats_clears_when_no_records():
  serv = _serv()
  profile = _profile()
  profile.sleep_analysis["most_used_scene"] = {"scene_id": "old"}
  profile.mindora_record = {"sleep.scene.cocos_island_moonlight": []}

  serv._update_scene_stats(profile)

  assert "most_used_scene" not in profile.sleep_analysis


def test_update_profile_preserves_behaviors_on_empty_update(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()
  existing.behaviors["heart_rate"] = [[1, 70]]

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda uid, new, old: old.sleep_scenarios_reco)
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda uid, new, old: old.standard_sop_reco)

  # Empty behavior update should not wipe existing heart_rate.
  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile("user-1", new_profile, skip_sleep_scenarios_reco_update=True)

  assert succ is True
  assert saved["profile"].behaviors["heart_rate"] == [[1, 70]]


def test_build_overview_uses_precomputed_most_used_scene():
  """新鲜度门后：weekly_best 读路径现算 7 天时间窗（mindora_record），
  不再回退持久化的全时段快照——窗内无记录时整卡省略。"""
  server = UserServer.__new__(UserServer)
  profile = _profile()
  profile.sleep_analysis["most_used_scene"] = {
    "scene_id": "kyoto_forest",
    "scene_name": "Kyoto Forest",
    "count": 7,
  }

  class _FakeData:
    date = "2026-06-26"
    modules = ["weekly_best"]
    language = "en"

  result = server._build_overview(_FakeData(), profile)

  assert "weekly_best" not in result


def test_build_overview_weekly_best_from_window_records():
  """7 天时间窗内有场景记录 → weekly_best 现算返回。"""
  server = UserServer.__new__(UserServer)
  profile = _profile()
  now = int(time.time())
  profile.mindora_record = {
    "sleep.scene.kyoto_forest": [(now - i * 86400, 60) for i in range(7)],
  }

  class _FakeData:
    date = "2026-06-26"
    modules = ["weekly_best"]
    language = "en"

  result = server._build_overview(_FakeData(), profile)

  assert result["weekly_best"]["audio_name"] == "Kyoto Forest"
  assert result["weekly_best"]["used_times"] == 7



def test_update_profile_basic_does_not_call_llm(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))

  def fail_llm(name):
    def _fail(*args, **kwargs):
      raise AssertionError(f"{name} should not be called by update_profile_basic")
    return _fail

  monkeypatch.setattr(serv, "calc_sleep_reco", fail_llm("calc_sleep_reco"))
  monkeypatch.setattr(serv, "calc_standard_sop_reco", fail_llm("calc_standard_sop_reco"))
  monkeypatch.setattr(serv, "calc_sleep_insight", fail_llm("calc_sleep_insight"))
  monkeypatch.setattr(serv, "calc_analysis_reports", fail_llm("calc_analysis_reports"))

  new_profile = UserProfile(
    uid_emb=[],
    long_term_profile=[],
    behaviors={"heart_rate": [(1, 70)], "sleep_status": [], "clicks": [], "plays": []},
    mindora_record={"sleep.scene.cocos_island_moonlight": []},
  )

  succ = serv.update_profile_basic("user-1", new_profile)

  assert succ is True
  assert saved["uid"] == "user-1"
  assert saved["profile"].behaviors["heart_rate"] == [(1, 70)]


def test_update_profile_llm_applies_results(monkeypatch):
  serv = _serv()
  saved = {}
  existing = _profile()

  monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
  monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update({"uid": uid, "profile": profile}))
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda uid, new, old: [_scenario("fresh_sleep")])
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda uid, new, old: [_scenario("fresh_sop")])

  monkeypatch.setattr(serv, "calc_sleep_insight", lambda uid, profile: _insight_report("LLM greeting"))
  monkeypatch.setattr(serv, "calc_analysis_reports", lambda uid, profile: {"analysis_sleep_day": []})

  succ = serv.update_profile_llm("user-1")

  assert succ is True
  assert saved["uid"] == "user-1"
  assert [item.scenario_name for item in saved["profile"].sleep_scenarios_reco] == ["fresh_sleep"]
  assert [item.scenario_name for item in saved["profile"].standard_sop_reco] == ["fresh_sop"]
  assert saved["profile"].sleep_insight.greeting.title == "LLM greeting"
  assert saved["profile"].analysis_reports == {"analysis_sleep_day": []}


def test_update_profile_llm_returns_false_when_profile_missing(monkeypatch):
  serv = _serv()
  monkeypatch.setattr(serv, "get_profile", lambda uid: None)

  succ = serv.update_profile_llm("user-missing")

  assert succ is False


async def test_schedule_llm_update_if_needed_respects_cooldown(monkeypatch):
  server = UserServer.__new__(UserServer)
  server._llm_tracker_lock = asyncio.Lock()
  server._llm_update_tracker = {}
  server._llm_tasks = set()
  server._llm_pending = {}
  server._llm_last_attempt = {}
  server._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)

  created_tasks = []
  original_create_task = asyncio.create_task

  def fake_create_task(coro):
    task = original_create_task(coro)
    created_tasks.append(task)
    return task

  monkeypatch.setattr(asyncio, "create_task", fake_create_task)

  await server._schedule_llm_update_if_needed("u1")
  await server._schedule_llm_update_if_needed("u1")

  assert len(created_tasks) == 1
  created_tasks[0].cancel()
  try:
    await created_tasks[0]
  except asyncio.CancelledError:
    pass


async def test_handle_update_profile_returns_quickly(monkeypatch):
  server = UserServer.__new__(UserServer)
  server.server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
  server._llm_tracker_lock = asyncio.Lock()
  server._llm_update_tracker = {}
  server._llm_tasks = set()
  server._llm_pending = {}
  server._llm_last_attempt = {}
  server._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)
  server.debug_uid_set = {"test_debug_user_001"}

  class FakeUserServ:
    def update_profile_basic(self, uid, new_profile, health_schema_version=None, timezone=None, language=None):
      return True

    def update_profile_llm(self, uid, skip_reco=False, skip_analysis=False):
      # This should run in background, not block the response.
      time.sleep(5)
      return True

    def get_profile(self, uid):
      return None  # 响应里 data 允许为 None（见同步约定 §3 的兜底）

  server.user_serv = FakeUserServ()

  request = ProfileRequest(
    request_type="update_profile",
    timestamp=int(time.time()),
    version="1.0",
    data=ProfileData(
      uid="test_debug_user_001",
      skip_sleep_scenarios_reco_update=False,
    ),
  )

  start = time.monotonic()
  response = await server.handle_update_profile(request)
  elapsed = time.monotonic() - start

  assert response.code == 0
  assert elapsed < 1.0

  # Clean up any background task that was scheduled.
  for task in list(server._llm_tasks):
    task.cancel()
    try:
      await task
    except asyncio.CancelledError:
      pass


# ── 个人资料同步约定（个人资料同步约定.md）────────────────────────

def _addr(addr_id: str) -> Address:
  return Address(id=addr_id, is_default=False, region="上海市 普陀区",
                 detail="中海中心A座1501室", name="张三", phone="13866997600")


def test_merge_personal_profile_null_keeps_empty_clears():
  """null=保持原值，""=置空，有内容=覆盖。"""
  old = Profile(nickname="DK", phone="13866997600", birthday="1998.08.12", gender="Male")
  new = Profile.model_validate({"nickname": "www", "phone": None, "birthday": ""})
  merged = UserProfileServ._merge_personal_profile(old, new)
  assert merged.nickname == "www"          # 覆盖
  assert merged.phone == "13866997600"     # 显式 null → 保持
  assert merged.birthday == ""             # "" → 置空
  assert merged.gender == "Male"           # 键未出现 → 保持


def test_merge_personal_profile_address_list_whole_replace():
  old = Profile(address_list=[_addr("a1"), _addr("a2")])
  # 键出现 → 整体替换
  merged = UserProfileServ._merge_personal_profile(old, Profile.model_validate({"address_list": [_addr("a3").model_dump()]}))
  assert [a.id for a in merged.address_list] == ["a3"]
  # 空数组 → 清空地址（不影响其他字段）
  merged = UserProfileServ._merge_personal_profile(old, Profile.model_validate({"address_list": []}))
  assert merged.address_list == []
  # 键不出现 → 保持
  merged = UserProfileServ._merge_personal_profile(old, Profile.model_validate({"nickname": "x"}))
  assert [a.id for a in merged.address_list] == ["a1", "a2"]


def test_merge_personal_profile_avatar_absent_kept():
  old = Profile(avatar_base64="abc123", avatar_mime_type="image/png")
  merged = UserProfileServ._merge_personal_profile(old, Profile.model_validate({"nickname": "x"}))
  assert merged.avatar_base64 == "abc123"
  assert merged.avatar_mime_type == "image/png"


def test_merge_personal_profile_new_profile_normalizes_null():
  """新建画像：显式 null 落在字段默认值上，不落 None。"""
  merged = UserProfileServ._merge_personal_profile(
    None, Profile.model_validate({"nickname": "DK", "phone": None}))
  assert merged.nickname == "DK"
  assert merged.phone == ""                    # 默认值
  assert merged.avatar_mime_type == "image/jpeg"


def test_merge_personal_profile_new_is_none_keeps_old():
  old = Profile(nickname="DK")
  assert UserProfileServ._merge_personal_profile(old, None) is old


def test_apply_basic_update_merges_personal_profile_for_existing():
  """回归：已有画像的个人资料更新此前被静默丢弃（_apply_basic_update 从不合并 profile 容器）。"""
  serv = _serv()
  existing = _profile()
  existing.profile = Profile(nickname="DK", phone="13866997600")
  new_profile = UserProfile(
    uid_emb=[], long_term_profile=[],
    profile=Profile.model_validate({"nickname": "www"}),
  )
  merged = serv._apply_basic_update("u", new_profile, existing)
  assert merged.profile.nickname == "www"      # 更新生效
  assert merged.profile.phone == "13866997600"  # 未提交字段不被抹掉


async def test_handle_update_profile_returns_uid_msg_and_merged_profile():
  """同步约定 §2/§3：msg 回显 uid 而非 timestamp；data 返回合并后的完整 profile。"""
  server = UserServer.__new__(UserServer)
  server.server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
  server._llm_tracker_lock = asyncio.Lock()
  server._llm_update_tracker = {}
  server._llm_tasks = set()
  server._llm_pending = {}
  server._llm_last_attempt = {}
  server._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)
  server.debug_uid_set = {"test_debug_user_001"}

  merged_profile = UserProfile(uid_emb=[], long_term_profile=[],
                               profile=Profile(nickname="www"))

  class FakeUserServ:
    def update_profile_basic(self, uid, new_profile, health_schema_version=None, timezone=None, language=None):
      return True

    def get_profile(self, uid):
      return merged_profile

  server.user_serv = FakeUserServ()

  ts = int(time.time())
  request = ProfileRequest(
    request_type="update_profile",
    timestamp=ts,
    version="1.0",
    data=ProfileData(uid="test_debug_user_001"),
  )
  response = await server.handle_update_profile(request)
  assert response.code == 0
  assert "test_debug_user_001" in response.msg
  assert str(ts) not in response.msg
  assert response.data["user_profile"]["profile"]["nickname"] == "www"
