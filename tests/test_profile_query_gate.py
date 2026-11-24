"""query_profile 时段黑名单（profile_query_gate）测试。

配置 JSON + mtime 热加载；名单内 uid 仅在 allow_windows 时段内可 query_profile，
时段外 403；名单外不受影响；update_profile / query_revision 不受限。
"""
import datetime
import json

import pytest
from zoneinfo import ZoneInfo

import profile_query_gate as gate
from user_profile import ProfileData, ProfileRequest, UserProfile
from user_server import UserServer

UTC = datetime.timezone.utc
SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
  """每个用例独立的配置文件路径 + 缓存复位。"""
  path = tmp_path / "blacklist.json"
  monkeypatch.setenv("PROFILE_QUERY_BLACKLIST_PATH", str(path))
  gate._reset_cache_for_test()
  yield path
  gate._reset_cache_for_test()


def _write(path, blacklist: dict):
  path.write_text(json.dumps({"blacklist": blacklist}), encoding="utf-8")
  gate._reset_cache_for_test()


def _at(hh: int, mm: int, tz=UTC) -> datetime.datetime:
  return datetime.datetime(2026, 8, 24, hh, mm, tzinfo=tz)


# ── 时段判定 ─────────────────────────────────────────────────────────

class TestWindowMatching:
  def test_uid_not_in_blacklist_always_allowed(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00"]}})
    assert gate.is_query_allowed("someone-else", _at(3, 0)) is True

  def test_inside_window_allowed(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00"]}})
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is True
    assert gate.is_query_allowed("uid-a", _at(2, 0)) is True   # 边界含
    assert gate.is_query_allowed("uid-a", _at(6, 0)) is True

  def test_outside_window_denied(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00"]}})
    assert gate.is_query_allowed("uid-a", _at(1, 59)) is False
    assert gate.is_query_allowed("uid-a", _at(6, 1)) is False

  def test_overnight_window(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["22:00-02:00"]}})
    assert gate.is_query_allowed("uid-a", _at(23, 30)) is True
    assert gate.is_query_allowed("uid-a", _at(1, 30)) is True
    assert gate.is_query_allowed("uid-a", _at(12, 0)) is False

  def test_multiple_windows(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00", "13:00-14:00"]}})
    assert gate.is_query_allowed("uid-a", _at(13, 30)) is True
    assert gate.is_query_allowed("uid-a", _at(10, 0)) is False

  def test_empty_windows_means_blocked_all_day(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": []}})
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is False

  def test_entry_timezone(self, _isolated_config):
    """窗口按条目自己的时区解释：上海 02:00-06:00 = UTC 18:00-22:00（前一天）。"""
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00"], "timezone": "Asia/Shanghai"}})
    assert gate.is_query_allowed("uid-a", _at(20, 0, UTC)) is True    # 上海 04:00
    assert gate.is_query_allowed("uid-a", _at(4, 0, UTC)) is False    # 上海 12:00

  def test_naive_default_timezone_is_utc(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["02:00-06:00"]}})
    assert gate.is_query_allowed("uid-a", _at(3, 0, UTC)) is True


# ── 配置加载 ─────────────────────────────────────────────────────────

class TestConfigLoading:
  def test_missing_file_allows_all(self, _isolated_config):
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is True

  def test_broken_json_keeps_previous_good(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": []}})
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is False
    _isolated_config.write_text("{not json", encoding="utf-8")
    # 不 reset 缓存：mtime 变化触发重载，解析失败应沿用缓存里的上一份可用配置
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is False

  def test_invalid_window_skipped(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": ["25:00-99:99", "bad"]}})
    # 全部窗口非法 → 等效空窗口 → 全天禁止
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is False

  def test_hot_reload_on_change(self, _isolated_config):
    _write(_isolated_config, {"uid-a": {"allow_windows": []}})
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is False
    _write(_isolated_config, {"uid-a": {"allow_windows": ["00:00-23:59"]}})
    assert gate.is_query_allowed("uid-a", _at(3, 0)) is True


# ── handle_query_profile 接入 ────────────────────────────────────────

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


def _request():
  return ProfileRequest(
    request_type="query_profile", timestamp=1,
    data=ProfileData(uid="u1", jwt_token="tok", device_type="pc"),
  )


class TestHandleQueryProfileGate:
  def test_denied_outside_window(self, _isolated_config, monkeypatch):
    _write(_isolated_config, {"u1": {"allow_windows": ["02:00-06:00"]}})
    # 把 gate 的"现在"钉在窗口外
    monkeypatch.setattr(gate.datetime, "datetime", _FrozenDatetime)
    srv = _server(UserProfile(uid_emb=[]))
    resp = UserServer.handle_query_profile(srv, _request())
    assert resp.code == 403
    assert resp.data is None

  def test_allowed_inside_window(self, _isolated_config, monkeypatch):
    _write(_isolated_config, {"u1": {"allow_windows": ["02:00-06:00"]}})
    monkeypatch.setattr(gate.datetime, "datetime", _FrozenDatetimeEarly)
    profile = UserProfile(uid_emb=[])
    srv = _server(profile)
    resp = UserServer.handle_query_profile(srv, _request())
    assert resp.code == 0
    assert resp.data["user_profile"] is not None

  def test_unlisted_uid_unaffected(self, _isolated_config):
    _write(_isolated_config, {"other-uid": {"allow_windows": []}})
    srv = _server(UserProfile(uid_emb=[]))
    resp = UserServer.handle_query_profile(srv, _request())
    assert resp.code == 0


class _FrozenDatetime(datetime.datetime):
  """钉在 UTC 12:00（窗口外）。"""
  @classmethod
  def now(cls, tz=None):
    return datetime.datetime(2026, 8, 24, 12, 0, tzinfo=tz or UTC)


class _FrozenDatetimeEarly(datetime.datetime):
  """钉在 UTC 03:00（窗口内）。"""
  @classmethod
  def now(cls, tz=None):
    return datetime.datetime(2026, 8, 24, 3, 0, tzinfo=tz or UTC)
