import threading
import time

import pytest

from analysis_content import AnalysisContentService
from config import Config
from llm.analysis import _language_matches
from profile_service import UserProfileServ
from user_profile import BedtimeAdvice, UserProfile, bedtime_advice_due
from user_server import UserServer


def test_bedtime_advice_due_at_seven_day_boundary():
  now = 1_800_000_000
  profile = UserProfile(uid_emb=[])
  assert bedtime_advice_due(profile, now)
  profile.bedtime_advice = BedtimeAdvice(content="A suggestion", generated_at=now - 7 * 86400 + 1)
  assert not bedtime_advice_due(profile, now)
  profile.bedtime_advice.generated_at -= 1
  assert bedtime_advice_due(profile, now)


def test_bedtime_advice_due_when_language_changes():
  now = 1_800_000_000
  profile = UserProfile(uid_emb=[], last_request_language="en",
                        bedtime_advice=BedtimeAdvice(content="早点休息", language="zh-Hans", generated_at=now))
  assert bedtime_advice_due(profile, now, language="en")
  assert not bedtime_advice_due(profile, now, language="zh-Hans")


@pytest.mark.asyncio
async def test_missing_advice_schedules_without_sleep_analysis(monkeypatch):
  server = UserServer.__new__(UserServer)
  server._llm_last_attempt = {}
  server._activity = {}
  server.llm = type("LLM", (), {"enabled": True})()
  calls = []

  async def schedule(uid, skip_reco, skip_analysis, delay=0):
    calls.append((uid, skip_reco, skip_analysis, delay))

  server._schedule_llm_update_if_needed = schedule
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", False)
  await server._maybe_schedule_llm_update("u1", UserProfile(uid_emb=[]), None, None)
  assert calls == [("u1", True, True, 0)]


@pytest.mark.asyncio
async def test_changed_language_schedules_fresh_advice(monkeypatch):
  server = UserServer.__new__(UserServer)
  server._llm_last_attempt = {}
  server._activity = {}
  server.llm = type("LLM", (), {"enabled": True})()
  calls = []

  async def schedule(uid, skip_reco, skip_analysis, delay=0):
    calls.append((uid, skip_reco, skip_analysis, delay))

  server._schedule_llm_update_if_needed = schedule
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", False)
  profile = UserProfile(uid_emb=[], last_request_language="en",
                        bedtime_advice=BedtimeAdvice(content="早点休息", language="zh-Hans",
                                                     generated_at=int(time.time())))
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert calls == [("u1", True, True, 0)]


def test_generate_bedtime_advice_uses_profile_context_and_reuses_fresh_result():
  calls = []

  class LLM:
    enabled = True

    def generate_sync(self, request_type, ctx, language, modules):
      calls.append((request_type, ctx, language, modules))
      return {"content": "  Try a steady wind-down time tonight.  "}

  content = AnalysisContentService(lambda: LLM())
  profile = UserProfile(uid_emb=[], last_request_language="zh-CN")
  advice = content.calc_bedtime_advice("u1", profile)
  assert advice.content == "Try a steady wind-down time tonight."
  assert advice.language == "zh-Hans"
  assert calls[0][0] == "bedtime_advice"
  assert "record_count" not in calls[0][1]  # 无睡眠记录时只提供稀疏上下文
  profile.bedtime_advice = advice
  assert content.calc_bedtime_advice("u1", profile) is None
  assert len(calls) == 1


def test_generate_bedtime_advice_replaces_fresh_other_language():
  class LLM:
    enabled = True

    def generate_sync(self, request_type, ctx, language, modules):
      assert language == "en"
      return {"content": "Keep a steady bedtime."}

  profile = UserProfile(uid_emb=[], last_request_language="en",
                        bedtime_advice=BedtimeAdvice(content="早点休息", language="zh-Hans",
                                                     generated_at=int(time.time())))
  advice = AnalysisContentService(lambda: LLM()).calc_bedtime_advice("u1", profile)
  assert advice.content == "Keep a steady bedtime."
  assert advice.language == "en"


def test_english_bedtime_advice_rejects_chinese_llm_output():
  assert not _language_matches({"content": "睡前调暗灯光。"}, "en", "bedtime_advice")
  assert _language_matches({"content": "Dim the lights before bed."}, "en", "bedtime_advice")


def test_background_update_persists_advice_and_preserves_it_on_llm_failure(monkeypatch):
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  stored = UserProfile(uid_emb=[])

  class LLM:
    enabled = True
    result = {"content": "Dim the lights before bed."}

    def generate_sync(self, *args):
      return self.result

  llm = LLM()
  serv.llm = llm
  monkeypatch.setattr(serv, "get_profile", lambda uid: stored.model_copy(deep=True))

  def save(uid, profile):
    nonlocal stored
    stored = profile.model_copy(deep=True)

  monkeypatch.setattr(serv, "save_profile", save)
  monkeypatch.setattr(serv, "calc_standard_sop_reco", lambda *args: [])
  assert serv.update_profile_llm("u1", True, True)
  assert stored.bedtime_advice.content == "Dim the lights before bed."
  first_generated_at = stored.bedtime_advice.generated_at

  stored.bedtime_advice.generated_at -= 7 * 86400
  llm.result = {"content": ""}
  assert serv.update_profile_llm("u1", True, True)
  assert stored.bedtime_advice.content == "Dim the lights before bed."
  assert stored.bedtime_advice.generated_at == first_generated_at - 7 * 86400
