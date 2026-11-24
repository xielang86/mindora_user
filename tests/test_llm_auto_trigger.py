"""醒后每日自动睡眠分析（方案A）的触发机制测试：

- _analysis_needed：最新夜晚日期（按画像最近时区）> 最新日级报告日期才需要分析
- _maybe_schedule_llm_update：三态开关语义（None=自动 / True=跳过 / False=强制）
  + 同夜零散包只触发一次（attempt 标记）+ 防抖延迟
- _note_request_meta / note_request_meta：update 路径覆盖、读路径只补缺
- calc_sleep_insight：有新夜晚即重算；语言跟随画像
- list_uids：排除 "_meta:" 全局 KV
- _analysis_sweeper：给有未分析新夜晚的 uid 补触发
"""
import asyncio
import datetime
import threading
import time

import pytest

from config import Config
from user_profile import AnalysisTextReport, BedtimeAdvice, SleepResult, UserProfile
from user_server import UserServer, UserProfileServ
from analysis_content import explore_input_fingerprint


UTC = datetime.timezone.utc


def _ts(day: str, hour: int = 7) -> int:
  d = datetime.date.fromisoformat(day)
  return int(datetime.datetime(d.year, d.month, d.day, hour, tzinfo=UTC).timestamp())


def _night(day: str) -> SleepResult:
  return SleepResult(timestamp=_ts(day), sleep_quality=80.0)


def _fresh_advice(profile: UserProfile) -> UserProfile:
  profile.bedtime_advice = BedtimeAdvice(content="Keep a calm bedtime routine.", generated_at=int(time.time()))
  return profile


def _server() -> UserServer:
  server = UserServer.__new__(UserServer)
  server.server_semaphore = asyncio.Semaphore(1)
  server._llm_tracker_lock = asyncio.Lock()
  server._llm_update_tracker = {}
  server._llm_tasks = set()
  server._llm_pending = {}
  server._llm_last_attempt = {}
  server._activity = {}
  server._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)
  return server


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  return serv


def _day_report(date: str) -> AnalysisTextReport:
  return AnalysisTextReport(
    request_type="analysis_sleep_day", date=date, generated_at=_ts(date), modules={},
  )


# ── _analysis_needed：新夜晚判定 + 每日门 ─────────────────────────

def test_analysis_needed_without_sleep_data():
  server = _server()
  assert server._analysis_needed(UserProfile(uid_emb=[])) == (False, None)


def test_analysis_needed_first_night_no_report():
  server = _server()
  profile = UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC")
  needed, ts = server._analysis_needed(profile)
  assert needed is True and ts == _ts("2026-08-16")


def test_analysis_needed_same_day_report_already_generated():
  """当夜已生成日报告 → 同夜零散包不再触发（每日门的核心）。"""
  server = _server()
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[_night("2026-08-16")],
    last_request_timezone="UTC",
    analysis_reports={"analysis_sleep_day": [_day_report("2026-08-16")]},
  )
  profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
  needed, _ = server._analysis_needed(profile)
  assert needed is False


def test_analysis_needed_new_night_after_report():
  server = _server()
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[_night("2026-08-16"), _night("2026-08-17")],
    last_request_timezone="UTC",
    analysis_reports={"analysis_sleep_day": [_day_report("2026-08-16")]},
  )
  needed, ts = server._analysis_needed(profile)
  assert needed is True and ts == _ts("2026-08-17")


def test_analysis_needed_respects_profile_timezone():
  """醒在 UTC 08-16 23:30 = 上海 08-17 07:30：上海口径下需要 08-17 的报告。"""
  server = _server()
  ts = _ts("2026-08-16", 23) + 1800  # 23:30 UTC
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=ts, sleep_quality=80.0)],
    last_request_timezone="Asia/Shanghai",
    analysis_reports={"analysis_sleep_day": [_day_report("2026-08-16")]},
  )
  needed, _ = server._analysis_needed(profile)
  assert needed is True  # 上海已是 08-17，08-16 的报告不算数

  profile.last_request_timezone = "UTC"
  profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
  needed, _ = server._analysis_needed(profile)
  assert needed is False  # UTC 还是 08-16，报告已覆盖


def test_analysis_needed_without_timezone_falls_back_utc():
  server = _server()
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[_night("2026-08-16")],
    analysis_reports={"analysis_sleep_day": [_day_report("2026-08-16")]},
  )
  profile.analysis_reports["analysis_sleep_day"][0].input_fingerprint = explore_input_fingerprint(profile, "en")
  needed, _ = server._analysis_needed(profile)
  assert needed is False


# ── _maybe_schedule_llm_update：三态开关 + 节流 ───────────────────

@pytest.fixture
def recorder(monkeypatch):
  """把调度层替换为记录器，返回 (server, calls)。"""
  calls = []

  async def fake_schedule(uid, skip_reco=False, skip_analysis=False, delay=0):
    calls.append({"uid": uid, "skip_reco": skip_reco, "skip_analysis": skip_analysis, "delay": delay})

  return calls, fake_schedule


async def test_auto_trigger_schedules_with_debounce(monkeypatch, recorder):
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_DEBOUNCE_SECONDS", 600)
  server._activity["u1"] = time.time()  # 活跃门：窗口内有信号

  profile = UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC")
  await server._maybe_schedule_llm_update("u1", profile, None, None)

  assert len(calls) == 1
  assert calls[0]["delay"] == 600                       # 自动路径带防抖
  assert calls[0]["skip_reco"] is False and calls[0]["skip_analysis"] is False


async def test_repeated_short_plays_refresh_recommendations_without_a_new_night(monkeypatch, recorder):
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "ENABLE_SLEEP_RECO", True)
  now = int(time.time())
  cmd = "sleep.pure_music.ocean_wave"
  profile = _fresh_advice(UserProfile(
    standard_sop_reco=[cmd],
    sop_recommendation_details={"generated_at": now - 7 * 3600},
    behaviors={"plays": [[now - offset, {"cmd": cmd, "event": "sop_start", "duration": 15}]
                        for offset in (60, 120)]},
  ))

  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert len(calls) == 1
  assert calls[0]["skip_reco"] is False and calls[0]["skip_analysis"] is True

  await server._maybe_schedule_llm_update("u1", profile, True, None)
  profile.sop_recommendation_details["generated_at"] = now
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert len(calls) == 1


async def test_auto_trigger_blocked_for_inactive_user(monkeypatch, recorder):
  """收窄活跃门：无 /analysis、无 plays → 有新夜晚也不预生成。"""
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  profile = _fresh_advice(UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert calls == []


async def test_auto_trigger_active_via_persisted_plays(monkeypatch, recorder):
  """活跃门的持久化来源：画像里有窗口内的 plays 事件（重启后内存表丢了也能判）。"""
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  profile = UserProfile(
    uid_emb=[],
    sleep_data=[_night("2026-08-16")],
    behaviors={"plays": [[int(time.time()) - 3600, {"cmd": "sleep.scene.kyoto_forest", "event": "sop_start"}]]},
    last_request_timezone="UTC",
  )
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert len(calls) == 1


async def test_auto_trigger_stale_plays_do_not_count(monkeypatch, recorder):
  """窗口外的 plays（3 天前）不算活跃。"""
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS", 86400)

  profile = _fresh_advice(UserProfile(
    uid_emb=[],
    sleep_data=[_night("2026-08-16")],
    behaviors={"plays": [[int(time.time()) - 3 * 86400, {"cmd": "x", "event": "sop_start"}]]},
    last_request_timezone="UTC",
  ))
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert calls == []


async def test_auto_trigger_same_night_fragmented_packets_only_once(monkeypatch, recorder):
  """同夜的零散包：第一次触发后，attempt 标记挡住后续包。"""
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  server._activity["u1"] = time.time()

  profile = _fresh_advice(UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  await server._maybe_schedule_llm_update("u1", profile, None, None)  # 同夜第二个包
  assert len(calls) == 1


async def test_auto_trigger_again_for_new_night(monkeypatch, recorder):
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  server._activity["u1"] = time.time()

  await server._maybe_schedule_llm_update(
    "u1", UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"), None, None)
  await server._maybe_schedule_llm_update(
    "u1", UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16"), _night("2026-08-17")], last_request_timezone="UTC"), None, None)
  assert len(calls) == 2  # 新的一天 → 再触发一次


async def test_explicit_skip_true_suppresses_auto(monkeypatch, recorder):
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  profile = _fresh_advice(UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))
  await server._maybe_schedule_llm_update("u1", profile, True, True)
  assert calls == []


async def test_explicit_force_runs_immediately_without_sleep_data(monkeypatch, recorder):
  """显式 False = 客户端强制：无 sleep_data 也跑、不带防抖。"""
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  await server._maybe_schedule_llm_update("u1", UserProfile(uid_emb=[]), False, None)
  assert len(calls) == 1
  assert calls[0]["delay"] == 0
  assert calls[0]["skip_reco"] is False and calls[0]["skip_analysis"] is True  # 未强制的分析路不跑


async def test_auto_trigger_disabled_only_explicit_force_works(monkeypatch, recorder):
  calls, fake = recorder
  server = _server()
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", False)

  profile = _fresh_advice(UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))
  await server._maybe_schedule_llm_update("u1", profile, None, None)
  assert calls == []
  await server._maybe_schedule_llm_update("u1", profile, None, False)
  assert len(calls) == 1


async def test_update_profile_marks_activity_on_plays_batch(monkeypatch, recorder):
  """handle_update_profile：本批含 plays → 记活跃 → 自动路径通过活跃门。"""
  from user_profile import ProfileData, ProfileRequest

  calls, fake = recorder
  server = _server()
  server.debug_uid_set = {"test_debug_user_001"}
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_DEBOUNCE_SECONDS", 600)

  merged = _fresh_advice(UserProfile(
    uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))

  class FakeUserServ:
    def update_profile_basic(self, uid, new_profile, health_schema_version=None, timezone=None, language=None):
      return True

    def get_profile(self, uid):
      return merged

  server.user_serv = FakeUserServ()

  plays_batch = UserProfile(
    uid_emb=[],
    behaviors={"plays": [[int(time.time()), {"cmd": "sleep.scene.kyoto_forest", "event": "sop_start"}]]},
  )
  request = ProfileRequest(
    request_type="update_profile",
    timestamp=int(time.time()),
    version="1.0",
    data=ProfileData(uid="test_debug_user_001", user_profile=plays_batch),
  )
  resp = await server.handle_update_profile(request)
  assert resp.code == 0
  assert "test_debug_user_001" in server._activity   # plays 批次记了活跃
  assert len(calls) == 1 and calls[0]["delay"] == 600  # 活跃门通过 → 自动调度


async def test_update_profile_without_plays_stays_inactive(monkeypatch, recorder):
  """纯 sleep_data 同步（无 plays、无 /analysis 记录）→ 活跃门拦截，不预生成。"""
  from user_profile import ProfileData, ProfileRequest

  calls, fake = recorder
  server = _server()
  server.debug_uid_set = {"test_debug_user_001"}
  monkeypatch.setattr(server, "_schedule_llm_update_if_needed", fake)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  merged = _fresh_advice(UserProfile(
    uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC"))

  class FakeUserServ:
    def update_profile_basic(self, uid, new_profile, health_schema_version=None, timezone=None, language=None):
      return True

    def get_profile(self, uid):
      return merged

  server.user_serv = FakeUserServ()

  request = ProfileRequest(
    request_type="update_profile",
    timestamp=int(time.time()),
    version="1.0",
    data=ProfileData(uid="test_debug_user_001", user_profile=UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")])),
  )
  resp = await server.handle_update_profile(request)
  assert resp.code == 0
  assert server._activity == {}
  assert calls == []  # 不活跃 → 不预生成（等用户打开分析页时读路径懒触发）


async def test_schedule_dedupes_pending_task():
  """防抖窗口内已有 pending 任务 → 不重复调度（任务运行时才读最新画像）。"""
  server = _server()

  async def never():
    await asyncio.sleep(3600)

  server._llm_pending["u1"] = asyncio.ensure_future(never())
  await server._schedule_llm_update_if_needed("u1", delay=600)
  assert server._llm_tasks == set()  # 没有新任务
  server._llm_pending["u1"].cancel()
  try:
    await server._llm_pending["u1"]
  except asyncio.CancelledError:
    pass


# ── 请求环境登记 ─────────────────────────────────────────────────

def test_note_request_meta_update_path_overwrites():
  profile = UserProfile(uid_emb=[], last_request_timezone="UTC", last_request_language="en")
  changed = UserProfileServ._note_request_meta(profile, "Asia/Shanghai", "zh-Hans")
  assert changed is True
  assert profile.last_request_timezone == "Asia/Shanghai"
  assert profile.last_request_language == "zh-Hans"
  assert profile.last_request_at is not None


def test_note_request_meta_fill_only_does_not_overwrite():
  """读路径（AnalysisData 有默认值 UTC/en）不能把 update 路径记下的真实值冲掉。"""
  profile = UserProfile(
    uid_emb=[], last_request_timezone="Asia/Shanghai", last_request_language="zh-Hans")
  changed = UserProfileServ._note_request_meta(profile, "UTC", "en", fill_only=True)
  assert changed is False
  assert profile.last_request_timezone == "Asia/Shanghai"
  # 缺省时补缺
  empty = UserProfile(uid_emb=[])
  assert UserProfileServ._note_request_meta(empty, "UTC", "en", fill_only=True) is True
  assert empty.last_request_timezone == "UTC"


def test_note_request_meta_no_change_no_write():
  serv = _serv()
  profile = UserProfile(uid_emb=[], last_request_timezone="UTC", last_request_language="en")
  saved = []
  serv.get_profile = lambda uid: profile
  serv.save_profile = lambda uid, p: saved.append(uid)
  serv.note_request_meta("u1", "UTC", "en")
  assert saved == []  # 无变化不落盘
  serv.note_request_meta("u1", "Asia/Shanghai", None)  # fill_only 默认 True 但旧值是 UTC？
  # UTC 是"缺省语义"，显式换时区仍应覆盖——fill_only 只挡 None/缺省，挡不住不同值
  # 这里 Asia/Shanghai != UTC 且非 fill_only 语义冲突：fill_only=True 时只在缺省时写
  assert saved == []


def test_list_uids_excludes_meta_keys():
  serv = _serv()
  serv.storage_mode = "txt_json"
  serv.text_profiles = {"u1": {}, "u2": {}, "_meta:msg:p1": {}}
  assert sorted(serv.list_uids()) == ["u1", "u2"]


# ── insight 新鲜期：有新夜晚即重算 ────────────────────────────────

def test_insight_regenerates_when_new_night_arrived(monkeypatch):
  serv = _serv()
  from llm import SleepAnalysisLLM
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None

  now = int(time.time())
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=now - 3600, sleep_quality=80.0)],  # 1h 前醒的
    last_request_language="zh-Hans",
  )
  from user_profile import SleepInsightReport
  profile.sleep_insight = SleepInsightReport(
    date="2026-08-15", language="en", generated_at=now - 2 * 86400,  # 前天生成的（7天内）
    greeting={"module_id": 0, "title": "Old", "content": "old"},
  )

  captured = {}

  def fake_generate(request_type, ctx, language, modules):
    captured["language"] = language
    # 润色校验器要求：key 集合与规则结论一一对应，且 zh 输出必须含 CJK（polish_output_ok）
    return {
      c["key"]: {"title": "新夜晚", "content": "新内容", "evidence": [], "action": ""}
      for c in ctx.get("conclusions", [])
    }

  monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
  monkeypatch.setattr(serv.llm, "enabled", True)
  monkeypatch.setattr("analysis_content._polish_output_ok", lambda *_: True)

  result = serv.calc_sleep_insight("u1", profile)
  assert result.greeting.title == "新夜晚"          # 新夜晚 → 7天内也重算
  assert captured["language"] == "zh-Hans"        # 语言跟随画像
  assert result.language == "zh-Hans"


def test_insight_reused_when_no_new_night(monkeypatch):
  serv = _serv()
  from llm import SleepAnalysisLLM
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None

  now = int(time.time())
  profile = UserProfile(
    uid_emb=[],
    sleep_data=[SleepResult(timestamp=now - 2 * 86400, sleep_quality=80.0)],
  )
  from user_profile import SleepInsightReport
  profile.sleep_insight = SleepInsightReport(
    date=datetime.datetime.fromtimestamp(now - 2 * 86400, datetime.timezone.utc).date().isoformat(),
    language="en", generated_at=now - 3600,  # 生成于最新夜晚之后
    input_fingerprint=__import__("analysis_content").explore_input_fingerprint(profile, "en"),
    greeting={"module_id": 0, "title": "Fresh", "content": "fresh"},
  )

  def fail_llm(*args, **kwargs):
    raise AssertionError("无新夜晚且在 7 天内 → 不应调 LLM")

  monkeypatch.setattr(serv.llm, "generate_sync", fail_llm)
  monkeypatch.setattr(serv.llm, "enabled", True)

  result = serv.calc_sleep_insight("u1", profile)
  assert result.greeting.title == "Fresh"


# ── 文案语言优先级链：请求信封 last_request_language → Profile.language → en ──

def test_profile_language_prefers_request_envelope():
  from analysis_content import _profile_language
  from user_profile import Profile
  # 请求信封语言（App 当前界面语言，v2 必填）优先；Profile.language 可能是没碰过的
  # 默认值 "zh-CN"，不能压住真实界面语言
  p = UserProfile(uid_emb=[], profile=Profile(language="zh-CN"), last_request_language="en")
  assert _profile_language(p) == "en"
  # 信封缺失 → Profile.language 作参考位（zh-TW 归一化为 LLM 层认的 zh-Hant）
  p = UserProfile(uid_emb=[], profile=Profile(language="zh-TW"))
  assert _profile_language(p) == "zh-Hant"
  # 英文
  p = UserProfile(uid_emb=[], profile=Profile(language="en"))
  assert _profile_language(p) == "en"


def test_profile_language_falls_back_to_request_language():
  from analysis_content import _profile_language
  from user_profile import Profile
  # Profile.language 空串 → 看请求语言
  p = UserProfile(uid_emb=[], profile=Profile(language=""), last_request_language="ja")
  assert _profile_language(p) == "ja"
  # 无 Profile → 看请求语言
  p = UserProfile(uid_emb=[], last_request_language="zh-Hans")
  assert _profile_language(p) == "zh-Hans"
  # 都没有 → en
  assert _profile_language(UserProfile(uid_emb=[])) == "en"


def test_profile_language_unrecognized_falls_through():
  from analysis_content import _profile_language
  from user_profile import Profile
  # 归一化不了的值不猜，继续往后兜底（防止 "xx" 静默变英文又挡住请求语言）
  p = UserProfile(uid_emb=[], profile=Profile(language="klingon"), last_request_language="fr")
  assert _profile_language(p) == "fr"
  p = UserProfile(uid_emb=[], profile=Profile(language="klingon"))
  assert _profile_language(p) == "en"


def test_insight_language_prefers_request_envelope(monkeypatch):
  """calc_sleep_insight 端到端：请求信封语言覆盖 Profile.language。"""
  serv = _serv()
  from llm import SleepAnalysisLLM
  serv.llm = SleepAnalysisLLM.__new__(SleepAnalysisLLM)
  serv.llm._model = None

  from user_profile import Profile
  profile = UserProfile(
    uid_emb=[],
    profile=Profile(language="zh-CN"),          # 资料字段（可能是默认值）
    last_request_language="en",                  # 最近一次请求信封是英文 → 以此为准
    sleep_data=[SleepResult(timestamp=int(time.time()) - 3600, sleep_quality=80.0)],
  )

  captured = {}

  def fake_generate(request_type, ctx, language, modules):
    captured["language"] = language
    return {"greeting": {"title": "t", "content": "c", "evidence": [], "action": ""}}

  monkeypatch.setattr(serv.llm, "generate_sync", fake_generate)
  monkeypatch.setattr(serv.llm, "enabled", True)

  result = serv.calc_sleep_insight("u1", profile)
  assert captured["language"] == "en"
  assert result.language == "en"


# ── 兜底扫描 ─────────────────────────────────────────────────────

async def test_sweeper_catches_up_uid_with_unanalyzed_night(monkeypatch):
  server = _server()

  class FakeLLM:
    enabled = True

  server.llm = FakeLLM()

  profile = UserProfile(uid_emb=[], sleep_data=[_night("2026-08-16")], last_request_timezone="UTC")

  class FakeUserServ:
    def list_uids(self):
      return ["u1", "u2"]

    def get_profile(self, uid):
      return profile if uid == "u1" else UserProfile(uid_emb=[])  # u2 无数据

    def _resolve_tz(self, tz):
      return UserProfileServ._resolve_tz(tz)

  server.user_serv = FakeUserServ()
  server._activity["u1"] = time.time()  # u1 活跃；u2 不活跃但本来也没数据

  caught = []

  async def fake_maybe(uid, prof, skip_reco, skip_analysis):
    caught.append(uid)
    raise asyncio.CancelledError  # 扫到第一个就打断循环

  monkeypatch.setattr(server, "_maybe_schedule_llm_update", fake_maybe)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)
  monkeypatch.setattr(Config, "LLM_ANALYSIS_SWEEP_SECONDS", 0)

  with pytest.raises(asyncio.CancelledError):
    await server._analysis_sweeper()
  assert caught == ["u1"]


async def test_sweeper_skips_when_llm_disabled(monkeypatch):
  server = _server()

  class FakeLLM:
    enabled = False

  server.llm = FakeLLM()

  class FakeUserServ:
    def list_uids(self):
      raise AssertionError("LLM 未启用不应扫描")

  server.user_serv = FakeUserServ()
  monkeypatch.setattr(Config, "LLM_ANALYSIS_AUTO_TRIGGER", True)

  iterations = 0
  real_sleep = asyncio.sleep

  async def fake_sleep(seconds):
    nonlocal iterations
    iterations += 1
    if iterations >= 2:
      raise asyncio.CancelledError
    await real_sleep(0)

  monkeypatch.setattr(asyncio, "sleep", fake_sleep)
  with pytest.raises(asyncio.CancelledError):
    await server._analysis_sweeper()
