"""query_profile 的 sleep_data 限量返回（sleep_data_count）测试。

语义：0=不携带 sleep_data；缺省 1=只回最近一晚；N=最近 N 晚；
旧布尔开关 include_sleep_data 由 before-validator 兼容映射（false→0 / true→全部）。
返回的每条 sleep_data 附计算属性 sequence_summaries；
同一数量同时裁剪 footprint_days / inbox_messages / survey_submissions（保留最新 N 条）。
"""
import pytest

from user_profile import (
  FootprintDay, InboxMessage, ProfileData, ProfileRequest,
  SleepElement, SleepResult, SurveySubmission, UserProfile,
)
from user_server import UserServer
import profile_query_gate


def _profile(nights: int = 3, with_extras: bool = False) -> UserProfile:
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=1000 * (i + 1), sleep_quality=80.0) for i in range(nights)],
  )
  if with_extras:
    profile.footprint_days = {
      f"2026-09-0{i + 1}": FootprintDay(date=f"2026-09-0{i + 1}", app_active=True)
      for i in range(3)
    }
    profile.inbox_messages = [
      InboxMessage(message_id=f"m{i}", popup_id=f"p{i}", title=f"t{i}", created_at=100 * (i + 1))
      for i in range(3)
    ]
    profile.survey_submissions = {
      f"s{i}": SurveySubmission(submission_id=f"sub{i}", survey_id=f"s{i}", submitted_at=10 * (i + 1))
      for i in range(3)
    }
  return profile


def _server(profile):
  srv = UserServer.__new__(UserServer)
  srv._parse_for_uid = lambda data: "u1" if data and data.uid else None

  class _FakeServ:
    @staticmethod
    def get_profile(uid):
      return profile

    @staticmethod
    def _profile_for_log(p):
      return "{}"

  srv.user_serv = _FakeServ()
  return srv


def _request(**data_kw):
  data_kw.setdefault("device_type", "pc")
  return ProfileRequest(
    request_type="query_profile", timestamp=1,
    data=ProfileData(uid="u1", jwt_token="tok", **data_kw),
  )


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
  monkeypatch.setattr(profile_query_gate, "is_query_allowed", lambda uid: True)


def _sleep_data_of(resp):
  return (resp.data or {}).get("user_profile", {}).get("sleep_data")


class TestSleepDataCount:
  def test_zero_omits_sleep_data(self):
    resp = UserServer.handle_query_profile(_server(_profile()), _request(sleep_data_count=0))
    assert resp.code == 0
    assert "sleep_data" not in resp.data["user_profile"]

  def test_default_returns_latest_one(self):
    """不带字段 → 默认 1：只回最近一晚（timestamp 最大的那条）。"""
    resp = UserServer.handle_query_profile(_server(_profile()), _request())
    sd = _sleep_data_of(resp)
    assert len(sd) == 1
    assert sd[0]["timestamp"] == 3000

  def test_n_returns_latest_n_ascending(self):
    """最近 N 晚：升序与存库/快照一致（[-1] 最新），截尾保留最新。"""
    resp = UserServer.handle_query_profile(_server(_profile()), _request(sleep_data_count=2))
    sd = _sleep_data_of(resp)
    assert [r["timestamp"] for r in sd] == [2000, 3000]

  def test_n_larger_than_stock_returns_all(self):
    resp = UserServer.handle_query_profile(_server(_profile(nights=2)), _request(sleep_data_count=30))
    assert len(_sleep_data_of(resp)) == 2

  def test_other_fields_untouched(self):
    resp = UserServer.handle_query_profile(_server(_profile()), _request(sleep_data_count=0))
    up = resp.data["user_profile"]
    assert "behaviors" in up and "analysis_reports" in up


class TestLegacyIncludeSleepData:
  def test_legacy_false_maps_to_zero(self):
    resp = UserServer.handle_query_profile(_server(_profile()), _request(include_sleep_data=False))
    assert "sleep_data" not in resp.data["user_profile"]

  def test_legacy_true_maps_to_all(self):
    resp = UserServer.handle_query_profile(_server(_profile()), _request(include_sleep_data=True))
    assert len(_sleep_data_of(resp)) == 3

  def test_new_field_wins_over_legacy(self):
    """两个都带时以新字段 sleep_data_count 为准。"""
    resp = UserServer.handle_query_profile(
      _server(_profile()), _request(include_sleep_data=True, sleep_data_count=1))
    assert len(_sleep_data_of(resp)) == 1

  def test_negative_rejected(self):
    with pytest.raises(Exception):
      _request(sleep_data_count=-1)


class TestSequenceSummaries:
  def _profile_with_stages(self) -> UserProfile:
    return UserProfile(
      uid_emb=[],
      sleep_data=[SleepResult(
        timestamp=3000,
        sleep_status=[
          SleepElement(start_time=100, duration=30.0, sleep_type="core"),
          SleepElement(start_time=200, duration=20.0, sleep_type="deep"),
          SleepElement(start_time=300, duration=40.0, sleep_type="rem"),
          SleepElement(start_time=400, duration=5.0, sleep_type="awake"),
        ],
      )],
    )

  def test_sequence_summaries_present(self):
    """每条返回的 sleep_data 携带 sequence_summaries（property 现算，不进库存储）。"""
    resp = UserServer.handle_query_profile(_server(self._profile_with_stages()), _request())
    sd = _sleep_data_of(resp)
    assert len(sd) == 1
    summ = sd[0].get("sequence_summaries")
    assert summ is not None
    assert summ["rem_sleep_duration"] == 40.0
    assert summ["core_sleep_duration"] == 30.0
    assert summ["deep_sleep_duration"] == 20.0
    assert summ["night_awake_count"] == 1
    assert summ["night_awake_duration"] == 5.0
    assert summ["time_in_bed"] == 95.0
    assert summ["night_awake_type"] == "Brief awakening"

  def test_sleep_status_opt_out_keeps_summary_and_onset(self):
    resp = UserServer.handle_query_profile(
      _server(self._profile_with_stages()), _request(include_sleep_status=False))
    record = _sleep_data_of(resp)[0]
    assert "sleep_status" not in record
    assert record["sequence_summaries"]["deep_sleep_duration"] == 20.0
    assert "onset" in record

  def test_default_still_returns_sleep_status(self):
    resp = UserServer.handle_query_profile(_server(self._profile_with_stages()), _request())
    assert len(_sleep_data_of(resp)[0]["sleep_status"]) == 4

  def test_sequence_summaries_empty_stages(self):
    """无 sleep_status 时也要带 sequence_summaries（全 0 / None），schema 保持一致。"""
    resp = UserServer.handle_query_profile(_server(_profile(nights=1)), _request())
    summ = _sleep_data_of(resp)[0].get("sequence_summaries")
    assert summ is not None
    assert summ["time_in_bed"] == 0
    assert summ["night_awake_count"] == 0
    assert summ["night_awake_type"] is None


class TestExtrasTrimmedByEngagementCount:
  """footprint_days / inbox_messages / popup_states / survey_submissions 由
  engagement_count 独立控制（不再跟随 sleep_data_count），缺省各回最新 1 条。"""

  def test_default_returns_latest_one_each(self):
    resp = UserServer.handle_query_profile(_server(_profile(with_extras=True)), _request())
    up = resp.data["user_profile"]
    assert list(up["footprint_days"].keys()) == ["2026-09-03"]
    assert [m["message_id"] for m in up["inbox_messages"]] == ["m2"]
    assert list(up["survey_submissions"].keys()) == ["s2"]

  def test_n_returns_latest_n_each(self):
    resp = UserServer.handle_query_profile(
      _server(_profile(with_extras=True)), _request(engagement_count=2))
    up = resp.data["user_profile"]
    assert list(up["footprint_days"].keys()) == ["2026-09-02", "2026-09-03"]
    assert [m["message_id"] for m in up["inbox_messages"]] == ["m1", "m2"]
    assert list(up["survey_submissions"].keys()) == ["s1", "s2"]

  def test_zero_omits_extras(self):
    resp = UserServer.handle_query_profile(
      _server(_profile(with_extras=True)), _request(engagement_count=0))
    up = resp.data["user_profile"]
    assert "footprint_days" not in up
    assert "inbox_messages" not in up
    assert "popup_states" not in up
    assert "survey_submissions" not in up

  def test_n_larger_than_stock_returns_all(self):
    resp = UserServer.handle_query_profile(
      _server(_profile(nights=1, with_extras=True)), _request(engagement_count=30))
    up = resp.data["user_profile"]
    assert len(up["footprint_days"]) == 3
    assert len(up["inbox_messages"]) == 3
    assert len(up["survey_submissions"]) == 3

  def test_sleep_data_count_no_longer_trims_extras(self):
    """sleep_data_count=0 只影响 sleep_data，四件套仍按 engagement_count 缺省回 1 条。"""
    resp = UserServer.handle_query_profile(
      _server(_profile(with_extras=True)), _request(sleep_data_count=0))
    up = resp.data["user_profile"]
    assert "sleep_data" not in up
    assert list(up["footprint_days"].keys()) == ["2026-09-03"]
    assert len(up["inbox_messages"]) == 1
