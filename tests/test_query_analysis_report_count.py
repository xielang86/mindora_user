"""query_profile 的 analysis_reports 限量返回（analysis_report_count）测试。

语义：缺省 1=每类(day/week/month/overview/explore)只回最新一份；N=每类最近 N 份；
0=不携带 analysis_reports。报告序列升序、最新在尾，截尾保留最新。
"""
import pytest

from user_profile import (
  AnalysisTextReport, ProfileData, ProfileRequest, UserProfile,
)
from user_server import UserServer
import profile_query_gate


def _report(i: int, prefix: str) -> AnalysisTextReport:
  return AnalysisTextReport(
    request_type=prefix, date=f"2026-09-0{i + 1}", generated_at=100 * (i + 1),
    modules={f"{prefix}_module": {"text": f"{prefix}-{i}"}},
  )


def _profile(per_key: int = 3) -> UserProfile:
  return UserProfile(
    uid_emb=[],
    analysis_reports={
      key: [_report(i, key) for i in range(per_key)]
      for key in (
        "analysis_overview", "analysis_sleep_day", "analysis_explore",
        "analysis_sleep_week", "analysis_sleep_month",
      )
    },
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


def _reports_of(resp):
  return (resp.data or {}).get("user_profile", {}).get("analysis_reports")


class TestAnalysisReportCount:
  def test_default_returns_latest_one_per_key(self):
    """不带字段 → 默认 1：每类只回最新一份（generated_at 最大的那条）。"""
    resp = UserServer.handle_query_profile(_server(_profile()), _request())
    reports = _reports_of(resp)
    assert set(reports) == {
      "analysis_overview", "analysis_sleep_day", "analysis_explore",
      "analysis_sleep_week", "analysis_sleep_month",
    }
    for key, items in reports.items():
      assert len(items) == 1
      assert items[0]["generated_at"] == 300

  def test_n_returns_latest_n_ascending(self):
    """最近 N 份：升序保留（[-1] 最新），截尾。"""
    resp = UserServer.handle_query_profile(
      _server(_profile()), _request(analysis_report_count=2))
    day = _reports_of(resp)["analysis_sleep_day"]
    assert [r["generated_at"] for r in day] == [200, 300]

  def test_zero_omits_analysis_reports(self):
    resp = UserServer.handle_query_profile(
      _server(_profile()), _request(analysis_report_count=0))
    assert "analysis_reports" not in resp.data["user_profile"]

  def test_n_larger_than_stock_returns_all(self):
    resp = UserServer.handle_query_profile(
      _server(_profile(per_key=2)), _request(analysis_report_count=30))
    assert len(_reports_of(resp)["analysis_sleep_week"]) == 2

  def test_negative_rejected(self):
    with pytest.raises(Exception):
      _request(analysis_report_count=-1)

  def test_empty_profile_reports_untouched(self):
    """无报告时字段照常存在（各类空列表），不报错。"""
    resp = UserServer.handle_query_profile(_server(UserProfile(uid_emb=[])), _request())
    reports = _reports_of(resp)
    assert isinstance(reports, dict)
    assert all(items == [] for items in reports.values())

  def test_zero_does_not_touch_other_fields(self):
    resp = UserServer.handle_query_profile(
      _server(_profile()), _request(analysis_report_count=0))
    up = resp.data["user_profile"]
    assert "sleep_data" in up and "behaviors" in up
