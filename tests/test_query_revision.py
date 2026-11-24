"""query_revision 轻量变更探测（设备 3s 探测 + 300s 全量兜底）。

设计：revision 是 UserProfile 字段（随画像落盘，原子），save_profile 统一 +1，
任何写路径（App/设备 update、后台 LLM 写回、读路径补缺）自动触发；
query_profile / update_profile 响应经 model_dump 天然携带 revision（回显过滤基线）。
"""
import json
import threading
from contextlib import contextmanager

from user_profile import ProfileData, ProfileRequest, UserProfile
from user_server import UserProfileServ, UserServer


class _FakeDB:
  def __init__(self):
    self.store = {}

  def get(self, k):
    return self.store.get(k)

  def put(self, k, v):
    self.store[k] = v

  @contextmanager
  def snapshot(self):
    yield self

  @contextmanager
  def write_batch(self, transaction=True):
    pending = {}

    class Batch:
      def put(self, k, v):
        pending[k] = v

    yield Batch()
    self.store.update(pending)


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  serv.storage_mode = "leveldb"
  serv.db = _FakeDB()
  return serv


# ── revision 维护（save_profile 统一 +1）────────────────────────────

def test_save_profile_bumps_revision_and_persists():
  serv = _serv()
  profile = UserProfile(uid_emb=[])
  serv.save_profile("u1", profile)
  assert profile.revision == 1
  serv.save_profile("u1", profile)
  assert profile.revision == 2
  # 落盘后重新读出，版本号保持
  assert serv.get_profile("u1").revision == 2


def test_legacy_profile_defaults_revision_zero():
  """老画像（无 revision 字段）读出为 0，首次 save 变 1。"""
  serv = _serv()
  serv.db.store[b"u1"] = json.dumps({"uid_emb": []}).encode()
  profile = serv.get_profile("u1")
  assert profile.revision == 0
  serv.save_profile("u1", profile)
  assert profile.revision == 1


def test_client_sent_revision_ignored_on_merge():
  """设备 LWW 合并回推的 profile 里带陈旧 revision：合并分支不拷贝，不被污染。"""
  serv = _serv()
  existing = UserProfile(uid_emb=[])
  existing.revision = 5
  new_profile = UserProfile(uid_emb=[], revision=999)
  merged = serv._apply_basic_update("u1", new_profile, existing)
  assert merged.revision == 5
  serv.db.put(b"u1", json.dumps({"uid_emb": [], "revision": 5}).encode())
  serv.save_profile("u1", merged)
  assert merged.revision == 6


def test_new_profile_revision_reset_to_zero():
  """新建画像：客户端携带的 revision 作废，从 0 起。"""
  serv = _serv()
  new_profile = UserProfile(uid_emb=[], revision=999)
  merged = serv._apply_basic_update("u1", new_profile, None)
  assert merged.revision == 0


# ── query_revision / query_profile 接口 ──────────────────────────────

def _server(profile):
  srv = UserServer.__new__(UserServer)
  srv._parse_for_uid = lambda data: "u1" if data and data.uid else None

  class _FakeServ:
    @staticmethod
    def get_revision(uid):
      return profile.revision if profile else 0

    @staticmethod
    def get_profile(uid):
      return profile

    @staticmethod
    def _profile_for_log(p):
      return "{}"

  srv.user_serv = _FakeServ()
  return srv


def _request(request_type="query_revision"):
  return ProfileRequest(
    request_type=request_type, timestamp=1,
    data=ProfileData(uid="u1", jwt_token="tok", device_type="pc"),
  )


def test_query_revision_returns_current_value():
  profile = UserProfile(uid_emb=[])
  profile.revision = 42
  srv = _server(profile)
  resp = UserServer.handle_query_revision(srv, _request())
  assert resp.code == 0
  assert resp.data == {"revision": 42}
  assert resp.request_type == "query_revision"


def test_query_revision_unknown_user_returns_zero():
  srv = _server(None)
  resp = UserServer.handle_query_revision(srv, _request())
  assert resp.code == 0
  assert resp.data == {"revision": 0}


def test_query_revision_requires_identity():
  srv = _server(None)
  srv._parse_for_uid = lambda data: None
  resp = UserServer.handle_query_revision(srv, _request())
  assert resp.code == 401


def test_query_profile_response_carries_revision():
  """全量拉取响应自带 revision：设备拉完直接记基线，无需多问一次。"""
  profile = UserProfile(uid_emb=[])
  profile.revision = 7
  srv = _server(profile)
  resp = UserServer.handle_query_profile(srv, _request("query_profile"))
  assert resp.code == 0
  assert resp.data["user_profile"]["revision"] == 7
