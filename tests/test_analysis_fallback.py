"""tests/test_analysis_fallback.py — 零睡眠记录用户的兜底分析内容。

覆盖：
  1. analysis_fallback 模板构建器（结构/语言/昵称/年龄段个性化）
  2. calc_sleep_insight：无数据出兜底、幂等、保留真实报告、有数据后替换兜底
  3. calc_analysis_reports：5 能力兜底、幂等、有数据后兜底被真报告替换
  4. ensure_fallback_content 读路径补齐
  5. _analysis_needed：同日兜底报告不拦截真分析
  6. /analysis 端点：零数据用户首次请求即见兜底内容
"""
from __future__ import annotations

import datetime
import threading
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import user_server as us
from analysis_content import AnalysisContentService
from analysis_fallback import (
  build_fallback_insight, build_fallback_report, fallback_modules_for,
)
from llm import SleepAnalysisLLM
from user_profile import (
  Profile, SleepResult, SleepInsightReport, AnalysisTextReport, UserProfile,
)
from user_server import UserProfileServ


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None
  return serv


def _real_report(greeting_title: str = "Real", generated_at: int | None = None) -> SleepInsightReport:
  return SleepInsightReport(
    date="2026-08-17",
    language="en",
    generated_at=generated_at if generated_at is not None else int(time.time()),
    llm_used=True,
    greeting={"module_id": 0, "title": greeting_title, "content": "real content"},
  )


# ── 1. 模板构建器 ────────────────────────────────────────────────────────────

class TestFallbackBuilders:
  def test_insight_structure_and_marker(self):
    profile = UserProfile(uid_emb=[], profile=Profile(nickname="小明", language="zh-CN"))
    report = build_fallback_insight(profile, "zh-Hans", "2026-08-18", now=1000)
    assert report.llm_used is False
    assert report.language == "zh-Hans"
    assert report.date == "2026-08-18"
    assert report.generated_at == 1000
    ids = {
      "greeting": 0, "onset": 1, "architecture": 2,
      "intervention": 3, "scene_preference": 4, "micro_education": 5,
    }
    for key, mid in ids.items():
      module = getattr(report, key)
      assert module.module_id == mid
      assert module.title and module.content and module.action
      assert module.evidence == []
    # 昵称个性化 + Mindora 引导
    assert "小明" in report.greeting.title
    assert "Mindora" in report.greeting.content

  def test_insight_without_nickname_uses_generic_title(self):
    report = build_fallback_insight(UserProfile(uid_emb=[]), "en", "2026-08-18")
    assert report.greeting.title == "Start your sleep journey"

  @pytest.mark.parametrize("lang,expect", [
    ("zh-Hant", "開啟你的睡眠之旅"),
    ("en", "Start your sleep journey"),
    ("ja", "Start your sleep journey"),      # 无模板语言 → en
    ("klingon", "Start your sleep journey"),
  ])
  def test_insight_language_selection(self, lang, expect):
    report = build_fallback_insight(UserProfile(uid_emb=[]), lang, "2026-08-18")
    assert expect in report.greeting.title
    assert report.language == ("en" if lang not in ("zh-Hans", "zh-Hant") else lang)

  def test_report_modules_match_llm_schema(self):
    profile = UserProfile(uid_emb=[])
    overview = fallback_modules_for("analysis_overview", profile, "en")
    assert set(overview["sleep_insight"]) == {"title", "description"}

    day = fallback_modules_for("analysis_sleep_day", profile, "en")
    assert set(day["sleep_scenarios"]) == {"title", "description"}
    assert set(day["stage_insights"]) == {"awake", "rem", "core", "deep"}
    assert set(day["stage_insights"]["deep"]) == {"description"}

    week = fallback_modules_for("analysis_sleep_week", profile, "en")
    assert set(week["score_summary"]) == {"label"}
    assert set(week["sleep_trends"]) == {"body", "description"}

    month = fallback_modules_for("analysis_sleep_month", profile, "en")
    assert set(month) == {"score_summary", "sleep_trends", "onset_efficiency"}

    explore = fallback_modules_for("analysis_explore", profile, "en")
    assert set(explore["header_summary"]) == {"intro_text", "intro_detail_text"}
    assert set(explore["onset_efficiency"]) == {"label", "description"}
    assert set(explore["sleep_advice"]) == {"description"}

  def test_explore_advice_varies_by_age_group(self):
    young = UserProfile(uid_emb=[], profile=Profile(birthday="2005-03-01"))
    senior = UserProfile(uid_emb=[], profile=Profile(birthday="1950-03-01"))
    unknown = UserProfile(uid_emb=[])
    a_young = fallback_modules_for("analysis_explore", young, "en")["sleep_advice"]["description"]
    a_senior = fallback_modules_for("analysis_explore", senior, "en")["sleep_advice"]["description"]
    a_unknown = fallback_modules_for("analysis_explore", unknown, "en")["sleep_advice"]["description"]
    assert len({a_young, a_senior, a_unknown}) == 3

  def test_build_fallback_report_fields(self):
    report = build_fallback_report(
      "analysis_sleep_week", UserProfile(uid_emb=[]), "zh-Hans",
      date="2026-08-18", start_date="2026-08-12", end_date="2026-08-18", now=7,
    )
    assert report.request_type == "analysis_sleep_week"
    assert report.start_date == "2026-08-12"
    assert report.llm_used is False
    assert report.generated_at == 7


# ── 2. calc_sleep_insight 兜底 ───────────────────────────────────────────────

class TestCalcSleepInsightFallback:
  def test_no_data_generates_fallback(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="zh-Hans")
    result = serv.calc_sleep_insight("u1", profile)
    assert result is not None
    assert result.llm_used is False
    assert result.language == "zh-Hans"

  def test_no_data_existing_fallback_kept_idempotent(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    first = serv.calc_sleep_insight("u1", profile)
    profile.sleep_insight = first
    second = serv.calc_sleep_insight("u1", profile)
    assert second is first  # 幂等：同一对象，不重复生成

  def test_no_data_existing_real_report_kept(self):
    """真实报告（历史上曾有数据、后来数据轮换掉了）不被兜底覆盖。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    profile.sleep_insight = _real_report("Keep me")
    result = serv.calc_sleep_insight("u1", profile)
    assert result.greeting.title == "Keep me"

  def test_fallback_replaced_by_real_once_data_arrives(self, monkeypatch):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    profile.sleep_insight = serv.calc_sleep_insight("u1", profile)  # 兜底
    assert profile.sleep_insight.llm_used is False
    # 第一晚数据到达
    profile.sleep_data = [SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)]
    monkeypatch.setattr(serv.llm, "enabled", True)
    monkeypatch.setattr("analysis_content._polish_output_ok", lambda *_: True)

    def fake_generate(request_type, ctx, language, modules):
      # 润色校验器要求输出 key 集合与规则结论一一对应（见 polish_output_ok）
      return {
        c["key"]: {"title": "Real night", "content": "c", "evidence": [], "action": ""}
        for c in ctx.get("conclusions", [])
      }

    monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
    result = serv.calc_sleep_insight("u1", profile)
    assert result.llm_used is True
    assert result.greeting.title == "Real night"

  def test_data_but_llm_disabled_returns_rule_template(self):
    """有数据但 LLM 不可用：规则模板报告立即可用（llm_used=False，LLM 挂了也不影响输出），
    不出零数据兜底（分析兜底只在零记录时触发）。"""
    serv = _serv()
    profile = UserProfile(
      uid_emb=[],
      sleep_data=[SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)],
    )
    result = serv.calc_sleep_insight("u1", profile)
    assert result is not None
    assert result.llm_used is False


# ── 3. calc_analysis_reports 兜底 ───────────────────────────────────────────

class TestCalcAnalysisReportsFallback:
  def test_no_data_generates_five_fallback_reports(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="en")
    reports = serv.calc_analysis_reports("u1", profile)
    assert reports is not None
    today = datetime.date.today().isoformat()
    for key in ("analysis_overview", "analysis_sleep_day", "analysis_explore",
                "analysis_sleep_week", "analysis_sleep_month"):
      assert len(reports[key]) == 1
      assert reports[key][0].llm_used is False
      assert reports[key][0].date == today
    # 周/月带起止日期
    assert reports["analysis_sleep_week"][0].start_date is not None
    assert reports["analysis_sleep_month"][0].start_date is not None

  def test_no_data_idempotent_second_call_returns_none(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    first = serv.calc_analysis_reports("u1", profile)
    assert first is not None
    profile.analysis_reports = first
    assert serv.calc_analysis_reports("u1", profile) is None

  def test_no_data_fills_only_missing_periods(self):
    """已有当期真实报告的能力不出兜底；只补缺失的。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    today = datetime.date.today().isoformat()
    profile.analysis_reports["analysis_overview"] = [AnalysisTextReport(
      request_type="analysis_overview", date=today,
      generated_at=int(time.time()) - 100, llm_used=True,
      modules={"sleep_insight": {"title": "Real", "description": "real"}},
    )]
    reports = serv.calc_analysis_reports("u1", profile)
    assert reports is not None
    overview = reports["analysis_overview"]
    assert len(overview) == 1 and overview[0].llm_used is True
    assert reports["analysis_sleep_day"][0].llm_used is False

  def test_fallback_replaced_by_real_once_data_arrives(self, monkeypatch):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    profile.analysis_reports = serv.calc_analysis_reports("u1", profile)
    assert profile.analysis_reports["analysis_sleep_day"][0].llm_used is False

    profile.sleep_data = [SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)]
    monkeypatch.setattr(serv.llm, "enabled", True)
    monkeypatch.setattr(
      serv.llm, "generate_sync",
      lambda rt, ctx, lang, modules: {"description": f"real {rt}"},
    )
    reports = serv.calc_analysis_reports("u1", profile)
    assert reports is not None
    day = reports["analysis_sleep_day"]
    # 同周期 upsert：兜底被真报告替换，不是两条并存
    assert len(day) == 1
    assert day[0].llm_used is False
    assert day[0].modules["sleep_scenarios"]["description"] == "No sleep-scene usage was recorded last night."


# ── 3b. 语言漂移重生成（last_request_language 为唯一事实源） ─────────────────

class TestLanguageDriftRegeneration:
  def test_fallback_insight_regenerated_on_language_switch(self):
    """零记录兜底 insight：语言漂移即时重建（模板零成本，切语言即时生效）。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="zh-Hans")
    profile.sleep_insight = serv.calc_sleep_insight("u1", profile)
    assert profile.sleep_insight.language == "zh-Hans"
    # 用户切到英文（update_profile 信封 data.language="en"）
    profile.last_request_language = "en"
    result = serv.calc_sleep_insight("u1", profile)
    assert result is not profile.sleep_insight
    assert result.language == "en"
    assert result.llm_used is False

  def test_fallback_insight_template_only_language_no_regen_loop(self):
    """ja 等无模板的语言归一为 en：已存 en 兜底不被误判漂移而每次重建。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="ja")
    first = serv.calc_sleep_insight("u1", profile)
    assert first.language == "en"
    profile.sleep_insight = first
    assert serv.calc_sleep_insight("u1", profile) is first

  def test_real_insight_kept_when_data_aged_out_despite_drift(self):
    """数据轮换掉后只剩真实报告：语言漂移也不出兜底覆盖（无数据无法重建）。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="zh-Hans")
    profile.sleep_insight = _real_report("Keep me")  # 历史 LLM 报告，language="en"
    result = serv.calc_sleep_insight("u1", profile)
    assert result.greeting.title == "Keep me"

  def test_real_insight_regenerated_on_language_drift(self, monkeypatch):
    """有数据 + 真实报告语言漂移：不再复用，走 LLM 按新语言重生成。"""
    serv = _serv()
    profile = UserProfile(
      uid_emb=[],
      last_request_language="en",
      sleep_data=[SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)],
    )
    profile.sleep_insight = SleepInsightReport(
      date="2026-08-20", language="zh-Hans", generated_at=int(time.time()),
      llm_used=True, greeting={"module_id": 0, "title": "旧中文", "content": "c"},
    )
    captured = {}

    def fake_generate(request_type, ctx, language, modules):
      captured["language"] = language
      # 润色校验器要求输出 key 集合与规则结论一一对应（见 polish_output_ok）
      return {
        c["key"]: {"title": "Fresh EN", "content": "c", "evidence": [], "action": ""}
        for c in ctx.get("conclusions", [])
      }

    monkeypatch.setattr(serv.llm, "enabled", True)
    monkeypatch.setattr("analysis_content._polish_output_ok", lambda *_: True)
    monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
    result = serv.calc_sleep_insight("u1", profile)
    assert captured["language"] == "en"
    assert result.language == "en"
    assert result.greeting.title == "Fresh EN"

  def test_fallback_reports_regenerated_on_language_drift(self):
    """零记录兜底 reports：语言漂移后同周期 upsert 为新语言，不是两条并存。"""
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="zh-Hans")
    profile.analysis_reports = serv.calc_analysis_reports("u1", profile)
    assert profile.analysis_reports["analysis_overview"][0].language == "zh-Hans"
    profile.last_request_language = "en"
    reports = serv.calc_analysis_reports("u1", profile)
    assert reports is not None
    overview = reports["analysis_overview"]
    assert len(overview) == 1
    assert overview[0].language == "en"
    assert overview[0].llm_used is False
    # 漂移重建后再跑一次：幂等，不再变
    profile.analysis_reports = reports
    assert serv.calc_analysis_reports("u1", profile) is None

  def test_real_report_regenerated_on_language_drift(self, monkeypatch):
    """有数据 + 当期真实报告语言漂移：不复用，重新调 LLM。"""
    serv = _serv()
    profile = UserProfile(
      uid_emb=[],
      last_request_language="en",
      sleep_data=[SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)],
    )
    today = datetime.date.today().isoformat()
    profile.analysis_reports["analysis_overview"] = [AnalysisTextReport(
      request_type="analysis_overview", date=today, language="zh-Hans",
      generated_at=int(time.time()) - 100, llm_used=True,
      modules={"sleep_insight": {"title": "旧中文", "description": "zh"}},
    )]
    calls = []

    def fake_generate(rt, ctx, lang, modules):
      calls.append((rt, lang))
      if rt == "insight_polish":
        # 规则模板类型走 insight_polish：输出 key 必须与规则结论一一对应（polish_output_ok）
        _KEY2RT = {"home_summary": "analysis_overview", "trend7": "analysis_sleep_week",
                   "trend30": "analysis_sleep_month"}
        return {
          c["key"]: {"title": "t", "text": f"fresh {_KEY2RT.get(c['key'], c['key'])}"}
          for c in ctx.get("conclusions", [])
        }
      return {"description": f"fresh {rt}"}

    monkeypatch.setattr(serv.llm, "enabled", True)
    monkeypatch.setattr("analysis_content._polish_output_ok", lambda *_: True)
    monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
    reports = serv.calc_analysis_reports("u1", profile)
    assert reports is not None
    assert ("insight_polish", "en") in calls  # overview 等规则模板类型按新语言重润色
    overview = reports["analysis_overview"]
    assert len(overview) == 1
    assert overview[0].language == "en"
    assert overview[0].modules["sleep_insight"]["description"] == "fresh analysis_overview"

  def test_real_current_report_not_regenerated(self, monkeypatch):
    serv = _serv()
    profile = UserProfile(
      uid_emb=[],
      sleep_data=[SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)],
    )
    today = datetime.date.today().isoformat()
    profile.analysis_reports["analysis_sleep_day"] = [AnalysisTextReport(
      request_type="analysis_sleep_day", date=today,
      generated_at=int(time.time()) - 100, llm_used=True,
      modules={"description": "cached real"},
    )]
    from analysis_content import explore_input_fingerprint
    profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
    calls = []
    monkeypatch.setattr(serv.llm, "enabled", True)
    monkeypatch.setattr(
      serv.llm, "generate_sync",
      lambda rt, ctx, lang, modules: calls.append(rt) or {"description": "new"},
    )
    reports = serv.calc_analysis_reports("u1", profile)
    assert "analysis_sleep_day" not in calls
    assert reports["analysis_sleep_day"][0].modules["description"] == "cached real"


# ── 4. ensure_fallback_content（读路径补齐） ────────────────────────────────

class TestEnsureFallbackContent:
  def test_fills_gaps_and_reports_changed(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[], last_request_language="zh-Hans")
    changed = serv.content.ensure_fallback_content("u1", profile)
    assert changed is True
    assert profile.sleep_insight is not None
    assert profile.sleep_insight.llm_used is False
    assert profile.sleep_insight.language == "zh-Hans"
    assert len(profile.analysis_reports["analysis_explore"]) == 1

  def test_idempotent_second_call(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    assert serv.content.ensure_fallback_content("u1", profile) is True
    assert serv.content.ensure_fallback_content("u1", profile) is False

  def test_noop_when_sleep_data_exists(self):
    serv = _serv()
    profile = UserProfile(
      uid_emb=[],
      sleep_data=[SleepResult(timestamp=int(time.time()), sleep_quality=80.0)],
    )
    assert serv.content.ensure_fallback_content("u1", profile) is False
    assert profile.sleep_insight is None
    assert profile.analysis_reports["analysis_sleep_day"] == []

  def test_keeps_existing_real_insight(self):
    serv = _serv()
    profile = UserProfile(uid_emb=[])
    profile.sleep_insight = _real_report("Real greeting")
    serv.content.ensure_fallback_content("u1", profile)
    assert profile.sleep_insight.greeting.title == "Real greeting"


# ── 5. _analysis_needed：同日兜底报告不拦截真分析 ───────────────────────────

class TestAnalysisNeededWithFallback:
  def _profile_with_day_report(self, llm_used: bool) -> UserProfile:
    now = int(time.time())
    profile = UserProfile(
      uid_emb=[],
      sleep_data=[SleepResult(timestamp=now - 3600, sleep_quality=80.0)],
    )
    today = datetime.datetime.fromtimestamp(now).date().isoformat()
    profile.analysis_reports["analysis_sleep_day"] = [AnalysisTextReport(
      request_type="analysis_sleep_day", date=today, generated_at=now - 7200,
      llm_used=llm_used, modules={"description": "x"},
    )]
    from analysis_content import explore_input_fingerprint
    profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
    return profile

  def test_same_day_fallback_report_still_needed(self):
    server = us.UserServer.__new__(us.UserServer)
    profile = self._profile_with_day_report(llm_used=False)
    needed, ts = server._analysis_needed(profile)
    assert needed is True and ts is not None

  def test_same_day_real_report_not_needed(self):
    server = us.UserServer.__new__(us.UserServer)
    profile = self._profile_with_day_report(llm_used=True)
    needed, _ = server._analysis_needed(profile)
    assert needed is False


# ── 6. /analysis 端点：零数据用户首次请求即见兜底 ───────────────────────────

def _build_zero_data_app(profile: UserProfile):
  """真实 handle_analysis_http + 真实 builders + 真 AnalysisContentService（无 LLM）。"""
  saved: dict = {}

  class FakeUserServ:
    content = AnalysisContentService(lambda: None)

    def get_profile(self, uid):
      return profile if uid == "test_uid" else None

    def save_profile(self, uid, p):
      saved[uid] = p

    def note_request_meta(self, uid, timezone, language, fill_only=True):
      # 与真实实现同口径（fill_only 补缺）：兜底内容语言以画像登记值为准
      if fill_only:
        if not profile.last_request_timezone:
          profile.last_request_timezone = timezone
        if not profile.last_request_language:
          profile.last_request_language = language

  class FakeSrv:
    def __init__(self):
      self.user_serv = FakeUserServ()
      self._activity = {}
      self._llm_last_attempt = {}
      self.scheduled = []

    def _parse_for_uid(self, data):
      return data.uid if data.uid else None

    def _mark_activity(self, uid):
      self._activity[uid] = time.time()

    async def _schedule_llm_update_if_needed(self, uid, skip_reco=False, skip_analysis=False, delay=0):
      self.scheduled.append(uid)

  srv = FakeSrv()
  for name in (
    "handle_analysis_http", "_build_analysis_data", "_build_overview",
    "_build_sleep_day", "_build_sleep_week", "_build_sleep_month",
    "_build_explore", "get_overall_score", "_filter_modules", "_analysis_needed",
  ):
    setattr(srv, name, types.MethodType(getattr(us.UserServer, name), srv))
  srv._newest_sleep_ts = us.UserServer._newest_sleep_ts  # staticmethod：直接赋值，不绑定

  app = web.Application()
  app.router.add_post("/analysis", srv.handle_analysis_http)
  app.saved = saved
  return app


def _analysis_body(request_type="analysis_overview", **data_overrides):
  data = {"uid": "test_uid", "language": "zh-Hans", "timezone": "Asia/Shanghai"}
  data.update(data_overrides)
  return {
    "request_type": request_type, "version": "1.0",
    "timestamp": int(time.time()), "data": data,
  }


class TestAnalysisEndpointFallback:
  @pytest.mark.asyncio
  async def test_zero_data_user_gets_fallback_immediately(self):
    app = _build_zero_data_app(UserProfile(uid_emb=[]))
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/analysis", json=_analysis_body())
      assert resp.status == 200
      payload = await resp.json()
      insight = payload["data"]["sleep_insight"]
      assert insight["title"]
      assert "Mindora" in insight["description"]
    # 兜底已落库（下次不再重复生成），且标记 llm_used=False
    saved_profile = app.saved["test_uid"]
    report = saved_profile.analysis_reports["analysis_overview"][0]
    assert report.llm_used is False
    assert saved_profile.sleep_insight is not None
    assert saved_profile.sleep_insight.llm_used is False

  @pytest.mark.asyncio
  async def test_zero_data_explore_uses_documented_cards(self):
    app = _build_zero_data_app(UserProfile(uid_emb=[]))
    async with TestClient(TestServer(app)) as client:
      resp = await client.post("/analysis", json=_analysis_body("analysis_explore"))
      assert resp.status == 200
      payload = await resp.json()
      assert payload["data"]["sleep_advice"]["description"]
      assert "insight" not in payload["data"]
      assert payload["data"]["header_summary"]["intro_text"]
      assert payload["data"]["data_ready"] is False

  @pytest.mark.asyncio
  async def test_second_request_idempotent_no_extra_save(self):
    app = _build_zero_data_app(UserProfile(uid_emb=[]))
    async with TestClient(TestServer(app)) as client:
      await client.post("/analysis", json=_analysis_body())
      assert len(app.saved) == 1
      resp = await client.post("/analysis", json=_analysis_body())
      assert resp.status == 200
      assert len(app.saved) == 1  # 内容已齐，不再落库
