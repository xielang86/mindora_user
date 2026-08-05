"""analysis_content.py — LLM 分析内容的生成与检索。

从 user_server.py 拆出（原 UserProfileServ 的职责C）：
  - calc_sleep_insight / calc_analysis_reports：update_profile 后台任务中生成，
    按新鲜期/周期复用，结果写入画像（sleep_insight / analysis_reports）
  - _find_analysis_report / _visible_insight_dict：请求时从画像检索已生成内容

通过 get_llm 回调取 LLM 实例（而不是构造时固化），兼容测试在构造后替换 llm 的用法。
"""
import datetime
import logging
import time
from typing import Any, Optional

from pydantic import ValidationError

from llm_service import extract_sleep_context
from user_profile import (
  UserProfile, SleepInsightReport, AnalysisTextReport,
  ANALYSIS_REPORT_KEYS, ANALYSIS_REPORT_RETENTION,
  compute_recent_sleep_stats,
)


class AnalysisContentService:
  def __init__(self, get_llm):
    """get_llm: 无参回调，返回 SleepAnalysisLLM 实例（可为 None）。"""
    self._get_llm = get_llm

  @property
  def llm(self):
    return self._get_llm()

  _INSIGHT_MODULE_KEYS = [
    ("greeting", 0),
    ("onset", 1),
    ("architecture", 2),
    ("intervention", 3),
    ("scene_preference", 4),
    ("micro_education", 5),
  ]

  def calc_sleep_insight(self, uid: str, profile: UserProfile) -> Optional[SleepInsightReport]:
    """Generate the 6-module insight report (mindora_advice.md 模块0-5) via LLM
    and return it for storage in ``profile.sleep_insight``.

    If the stored report is less than 7 days old, reuse it instead of calling
    the LLM again.  Returns None when there is nothing to store (LLM disabled
    and no existing report).
    """
    existing = profile.sleep_insight
    now = int(time.time())
    if existing and existing.generated_at and now - existing.generated_at < 7 * 86400:
      logging.info(f"sleep_insight still fresh for uid={uid}, skipping LLM")
      return existing

    if not self.llm or not self.llm.enabled:
      return existing

    class _FakeData:
      date = datetime.date.today().isoformat()
      start_date = None
      end_date = None
      language = "en"

    ctx = extract_sleep_context(profile, _FakeData())
    llm_result = self.llm.generate_sync("sleep_insight_report", ctx, "en", [])
    if not llm_result:
      return existing

    report_data: dict[str, Any] = {
      "date": datetime.date.today().isoformat(),
      "language": "en",
      "generated_at": now,
      "llm_used": True,
    }
    for key, module_id in self._INSIGHT_MODULE_KEYS:
      m = llm_result.get(key) or {}
      report_data[key] = {
        "module_id": module_id,
        "title": m.get("title", "") or "",
        "content": m.get("content", "") or "",
        "evidence": m.get("evidence", []) or [],
        "action": m.get("action", "") or "",
      }

    try:
      report = SleepInsightReport(**report_data)
    except ValidationError as e:
      logging.error(f"invalid insight report from LLM for uid={uid}: {e}")
      return existing

    # 模块3 展示条件（mindora_advice.md）：近7日存在短暂觉醒才展示，否则前端隐藏
    stats = compute_recent_sleep_stats(profile, days=7)
    if not stats.get("avg_awake_count"):
      report.intervention.visible = False
    return report

  @staticmethod
  def _analysis_specs_for_today() -> list:
    """5 个分析能力的当前周期定义：(request_type, start_date, end_date, date, modules)。

    日级能力 start_date/end_date 为 None、date=今日；周/月带起止日期。
    """
    today = datetime.date.today()
    today_str = today.isoformat()
    week_start = (today - datetime.timedelta(days=6)).isoformat()
    month_start = (today - datetime.timedelta(days=29)).isoformat()
    return [
      ("analysis_overview", None, None, today_str, []),
      ("analysis_sleep_day", None, None, today_str, []),
      ("analysis_explore", None, None, today_str, [
        "header_summary", "score_summary", "onset_efficiency",
        "sleep_structure", "night_fluctuation", "scene_preference", "sleep_advice",
      ]),
      ("analysis_sleep_week", week_start, today_str, today_str, []),
      ("analysis_sleep_month", month_start, today_str, today_str, []),
    ]

  @staticmethod
  def _upsert_analysis_report(reports: list, report: AnalysisTextReport, retention: int) -> list:
    """按周期 upsert（同周期替换），按日期排序并裁剪到保留条数。"""
    def same_period(r: AnalysisTextReport) -> bool:
      if report.start_date is not None:
        return r.start_date == report.start_date and r.end_date == report.end_date
      return r.date == report.date and r.start_date is None

    kept = [r for r in reports if not same_period(r)]
    kept.append(report)
    kept.sort(key=lambda r: (r.end_date or r.date, r.generated_at))
    return kept[-retention:]

  def calc_analysis_reports(self, uid: str, profile: UserProfile, language: str = "en") -> Optional[dict]:
    """异步生成 5 个分析能力的当前周期文案报告，返回更新后的 analysis_reports。

    在 update_profile 的后台 LLM 更新中调用；/analysis 请求时只读库。
    当前周期已有报告则复用（每周期每能力至多一次 LLM 调用）。
    LLM 不可用或全部失败时返回 None（调用方保留旧数据）。
    """
    if not self.llm or not self.llm.enabled:
      return None

    existing = profile.analysis_reports or {}
    reports: dict = {key: list(existing.get(key) or []) for key in ANALYSIS_REPORT_KEYS}
    now = int(time.time())
    changed = False

    for request_type, start_date, end_date, date, modules in self._analysis_specs_for_today():
      # 当前周期已有报告 → 复用，不重复调 LLM
      def _is_current(r: AnalysisTextReport) -> bool:
        if start_date is not None:
          return r.start_date == start_date and r.end_date == end_date
        return r.date == date and r.start_date is None

      if any(_is_current(r) for r in reports[request_type]):
        continue

      class _FakeData:
        pass

      fake = _FakeData()
      fake.date = date
      fake.start_date = start_date
      fake.end_date = end_date
      fake.language = language
      fake.modules = modules

      ctx = extract_sleep_context(profile, fake)
      try:
        llm_result = self.llm.generate_sync(request_type, ctx, language, modules)
      except Exception as e:
        logging.error(f"analysis report generation failed for {request_type}: {e}")
        continue

      if not llm_result:
        continue

      report = AnalysisTextReport(
        request_type=request_type,
        date=date,
        start_date=start_date,
        end_date=end_date,
        language=language,
        generated_at=now,
        llm_used=True,
        modules=llm_result,
      )
      reports[request_type] = self._upsert_analysis_report(
        reports[request_type], report, ANALYSIS_REPORT_RETENTION[request_type],
      )
      changed = True

    return reports if changed else None

  @staticmethod
  def _find_analysis_report(
    profile: Optional[UserProfile],
    request_type: str,
    date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
  ) -> Optional[AnalysisTextReport]:
    """按周期精确查找库存报告；未命中时，若最新一条仍属当前周期则回退到它。"""
    if not profile or not profile.analysis_reports:
      return None
    reports = profile.analysis_reports.get(request_type) or []
    if not reports:
      return None

    for r in reversed(reports):
      if start_date is not None or end_date is not None:
        if r.start_date == start_date and r.end_date == end_date:
          return r
      elif date is not None and r.date == date and r.start_date is None:
        return r

    # 回退：请求的是当前周期（与生成时口径一致），直接用最新一条
    current = {rt: (s, e, d) for rt, s, e, d, _m in AnalysisContentService._analysis_specs_for_today()}
    if request_type in current:
      c_start, c_end, c_date = current[request_type]
      is_current_period = (
        (start_date is not None and start_date == c_start and end_date == c_end)
        or (start_date is None and end_date is None and (date is None or date == c_date))
      )
      if is_current_period:
        return reports[-1]
    return None

  @staticmethod
  def _visible_insight_dict(profile: Optional[UserProfile]) -> Optional[dict]:
    """返回过滤掉 visible=False 模块后的 6 模块洞察报告 dict；无报告返回 None。"""
    report = profile.sleep_insight if profile else None
    if report is None:
      return None
    data = report.model_dump(mode="json")
    for key, _mid in AnalysisContentService._INSIGHT_MODULE_KEYS:
      module = data.get(key)
      if isinstance(module, dict) and module.get("visible") is False:
        data.pop(key)
    return data
