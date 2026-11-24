"""update_profile 响应体契约：注入 sequence_summaries；裁剪只认客户端显式开关。

默认全量是刻意的：这份快照是客户端的本地基准（个人资料同步约定 §2/§3），
默认裁剪会被老客户端当成「服务端没有这些数据」而抹掉本地 sleep_data / behaviors 历史。
"""
import asyncio

import pytest

from user_profile import (
  FootprintDay, ProfileData, ProfileRequest, SleepElement, SleepResult, UserProfile,
)
from user_server import UserServer


def _profile(nights: int = 3) -> UserProfile:
  return UserProfile(
    uid_emb=[],
    behaviors={"plays": [(1, {"cmd": "sleep.scene.kyoto_forest", "event": "sop_start"})]},
    mindora_record={"sleep.scene.kyoto_forest": [(1, 60)]},
    sleep_data=[
      SleepResult(
        timestamp=1000 * (i + 1),
        sleep_quality=80.0,
        sleep_status=[
          SleepElement(start_time=0, duration=60.0, sleep_type="core"),
          SleepElement(start_time=3600, duration=30.0, sleep_type="deep"),
          SleepElement(start_time=5400, duration=10.0, sleep_type="awake"),
        ],
      )
      for i in range(nights)
    ],
  )


def _server(profile):
  srv = UserServer.__new__(UserServer)
  srv._parse_for_uid = lambda data: "u1" if data and data.uid else None
  srv._update_summary_for_log = lambda up: "{}"
  srv.server_semaphore = asyncio.Semaphore(1)
  srv._mark_activity = lambda uid: None

  async def _noop(*a, **kw):
    return None

  srv._maybe_schedule_llm_update = _noop

  class _FakeServ:
    @staticmethod
    def update_profile_basic(*a, **kw):
      return True

    @staticmethod
    def get_profile(uid):
      return profile

  srv.user_serv = _FakeServ()
  return srv


def _update(profile, **data_kw):
  req = ProfileRequest(
    request_type="update_profile", timestamp=1,
    data=ProfileData(uid="u1", jwt_token="tok", user_profile=UserProfile(), **data_kw),
  )
  resp = asyncio.run(UserServer.handle_update_profile(_server(profile), req))
  assert resp.code == 0
  return (resp.data or {}).get("user_profile", {})


class TestSequenceSummaries:
  def test_injected_into_update_response(self):
    body = _update(_profile())
    summ = body["sleep_data"][0]["sequence_summaries"]
    assert summ["time_in_bed"] == 100.0
    assert summ["deep_sleep_duration"] == 30.0
    assert summ["night_awake_count"] == 1


class TestTrimmingIsOptIn:
  def test_default_response_is_full(self):
    """老客户端不传任何开关 → 全量返回，不能悄悄裁成 1 晚。"""
    body = _update(_profile(nights=3))
    assert len(body["sleep_data"]) == 3
    assert "behaviors" in body
    assert "mindora_record" in body
    assert "analysis_reports" in body

  def test_explicit_switches_are_honored(self):
    body = _update(_profile(nights=3), sleep_data_count=1,
                   include_behaviors=False, include_mindora_record=False)
    assert len(body["sleep_data"]) == 1
    assert "behaviors" not in body
    assert "mindora_record" not in body

  def test_engagement_count_also_opt_in(self):
    """四件套同理：不显式传就全量返回，传了才裁。"""
    profile = _profile()
    profile.footprint_days = {
      f"2026-09-0{i + 1}": FootprintDay(date=f"2026-09-0{i + 1}", app_active=True)
      for i in range(3)
    }
    assert len(_update(profile)["footprint_days"]) == 3
    assert len(_update(profile, engagement_count=1)["footprint_days"]) == 1

  def test_explicit_zero_omits_sleep_data(self):
    body = _update(_profile(nights=3), sleep_data_count=0)
    assert "sleep_data" not in body

  def test_explicit_default_value_still_counts_as_explicit(self):
    """显式传 sleep_data_count=1（等于缺省值）也要生效——按 model_fields_set 判定。"""
    body = _update(_profile(nights=3), sleep_data_count=1)
    assert len(body["sleep_data"]) == 1


class TestQueryProfileUnchanged:
  def test_query_profile_still_applies_defaults(self, monkeypatch):
    import profile_query_gate
    monkeypatch.setattr(profile_query_gate, "is_query_allowed", lambda uid: True)
    profile = _profile(nights=3)
    srv = _server(profile)
    srv.user_serv._profile_for_log = staticmethod(lambda p: "{}")
    req = ProfileRequest(
      request_type="query_profile", timestamp=1,
      data=ProfileData(uid="u1", jwt_token="tok", device_type="pc"),
    )
    body = (UserServer.handle_query_profile(srv, req).data or {})["user_profile"]
    assert len(body["sleep_data"]) == 1  # 缺省仍是最近一晚
    assert "sequence_summaries" in body["sleep_data"][0]


@pytest.mark.parametrize("flags", [
  {},
  {"include_sop_tag_profile": False, "include_sop_recommendation_details": False},
  {"include_sop_tag_profile": True},
  {"include_sop_recommendation_details": True},
  {"include_sop_tag_profile": True, "include_sop_recommendation_details": True},
])
def test_query_sop_fields_are_independently_opt_in(monkeypatch, flags):
  import profile_query_gate
  monkeypatch.setattr(profile_query_gate, "is_query_allowed", lambda uid: True)
  profile = _profile()
  profile.sop_tag_profile = {"tag_exposure": {"forest": 2}}
  profile.sop_recommendation_details = {"reason": "test"}
  before = profile.model_dump()
  srv = _server(profile)
  srv.user_serv._profile_for_log = staticmethod(lambda p: "{}")
  req = ProfileRequest(request_type="query_profile", timestamp=1,
                       data=ProfileData(uid="u1", device_type="pc", **flags))
  resp = UserServer.handle_query_profile(srv, req)
  assert resp.code == 0
  body = resp.data["user_profile"]
  for key in ("sop_tag_profile", "sop_recommendation_details"):
    if flags.get(f"include_{key}", False):
      assert body[key] == before[key]
    else:
      assert key not in body
  assert profile.model_dump() == before


def test_update_preserves_sop_fields_by_default():
  profile = _profile()
  profile.sop_tag_profile = {"test": 1}
  profile.sop_recommendation_details = {"test": 2}
  body = _update(profile)
  assert body["sop_tag_profile"] == profile.sop_tag_profile
  assert body["sop_recommendation_details"] == profile.sop_recommendation_details
  trimmed = _update(profile, include_sop_tag_profile=False,
                    include_sop_recommendation_details=False)
  assert "sop_tag_profile" not in trimmed
  assert "sop_recommendation_details" not in trimmed
