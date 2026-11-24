import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from profile_service import UserProfileServ
from user_profile import BedtimeAdvice, UserProfile
from user_server import UserServer


def test_forced_refresh_calls_all_generators_and_replaces_fresh_advice(monkeypatch):
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  profile = UserProfile(uid_emb=[], last_request_language="en",
                        bedtime_advice=BedtimeAdvice(content="旧建议", language="zh-Hans",
                                                     generated_at=int(time.time())))
  calls = []
  monkeypatch.setattr(serv, "get_profile", lambda uid: profile.model_copy(deep=True))

  def save(uid, value):
    nonlocal profile
    profile = value.model_copy(deep=True)

  monkeypatch.setattr(serv, "save_profile", save)
  monkeypatch.setattr(serv, "calc_sleep_reco", lambda *args: calls.append("reco") or [])
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda *args: calls.append("sop") or [])

  class Content:
    def calc_sleep_insight(self, uid, p, force=False):
      calls.append(("insight", force))
      return None

    def calc_analysis_reports(self, uid, p, force=False):
      calls.append(("reports", force))
      return None

    def calc_bedtime_advice(self, uid, p, force=False):
      calls.append(("advice", force))
      return BedtimeAdvice(content="Rest well tonight.", language="en", generated_at=int(time.time()))

  serv.__dict__["_content"] = Content()
  assert serv.update_profile_llm("u1", force=True)
  assert calls == ["reco", "sop", ("insight", True), ("reports", True), ("advice", True)]
  assert profile.bedtime_advice.language == "en"
  assert profile.bedtime_advice.content == "Rest well tonight."


@pytest.mark.asyncio
async def test_ops_refresh_requires_admin_and_refreshes_once():
  profile = UserProfile(uid_emb=[], last_request_language="en")
  server = UserServer.__new__(UserServer)
  server.llm = SimpleNamespace(enabled=True)
  server.user_serv = SimpleNamespace(get_profile=lambda uid: profile,
                                     update_profile_llm=lambda *args: True)
  server._llm_tracker_lock = asyncio.Lock()
  server._llm_pending = {}
  server._llm_tasks = set()
  server._llm_update_tracker = {}
  server._llm_semaphore = asyncio.Semaphore(1)
  server._run_query = lambda fn, *args: asyncio.sleep(0, result=fn(*args))
  calls = []

  async def run(fn, *args):
    calls.append(args)
    return fn(*args)

  server._run_llm_work = run
  server._check_ops_admin = lambda token: asyncio.sleep(0, result="admin" if token == "valid" else None)

  class Request:
    def __init__(self, body):
      self.body = body

    async def json(self):
      return self.body

  denied = await server.handle_ops_refresh_user_llm_http(Request({"jwt_token": "bad", "uid": "u1"}))
  assert denied.status == 403
  assert calls == []
  ok = await server.handle_ops_refresh_user_llm_http(Request({"jwt_token": "valid", "uid": "u1"}))
  assert ok.status == 200
  assert calls == [("u1", False, False, True)]
