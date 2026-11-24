"""query_profile 的 mindora_record 开关（include_mindora_record）测试。

语义：缺省 True=携带（老客户端行为不变）；False=响应不含 mindora_record 键。
与 include_behaviors 互相独立——mindora_record 是 behaviors.plays 的服务端聚合态，
但 App 可能只想去掉其中一个。
"""
import pytest

from user_profile import ProfileData, ProfileRequest, UserProfile
from user_server import UserServer
import profile_query_gate

CMD = "sleep.scene.kyoto_forest"


def _profile() -> UserProfile:
  return UserProfile(
    uid_emb=[],
    mindora_record={CMD: [(1000, 60), (2000, 90)]},
    behaviors={"plays": [(1000, {"cmd": CMD, "event": "sop_start"})]},
    health_sync_days={"2026-09-23": 2},
  )


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


def _profile_of(resp):
  return (resp.data or {}).get("user_profile", {})


class TestIncludeMindoraRecord:
  def test_default_carries_mindora_record(self):
    resp = UserServer.handle_query_profile(_server(_profile()), _request())
    assert resp.code == 0
    assert _profile_of(resp)["mindora_record"][CMD] == [(1000, 60), (2000, 90)]

  def test_false_omits_mindora_record(self):
    resp = UserServer.handle_query_profile(
      _server(_profile()), _request(include_mindora_record=False))
    assert "mindora_record" not in _profile_of(resp)

  def test_independent_from_include_behaviors(self):
    """去掉 mindora_record 不影响 behaviors / health_sync_days。"""
    body = _profile_of(UserServer.handle_query_profile(
      _server(_profile()), _request(include_mindora_record=False)))
    assert "behaviors" in body and "health_sync_days" in body

    body = _profile_of(UserServer.handle_query_profile(
      _server(_profile()), _request(include_behaviors=False)))
    assert "behaviors" not in body and "health_sync_days" not in body
    assert "mindora_record" in body

  def test_both_off(self):
    body = _profile_of(UserServer.handle_query_profile(
      _server(_profile()),
      _request(include_behaviors=False, include_mindora_record=False)))
    assert "behaviors" not in body and "mindora_record" not in body

  def test_unknown_clients_unaffected(self):
    """老客户端不传该字段 → 默认携带。"""
    data = ProfileData(uid="u1", jwt_token="tok")
    assert data.include_mindora_record is True
