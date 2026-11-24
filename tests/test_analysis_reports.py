"""Tests for the /analysis stored-report path and LLM context/dispatch.

按 client_request.md 收敛后：/sleep_advice 端点与 SleepAdvice* 模型已删除，
6 模块洞察文案映射到 analysis_explore 的标准卡片字段。
本文件覆盖：
  1. extract_sleep_context 的聚合统计与 sleep_analysis 字段
  2. LLM dispatch（sleep_insight_report 类型）
  3. /analysis 库存报告服务（命中/落空/历史日期回退/modules 过滤）
  4. analysis_explore 响应按标准卡片返回洞察文案
"""
from __future__ import annotations

import datetime
import json
import time
import types
import pytest

from user_profile import (
    UserProfile,
    SleepResult,
    SleepInsightReport,
    AnalysisTextReport,
)


class TestSleepContextExtraction:
    def test_extract_sleep_context_includes_stats_and_sleep_analysis(self):
        from llm import extract_sleep_context

        profile = UserProfile(
            behaviors={"heart_rate": [(1, 70)], "clicks": [(2, "play")]},
            sleep_data=[
                SleepResult(
                    timestamp=int(time.time()),
                    sleep_quality=85.0,
                    soe=78.0,
                    onset=20.0,
                    first_sleep_time="23:10",
                    hr_before_sleep=66,
                    rr_before_sleep=14,
                    avg_heart_rate=60,
                    hrv=42,
                )
            ],
            sleep_analysis={
                "sleep_trend_week": "Trend week",
                "scene": {"title": "Scene title", "text": "Scene text"},
            },
            profile={"nickname": "Alice", "avatar_base64": "abc"},
        )

        class ReqData:
            date = "2026-04-15"
            start_date = None
            end_date = None
            language = "en"

        ctx = extract_sleep_context(profile, ReqData())
        assert ctx["avg_sleep_quality"] == 85.0  # explicit legacy fallback
        assert ctx.get("avg_onset_min") == 20.0
        # sleep_trend_week / scene.text 为死字段，已从 ctx 移除（趋势由 insight_rules
        # 现算，场景由 weekly_top_scene_* 覆盖，见 llm/analysis.py 注释）
        assert "sleep_trend_week" not in ctx
        assert "scene_text" not in ctx
        assert "user_profile_json" not in ctx
        assert "behaviors" not in ctx

    def test_extract_sleep_context_includes_latest_night_keys(self):
        """prompt 模板引用的 latest_* / scene_name / used_times 必须真实产出。"""
        from llm import extract_sleep_context
        from user_profile import SleepElement

        profile = UserProfile(
            mindora_record={"sleep.scene.rain_wash": [[int(time.time()), 1800]]},
            sleep_data=[
                SleepResult(
                    timestamp=int(time.time()),
                    sleep_quality=88.0,
                    soe=76.0,
                    onset=14.0,
                    first_sleep_time="23:40",
                    hr_before_sleep=62,
                    rr_before_sleep=14,
                    hrv=48,
                    hr_min=54,
                    hr_max=86,
                    sleep_status=[
                        SleepElement(start_time=int(time.time()) - 25200, duration=300, sleep_type="core"),
                        SleepElement(start_time=int(time.time()) - 7200, duration=90, sleep_type="deep"),
                        SleepElement(start_time=int(time.time()) - 3600, duration=60, sleep_type="rem"),
                    ],
                )
            ],
        )

        class ReqData:
            date = "2026-08-07"
            start_date = None
            end_date = None
            language = "en"

        ctx = extract_sleep_context(profile, ReqData())
        assert isinstance(ctx["latest_score"], (int, float))
        assert "latest_soe" not in ctx
        assert ctx["latest_onset_min"] == 14.0
        assert ctx["latest_first_sleep_time"] == "23:40"
        assert ctx["latest_hr_min"] == 54
        assert ctx["latest_hr_max"] == 86
        assert ctx["latest_deep_min"] == 90
        assert ctx["latest_deep_pct"] == 20.0  # 90 / 450 * 100
        assert ctx["scene_name"] == "Rain Wash"
        assert ctx["used_times"] == 1

    def test_all_prompt_templates_render_with_populated_ctx(self):
        """5 个 /analysis prompt 模板不允许引用 ctx 中不存在的 key（None 占位回归）。"""
        import re
        import llm.analysis as analysis_mod

        profile = UserProfile(
            mindora_record={"sleep.scene.rain_wash": [[int(time.time()), 1800]]},
            sleep_data=[SleepResult(timestamp=int(time.time()), sleep_quality=80.0, soe=70.0)],
        )

        class ReqData:
            date = "2026-08-07"
            start_date = "2026-08-01"
            end_date = "2026-08-07"
            language = "en"

        ctx = analysis_mod.extract_sleep_context(profile, ReqData())
        prompts = [
            analysis_mod._prompt_overview(ctx),
            analysis_mod._prompt_sleep_day(ctx),
            analysis_mod._prompt_sleep_week(ctx),
            analysis_mod._prompt_sleep_month(ctx),
            analysis_mod._prompt_explore(ctx, []),
            analysis_mod._prompt_sleep_insight(ctx),
        ]
        for prompt in prompts:
            # 模板里 {ctx.get('xxx')} 引用不存在的 key 时会渲染成字面 None
            assert not re.search(r": None\b", prompt), prompt


# --- LLM dispatch test -------------------------------------------------------

from llm import SleepAnalysisLLM


class TestLLMDispatch:
    """Confirm the dispatch table includes the insight report request type."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_none_when_disabled(self):
        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = None
        assert not llm.enabled
        result = await llm.generate("sleep_insight_report", {}, "en", [])
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_calls_prompt_builder(self, monkeypatch):
        """Patch _call to capture the prompt and return a valid JSON string."""
        captured = {}

        async def fake_call(self, prompt, model, route_name, language="en"):
            captured["prompt"] = prompt
            captured["route"] = route_name
            return json.dumps({
                "greeting": {"title": "Hello", "content": "c", "evidence": [], "action": ""},
            })

        monkeypatch.setattr(SleepAnalysisLLM, "_call", fake_call)

        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = object()  # Force enabled
        llm._last_good = {}  # 路由故障转移状态（__init__ 正常会建，__new__ 绕过需补）

        ctx = {"language": "en", "avg_sleep_quality": 80, "date": "2026-04-15"}
        result = await llm.generate("sleep_insight_report", ctx, "en", [])

        assert result is not None
        assert result["greeting"]["title"] == "Hello"
        assert "80" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_unknown_type_returns_none(self):
        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = object()
        result = await llm.generate("nonexistent_type", {}, "en", [])
        assert result is None

    def test_sync_dispatch_returns_none_when_disabled(self):
        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = None
        assert not llm.enabled
        result = llm.generate_sync("sleep_insight_report", {}, "en", [])
        assert result is None

    def test_sync_dispatch_calls_prompt_builder(self):
        """Patch model.invoke to capture the prompt and return a valid JSON string."""
        captured = {}

        class FakeModel:
            def invoke(self, messages):
                captured["prompt"] = messages[1].content
                return type("Resp", (), {"content": json.dumps({
                    "greeting": {"title": "Sync hello", "content": "c"},
                })})()

        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = FakeModel()
        llm._last_good = {}  # 路由故障转移状态（__init__ 正常会建，__new__ 绕过需补）

        ctx = {"language": "en", "avg_sleep_quality": 80, "date": "2026-04-15"}
        result = llm.generate_sync("sleep_insight_report", ctx, "en", [])

        assert result is not None
        assert result["greeting"]["title"] == "Sync hello"
        assert "80" in captured["prompt"]

    def test_sync_unknown_type_returns_none(self):
        llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
        llm._model = object()
        result = llm.generate_sync("nonexistent_type", {}, "en", [])
        assert result is None


# --- /analysis endpoint stored-report tests ----------------------------------

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import user_server as us


def _make_test_profile():
    return UserProfile(
        basic_info={"name": "alice"},
        sleep_data=[
            SleepResult(
                timestamp=int(time.time()),
                sleep_quality=85.0,
                soe=78.0,
                first_sleep_time="23:10",
                hr_before_sleep=66,
                rr_before_sleep=14,
                avg_heart_rate=60,
                hrv=42,
            )
        ],
        profile=None,
    )


def _build_analysis_app(stored_reports=None, profile=None):
    """Build a minimal aiohttp app wired to the real analysis handler.

    stored_reports: dict[request_type, list[AnalysisTextReport]] 注入 profile.analysis_reports。
    """
    profile = profile or _make_test_profile()
    if stored_reports:
        for key, reports in stored_reports.items():
            profile.analysis_reports[key] = reports
            from analysis_content import explore_input_fingerprint
            for report in reports:
                report.input_fingerprint = explore_input_fingerprint(profile, report.language)

    class FakeUserServ:
        def get_profile(self, uid):
            return profile if uid == "test_uid" else None

        def note_request_meta(self, uid, timezone, language, fill_only=True):
            return None

    class FakeSrv:
        def __init__(self):
            self.user_serv = FakeUserServ()
            self._activity = {}
            self._llm_last_attempt = {}
            self.scheduled = []

        def _parse_for_uid(self, data):
            return data.uid if data.uid else None

        def get_overall_score(self, profile):
            return 85.0

        def _build_analysis_data(self, req, profile):
            from analysis_builders import build_explore, build_sleep_week, build_sleep_month
            builders = {"analysis_explore": build_explore, "analysis_sleep_week": build_sleep_week,
                        "analysis_sleep_month": build_sleep_month}
            if req.request_type in builders:
                return builders[req.request_type](req.data, profile)
            return {}

        def _mark_activity(self, uid):
            self._activity[uid] = time.time()

        def _analysis_needed(self, prof):
            return False, None  # 测试画像无新夜晚，不触发懒生成

        async def _schedule_llm_update_if_needed(self, uid, skip_reco=False, skip_analysis=False, delay=0):
            self.scheduled.append(uid)

    srv = FakeSrv()
    srv.handle_analysis_http = types.MethodType(
        us.UserServer.handle_analysis_http, srv,
    )

    app = web.Application()
    app.router.add_post("/analysis", srv.handle_analysis_http)
    return app


def _analysis_body(**overrides):
    base = {
        "request_type": "analysis_overview",
        "version": "1.0",
        "timestamp": int(time.time()),
        "data": {"uid": "test_uid", "language": "en", "modules": []},
    }
    base.update(overrides)
    return base


def _overview_report(title: str, date: str) -> AnalysisTextReport:
    return AnalysisTextReport(
        request_type="analysis_overview",
        date=date,
        generated_at=int(time.time()) - 100,
        modules={"sleep_insight": {"title": title, "description": "Stored insight."}},
    )


class TestAnalysisStoredReports:
    """Verify /analysis serves LLM text from stored analysis_reports (no sync LLM)."""

    @pytest.mark.asyncio
    async def test_stored_report_text_returned(self):
        today = datetime.date.today().isoformat()
        app = _build_analysis_app(stored_reports={
            "analysis_overview": [_overview_report("Stored Title", today)],
        })
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/analysis", json=_analysis_body())
            assert resp.status == 200
            data = await resp.json()
            assert data["data"]["sleep_insight"]["title"] == "Stored Title"
            assert data["data"]["sleep_insight"]["description"] == "Stored insight."

    @pytest.mark.asyncio
    async def test_no_report_returns_skeleton_only(self):
        app = _build_analysis_app(stored_reports={})
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/analysis", json=_analysis_body())
            assert resp.status == 200
            data = await resp.json()
            assert "sleep_insight" not in data["data"]

    @pytest.mark.asyncio
    async def test_past_date_without_report_falls_back_gracefully(self):
        today = datetime.date.today().isoformat()
        app = _build_analysis_app(stored_reports={
            "analysis_overview": [_overview_report("Stored Title", today)],
        })
        # 请求历史日期，库里没有该日报告 → 返回骨架（不含库存文案）
        body = _analysis_body(data={
            "uid": "test_uid", "language": "en", "modules": [],
            "date": "2020-01-01",
        })
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/analysis", json=body)
            assert resp.status == 200
            data = await resp.json()
            assert "sleep_insight" not in data["data"]

    @pytest.mark.asyncio
    async def test_modules_filter_not_broken_by_stored_report(self):
        """分字段查询：库存报告含有多模块时，只返回请求的模块。"""
        today = datetime.date.today().isoformat()
        report = AnalysisTextReport(
            request_type="analysis_explore",
            date=today,
            generated_at=int(time.time()) - 100,
            modules={
                "score_summary": {"label": "Excellent"},
                "sleep_advice": {"description": "keep bedtime"},
            },
        )
        app = _build_analysis_app(stored_reports={"analysis_explore": [report]})
        body = {
            "request_type": "analysis_explore",
            "version": "1.0",
            "timestamp": int(time.time()),
            "data": {"uid": "test_uid", "language": "en", "date": today,
                     "modules": ["sleep_advice"]},
        }
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/analysis", json=body)
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert data["sleep_advice"]["description"] == "keep bedtime"
            assert set(data) == {"data_ready", "sleep_advice"}


class TestExploreMapsInsight:
    """库存洞察按可见性映射到标准卡片，响应不暴露内部报告结构。"""

    class _FakeData:
        date = datetime.date.today().isoformat()
        language = "en"
        modules = []

    def test_explore_maps_visible_insight_into_cards(self):
        profile = _make_test_profile()
        profile.sleep_insight = SleepInsightReport(
            date=self._FakeData.date,
            generated_at=int(time.time()),
            greeting={"module_id": 0, "title": "Morning", "content": "c"},
            intervention={"module_id": 3, "title": "Night", "content": "c", "visible": False},
        )
        from analysis_content import explore_input_fingerprint
        profile.sleep_insight.input_fingerprint = explore_input_fingerprint(profile, "en")
        server = us.UserServer.__new__(us.UserServer)
        result = server._build_explore(self._FakeData(), profile)

        assert "insight" not in result
        assert result["header_summary"]["intro_text"] == "c"
        assert result["night_fluctuation"]["description"] != "c"

    def test_explore_rule_text_without_report(self):
        server = us.UserServer.__new__(us.UserServer)
        result = server._build_explore(self._FakeData(), _make_test_profile())
        assert "insight" not in result
        assert result["onset_efficiency"]["description"]



class TestDayReportNightAlignedFallback:
    """日视图报告查找：骨架永远展示最新有效夜，文案应与同一夜对齐。

    生成只在新夜晚到达时触发，请求日期漂离报告日期时，最新日报告覆盖的
    正是最新一夜则合并它；有效时间锚定后夜晚/报告相对 anchor（最近有效夜）
    判新鲜——停戴超期锚定最后一夜，不再按无数据降级。
    """

    @staticmethod
    def _ts(days_ago: int, hour: int = 7) -> int:
        from zoneinfo import ZoneInfo
        d = datetime.date.today() - datetime.timedelta(days=days_ago)
        return int(datetime.datetime(d.year, d.month, d.day, hour,
                                     tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    @staticmethod
    def _date(days_ago: int) -> str:
        return (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()

    def _profile(self, night_days_ago: int, report_days_ago: int, llm_used: bool = True) -> UserProfile:
        profile = _make_test_profile()
        profile.sleep_data = [SleepResult(timestamp=self._ts(night_days_ago), sleep_quality=70.0)]
        profile.last_request_timezone = "Asia/Shanghai"
        profile.analysis_reports["analysis_sleep_day"] = [
            AnalysisTextReport(
                request_type="analysis_sleep_day",
                date=self._date(report_days_ago),
                language="en",
                generated_at=self._ts(report_days_ago, 12),
                llm_used=llm_used,
                modules={"sleep_scenarios": {"title": "T", "description": "D"}},
            )
        ]
        from analysis_content import explore_input_fingerprint
        profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
        return profile

    def _find(self, profile, request_date: str, request_type="analysis_sleep_day"):
        from profile_service import UserProfileServ
        return UserProfileServ._find_analysis_report(
            profile, request_type, request_date, None, None,
        )

    def test_report_covering_newest_night_returned_after_date_drift(self):
        """请求日期漂离（请求今天，最新一夜 2 天前）→ 合并覆盖同一夜的报告。"""
        profile = self._profile(night_days_ago=2, report_days_ago=2)
        report = self._find(profile, self._date(0))
        assert report is not None
        assert report.modules["sleep_scenarios"]["description"] == "D"

    def test_stale_night_and_report_still_merged(self):
        """有效时间锚定：最新一夜与报告都在 7 天外（停戴超期）→ 锚定最后一夜，
        报告与最新夜同晚、相对 anchor 新鲜 → 合并。"""
        profile = self._profile(night_days_ago=16, report_days_ago=16)
        report = self._find(profile, self._date(0))
        assert report is not None
        assert report.modules["sleep_scenarios"]["description"] == "D"

    def test_newer_night_without_report_still_rejected(self):
        """新夜晚（1 天前）还没有报告（最新报告 2 天前）→ 不合并，交给懒触发生成。"""
        profile = self._profile(night_days_ago=1, report_days_ago=2)
        assert self._find(profile, self._date(0)) is None

    def test_zero_data_fallback_report_same_night_returned(self):
        """第一晚当天：零数据兜底报告（llm_used=False）与夜晚同日 → 合并（旧行为保留）。"""
        profile = self._profile(night_days_ago=1, report_days_ago=1, llm_used=False)
        assert self._find(profile, self._date(0)) is not None

    def test_stale_exact_match_still_returned(self):
        """有效时间锚定：app 请求 10 天前的日期，报告与 anchor（最近有效夜）同晚 → 返回。"""
        profile = self._profile(night_days_ago=10, report_days_ago=10)
        assert self._find(profile, self._date(10)) is not None

    def test_no_sleep_data_night_fallback_skipped(self):
        """无睡眠数据：夜晚对齐不适用，非当前周期仍落空。"""
        profile = _make_test_profile()
        profile.sleep_data = []
        profile.analysis_reports["analysis_sleep_day"] = [
            AnalysisTextReport(
                request_type="analysis_sleep_day", date=self._date(10),
                generated_at=self._ts(10, 12), llm_used=False,
                modules={"sleep_scenarios": {"title": "T", "description": "D"}},
            )
        ]
        assert self._find(profile, self._date(0)) is None

    def test_week_view_not_affected(self):
        """夜晚对齐只限日视图：周视图无报告仍落空。"""
        profile = self._profile(night_days_ago=2, report_days_ago=2)
        report = self._find(profile, self._date(0), request_type="analysis_sleep_week")
        assert report is None


class TestAnchorStaleUser:
    """停戴用户（最新有效夜 10 天前）：生成与检索全部锚定最近有效夜，不再用 today。"""

    class _FakeLLM:
        enabled = True

        def generate_sync(self, request_type, ctx, language, modules):
            return {"description": f"text {request_type}"}

    @staticmethod
    def _ts(days_ago: int, hour: int = 7) -> int:
        from zoneinfo import ZoneInfo
        d = datetime.date.today() - datetime.timedelta(days=days_ago)
        return int(datetime.datetime(d.year, d.month, d.day, hour,
                                     tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def _profile(self) -> UserProfile:
        profile = _make_test_profile()
        profile.sleep_data = [SleepResult(timestamp=self._ts(10), sleep_quality=80.0)]
        profile.last_request_timezone = "Asia/Shanghai"
        return profile

    def test_insight_and_report_dates_anchor_last_night(self):
        """insight 与 5 能力报告的 date/end_date == 最近有效夜；generated_at 仍是真实生成时刻。"""
        from analysis_content import AnalysisContentService
        serv = AnalysisContentService(lambda: self._FakeLLM())
        profile = self._profile()
        anchor_d = datetime.date.today() - datetime.timedelta(days=10)
        anchor = anchor_d.isoformat()

        insight = serv.calc_sleep_insight("u", profile)
        assert insight.date == anchor
        assert abs(insight.generated_at - int(time.time())) < 60

        reports = serv.calc_analysis_reports("u", profile)
        assert reports["analysis_sleep_day"][0].date == anchor
        assert reports["analysis_overview"][0].date == anchor
        assert reports["analysis_explore"][0].date == anchor
        week = reports["analysis_sleep_week"][0]
        assert (week.start_date, week.end_date) == (
            (anchor_d - datetime.timedelta(days=6)).isoformat(), anchor)
        month = reports["analysis_sleep_month"][0]
        assert (month.start_date, month.end_date) == (
            (anchor_d - datetime.timedelta(days=29)).isoformat(), anchor)

    def test_stored_report_merges_for_anchor_period(self):
        """库存文案能 deep_merge 上：请求窗口 == anchor 周期即视为当前周期；
        洞察相对 anchor 新鲜（停戴超期不再被 7 天门拦掉）。"""
        from analysis_content import AnalysisContentService
        from profile_service import UserProfileServ
        serv = AnalysisContentService(lambda: self._FakeLLM())
        profile = self._profile()
        profile.sleep_insight = serv.calc_sleep_insight("u", profile)
        profile.analysis_reports = serv.calc_analysis_reports("u", profile)
        anchor_d = datetime.date.today() - datetime.timedelta(days=10)
        start = (anchor_d - datetime.timedelta(days=6)).isoformat()
        end = anchor_d.isoformat()

        week = UserProfileServ._find_analysis_report(
            profile, "analysis_sleep_week", None, start, end)
        assert week is not None

        day = UserProfileServ._find_analysis_report(
            profile, "analysis_sleep_day", end, None, None)
        assert day is not None

        assert UserProfileServ._visible_insight_dict(profile) is not None


class TestInsightNightConsistency:
    @staticmethod
    def _profile():
        from analysis_content import AnalysisContentService
        from user_profile import SleepElement
        now = int(time.time())
        profile = UserProfile(
            last_request_timezone="Asia/Shanghai", last_request_language="zh-Hans",
            sleep_data=[SleepResult(timestamp=now - 86400, sleep_quality=80)],
        )
        service = AnalysisContentService(lambda: None)
        old = service.calc_sleep_insight("u", profile)
        old.llm_used = True
        old.generated_at = now
        profile.sleep_insight = old
        profile.sleep_data.append(SleepResult(
            timestamp=now - 60, sleep_quality=80, onset=10, soe=96.4,
            sleep_status=[SleepElement(start_time=now - 3660, duration=60, sleep_type="deep")],
        ))
        return profile, service

    def test_read_replaces_previous_night_with_current_rules_without_mutation(self):
        from analysis_content import AnalysisContentService
        import insight_rules as ir
        from zoneinfo import ZoneInfo
        profile, service = self._profile()
        old = profile.sleep_insight.model_dump()
        result = service._visible_insight_dict(profile)
        assert result["date"] == ir.anchor_date(profile, ZoneInfo("Asia/Shanghai")).isoformat()
        assert result["llm_used"] is False
        assert "10" in result["onset"]["content"]
        assert "未能测得" not in result["onset"]["content"]
        assert profile.sleep_insight.model_dump() == old

    def test_background_regenerates_wrong_date_even_if_generated_after_latest_night(self):
        profile, service = self._profile()
        report = service.calc_sleep_insight("u", profile)
        assert report.date != profile.sleep_insight.date
        assert "10" in report.onset.content
        assert "未能测得" not in report.onset.content

    def test_current_night_report_is_reused(self):
        profile, service = self._profile()
        profile.sleep_insight = service.calc_sleep_insight("u", profile)
        profile.sleep_insight.llm_used = True
        assert service.calc_sleep_insight("u", profile) is profile.sleep_insight
        assert service._visible_insight_dict(profile)["llm_used"] is True


class TestExploreInputCache:
    def test_same_day_correction_regenerates_background_reports(self):
        from analysis_content import AnalysisContentService
        profile = _make_test_profile()
        service = AnalysisContentService(lambda: None)
        profile.sleep_data[0].onset = None
        profile.sleep_insight = service.calc_sleep_insight("u", profile)
        profile.sleep_insight.llm_used = True
        profile.analysis_reports = service.calc_analysis_reports("u", profile)
        old = profile.analysis_reports["analysis_explore"][-1]
        old.llm_used = True
        profile.sleep_data[0].onset = 10
        new = service.calc_analysis_reports("u", profile)["analysis_explore"][-1]
        assert new.input_fingerprint != old.input_fingerprint
        assert "10" in new.modules["onset_efficiency"]["description"]
        assert "10" in service.calc_sleep_insight("u", profile).onset.content


@pytest.mark.asyncio
@pytest.mark.parametrize("request_type,days", [("analysis_sleep_week", 7), ("analysis_sleep_month", 30)])
async def test_period_http_merges_anchor_report_after_request_window_moves(request_type, days):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    end = today - datetime.timedelta(days=3)
    start = end - datetime.timedelta(days=days-1)
    ts = int(datetime.datetime.combine(end, datetime.time(12), tzinfo=datetime.timezone.utc).timestamp())
    profile = UserProfile(sleep_data=[SleepResult(timestamp=ts, sleep_quality=80, onset=10)])
    report = AnalysisTextReport(request_type=request_type, date=end.isoformat(),
        start_date=start.isoformat(), end_date=end.isoformat(), language="en", generated_at=ts,
        modules={"sleep_trends": {"body": "Stored", "description": "Period prose", "score_series": []},
                 "score_summary": {"score": 999}})
    from analysis_content import explore_input_fingerprint
    report.input_fingerprint = explore_input_fingerprint(profile, "en")
    app = _build_analysis_app({request_type: [report]}, profile=profile)
    body = {"request_type": request_type, "version": "1.0", "timestamp": int(time.time()),
            "data": {"uid": "test_uid", "language": "en", "timezone": "UTC",
                     "start_date": (today-datetime.timedelta(days=days-1)).isoformat(),
                     "end_date": today.isoformat(), "modules": ["sleep_trends"]}}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/analysis", json=body)
        assert resp.status == 200
        data = (await resp.json())["data"]
    assert set(data) == {"sleep_trends"}
    assert data["sleep_trends"]["description"] == "Period prose"
    assert data["sleep_trends"]["start_date"] == start.isoformat()
    if days == 30:
                assert data["sleep_trends"]["score_series"][0]["score"] == 80
