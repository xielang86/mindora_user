"""截断一律保留最新：每个会截尾的序列都验一遍「乱序输入 → 留下的是最新的」。

唯一的例外是单条 SleepResult 内嵌序列（sleep_status / night_events 等），那里保留的是
序列开头——一晚之内「最新」没有意义，入睡段（onset / bed_time）才是必须留住的，
见 TestInNightSequencesKeepHead。
"""
import threading

import pytest

from user_profile import (
  AnalysisTextReport, FootprintDay, InboxMessage, PopupState, ProfileData,
  ProfileRequest, SleepElement, SleepResult, SurveySubmission, UserProfile,
  keep_latest_engagement,
)
from analysis_content import AnalysisContentService
from profile_service import UserProfileServ
from user_server import UserServer
import profile_query_gate


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
  monkeypatch.setattr(profile_query_gate, "is_query_allowed", lambda uid: True)


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  serv.storage_mode = "txt_json"
  serv.text_profiles = {}
  serv._flush_text_profiles_unlocked = lambda: None
  return serv


def _query(profile, **data_kw):
  srv = UserServer.__new__(UserServer)
  srv._parse_for_uid = lambda d: "u1"

  class _F:
    get_profile = staticmethod(lambda uid: profile)
    _profile_for_log = staticmethod(lambda p: "{}")

  srv.user_serv = _F()
  req = ProfileRequest(
    request_type="query_profile", timestamp=1,
    data=ProfileData(uid="u1", jwt_token="t", device_type="pc", **data_kw),
  )
  return UserServer.handle_query_profile(srv, req).data["user_profile"]


# 乱序的三晚，最新的那晚故意放在数组中间
_SHUFFLED = [1000, 3000, 2000]


class TestSleepDataKeepsLatest:
  def _shuffled_profile(self) -> UserProfile:
    return UserProfile(sleep_data=[
      SleepResult(timestamp=ts, sleep_quality=float(ts)) for ts in _SHUFFLED
    ])

  def test_save_profile_normalizes_order(self):
    serv = _serv()
    profile = self._shuffled_profile()
    serv.save_profile("u1", profile)
    assert [r.timestamp for r in profile.sleep_data] == [1000, 2000, 3000]

  def test_first_update_normalizes_order(self, monkeypatch):
    """首包路径（库里无画像）也要归一，否则读路径 [-N:] 取错夜。"""
    serv = _serv()
    saved = {}
    monkeypatch.setattr(serv, "get_profile", lambda uid: None)
    monkeypatch.setattr(serv, "save_profile", lambda uid, p: saved.update(profile=p))
    assert serv.update_profile_basic("u1", self._shuffled_profile())
    assert [r.timestamp for r in saved["profile"].sleep_data] == [1000, 2000, 3000]

  def test_first_update_dedups_same_timestamp(self):
    dup = UserProfile(sleep_data=[
      SleepResult(timestamp=1000, sleep_quality=1.0),
      SleepResult(timestamp=1000, sleep_quality=2.0),
    ])
    clean = UserProfileServ._new_profile_from_client(dup)
    assert len(clean.sleep_data) == 1
    assert clean.sleep_data[0].sleep_quality == 2.0  # 后来者覆盖

  def test_merge_keeps_newest_30(self):
    old = [SleepResult(timestamp=i, sleep_quality=1.0) for i in range(1, 41)]
    merged = UserProfileServ._merge_sleep_data(old, [SleepResult(timestamp=100)])
    assert len(merged) == UserProfileServ.MAX_SLEEP_DATA_LEN
    assert merged[-1].timestamp == 100
    assert merged[0].timestamp == 12  # 最旧的被挤掉

  def test_response_returns_latest_even_if_stored_unsorted(self):
    """存量行（上线前写入）可能仍是乱序，响应侧必须自己排。"""
    body = _query(self._shuffled_profile())
    assert [r["timestamp"] for r in body["sleep_data"]] == [3000]

  def test_response_n_returns_latest_n_in_order(self):
    body = _query(self._shuffled_profile(), sleep_data_count=2)
    assert [r["timestamp"] for r in body["sleep_data"]] == [2000, 3000]


class TestAnalysisReportsKeepLatest:
  def _report(self, date: str, generated_at: int) -> AnalysisTextReport:
    return AnalysisTextReport(
      request_type="analysis_sleep_day", date=date, generated_at=generated_at)

  def test_upsert_keeps_latest(self):
    kept = []
    for d in ("2026-09-01", "2026-09-03", "2026-09-02"):
      kept = AnalysisContentService._upsert_analysis_report(
        kept, self._report(d, 1), retention=2)
    assert [r.date for r in kept] == ["2026-09-02", "2026-09-03"]

  def test_response_returns_latest_even_if_stored_unsorted(self):
    profile = UserProfile()
    profile.analysis_reports = {"analysis_sleep_day": [
      self._report("2026-09-01", 1), self._report("2026-09-03", 3),
      self._report("2026-09-02", 2),
    ]}
    body = _query(profile)
    assert [r["date"] for r in body["analysis_reports"]["analysis_sleep_day"]] == ["2026-09-03"]


class TestEngagementKeepsLatest:
  def test_all_four_keep_newest(self):
    shuffled = [1, 3, 2]
    assert list(keep_latest_engagement("footprint_days", {
      f"2026-09-0{i}": FootprintDay(date=f"2026-09-0{i}") for i in shuffled}, 1)) == ["2026-09-03"]
    assert [m.message_id for m in keep_latest_engagement("inbox_messages", [
      InboxMessage(message_id=f"m{i}", popup_id="p", title="t", created_at=i)
      for i in shuffled], 1)] == ["m3"]
    assert list(keep_latest_engagement("survey_submissions", {
      f"s{i}": SurveySubmission(submission_id=f"sub{i}", survey_id=f"s{i}", submitted_at=i)
      for i in shuffled}, 1)) == ["s3"]
    assert list(keep_latest_engagement("popup_states", {
      f"p{i}": PopupState(last_impression_at=i) for i in shuffled}, 1)) == ["p3"]

  def test_response_keeps_newest(self):
    profile = UserProfile(inbox_messages=[
      InboxMessage(message_id=f"m{i}", popup_id=f"p{i}", title="t", created_at=i)
      for i in (1, 3, 2)
    ])
    body = _query(profile)
    assert [m["message_id"] for m in body["inbox_messages"]] == ["m3"]


class TestBehaviorAndMindoraRecordKeepLatest:
  def test_behaviors_keep_newest(self):
    serv = _serv()
    cap = UserProfileServ.MAX_BEHAVIOR_LEN
    old = {"clicks": [(i, i) for i in range(cap)]}
    merged = serv._merge_behavior(old, {"clicks": [(9999, 1), (9998, 1)]})
    assert merged["clicks"][-1][0] == 9999
    assert len(merged["clicks"]) == cap

  def test_mindora_record_keeps_newest(self):
    serv = _serv()
    cmd = "sleep.scene.kyoto_forest"
    cap = UserProfileServ.MAX_BEHAVIOR_LEN
    from user_profile import SleepElement, SleepResult
    profile = UserProfile(mindora_record={cmd: [(i, None) for i in range(cap)]},
                          sleep_data=[SleepResult(timestamp=15000, sleep_status=[
                            SleepElement(start_time=10000, duration=60, sleep_type="core")])])
    incoming = UserProfile(behaviors={"plays": [
      (9999, {"cmd": cmd, "event": "sop_start", "duration": 300}),
    ]})
    profile.behaviors["plays"] = incoming.behaviors["plays"]
    serv._update_mindora_record(profile)
    assert len(profile.mindora_record[cmd]) == cap
    assert profile.mindora_record[cmd][-1][0] == 9999


class TestInNightSequencesKeepHead:
  """刻意的例外：一晚之内保留序列开头（入睡段），不是结尾。"""

  def test_sleep_status_keeps_head(self):
    cap = UserProfileServ.MAX_SLEEP_RECORD_LIST_LEN["sleep_status"]
    record = SleepResult(timestamp=1, sleep_status=[
      SleepElement(start_time=i, duration=1.0, sleep_type="core") for i in range(cap + 5)
    ])
    profile = UserProfile(sleep_data=[record])
    UserProfileServ._cap_sleep_record_lists(profile)
    kept = profile.sleep_data[0].sleep_status
    assert kept[0].start_time == 0           # 入睡段保住
    assert kept[-1].start_time == cap - 1    # 丢的是醒来段
