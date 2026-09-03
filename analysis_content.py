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
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from analysis_fallback import (
  _canonical_lang as _fallback_lang,
  build_fallback_insight,
  build_fallback_report,
)
from llm import extract_sleep_context
from user_profile import (
  UserProfile, SleepInsightReport, AnalysisTextReport,
  ANALYSIS_REPORT_KEYS, ANALYSIS_REPORT_RETENTION,
  compute_recent_sleep_stats,
)


def _resolve_tz(tz_name: Optional[str]) -> datetime.tzinfo:
  """画像上记录的最近请求时区 → tzinfo；缺失/非法回退 UTC（不告警，读路径太吵）。"""
  if tz_name:
    try:
      return ZoneInfo(tz_name)
    except Exception:
      pass
  return datetime.timezone.utc


def _today_in_tz(tz_name: Optional[str]) -> datetime.date:
  return datetime.datetime.now(_resolve_tz(tz_name)).date()


# LLM 层 _lang_instruction 认的语言码（llm/analysis.py）：zh-Hans/zh-Hant/en/ja/ko/de/fr/it/es/id
_LANG_CANONICAL = {
  "zh-hans": "zh-Hans", "zh-cn": "zh-Hans", "zh-sg": "zh-Hans", "zh": "zh-Hans",
  "zh-hant": "zh-Hant", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh-mo": "zh-Hant",
  "en": "en", "ja": "ja", "ko": "ko", "de": "de", "fr": "fr",
  "it": "it", "es": "es", "id": "id",
}


def _profile_language(profile: UserProfile) -> str:
  """LLM 文案语言：以 last_request_language（请求信封 data.language，App 每次请求
  显式上送的当前界面语言，健康同步 v2 必填）为单一事实源；没有再看 Profile 结构里的
  language（用户资料显式设置；注意其默认值 "zh-CN" 不代表显式设置，只能作参考位），
  最后兜底 en。

  两个来源的写法都可能不标准（Profile.language 缺省 "zh-CN"，LLM 层认 "zh-Hans"），
  统一归一化；归一化不了的值不猜，继续往后兜底（避免 "zh-CN" 静默变英文）。
  """
  candidates = [
    getattr(profile, "last_request_language", None),
    getattr(profile.profile, "language", None) if profile.profile else None,
  ]
  for raw in candidates:
    if not raw:
      continue
    canon = _LANG_CANONICAL.get(raw.strip().lower())
    if canon:
      return canon
    logging.warning("unrecognized profile language %r, trying next source", raw)
  return "en"


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

    复用策略（醒后每日更新机制）：有比 generated_at 更新的 sleep_data 夜晚就重算；
    没有新夜晚时 7 天内复用（慢速刷新兜底），超过 7 天也重算。
    Returns None when there is nothing to store (LLM disabled and no existing report).
    """
    existing = profile.sleep_insight
    now = int(time.time())
    # 文案语言：last_request_language（请求信封）优先，Profile.language 参考，兜底 en
    lang = _profile_language(profile)
    if not profile.sleep_data:
      # 零睡眠记录：模板兜底（通用建议 + Mindora 引导，不烧 LLM）。
      # 已有内容且语言未漂移一律保留——幂等；兜底（llm_used=False）语言漂移则按新语言
      # 重建（模板零成本，切语言即时生效）；LLM 报告无数据无法重建，保留旧语言内容。
      if existing is not None and (
        existing.llm_used or existing.language == _fallback_lang(lang)
      ):
        return existing
      return build_fallback_insight(
        profile, lang,
        _today_in_tz(profile.last_request_timezone).isoformat(), now,
      )
    if existing and existing.generated_at and existing.llm_used \
        and existing.language == lang:
      newest_sleep_ts = max(
        (int(r.timestamp or 0) for r in (profile.sleep_data or [])), default=0,
      )
      has_new_night = newest_sleep_ts > existing.generated_at
      if not has_new_night and now - existing.generated_at < 7 * 86400:
        logging.info(f"sleep_insight still fresh for uid={uid}, skipping LLM")
        return existing
    # existing 是兜底（llm_used=False）或语言已漂移 → 有真实夜晚了，继续走 LLM 替换之

    if not self.llm or not self.llm.enabled:
      return existing

    report_tz = profile.last_request_timezone

    class _FakeData:
      date = _today_in_tz(report_tz).isoformat()
      start_date = None
      end_date = None
      language = lang

    ctx = extract_sleep_context(profile, _FakeData())
    llm_result = self.llm.generate_sync("sleep_insight_report", ctx, lang, [])
    if not llm_result:
      return existing

    report_data: dict[str, Any] = {
      "date": _today_in_tz(report_tz).isoformat(),
      "language": lang,
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
  def _analysis_specs_for_today(tz_name: Optional[str] = None) -> list:
    """5 个分析能力的当前周期定义：(request_type, start_date, end_date, date, modules)。

    日级能力 start_date/end_date 为 None、date=今日；周/月带起止日期。
    "今日"按用户画像最近请求时区计算（缺省 UTC），与每日触发门同口径。
    """
    today = _today_in_tz(tz_name)
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

  def calc_analysis_reports(self, uid: str, profile: UserProfile, language: Optional[str] = None) -> Optional[dict]:
    """异步生成 5 个分析能力的当前周期文案报告，返回更新后的 analysis_reports。

    在 update_profile 的后台 LLM 更新中调用；/analysis 请求时只读库。
    当前周期已有报告则复用（每周期每能力至多一次 LLM 调用）。
    语言：last_request_language（请求信封）优先，Profile.language 参考，兜底 en；周期日期按画像最近请求时区（缺省 UTC）。
    LLM 不可用或全部失败时返回 None（调用方保留旧数据）。
    """
    language = language or _profile_language(profile)
    tz_name = profile.last_request_timezone
    existing = profile.analysis_reports or {}
    reports: dict = {key: list(existing.get(key) or []) for key in ANALYSIS_REPORT_KEYS}
    now = int(time.time())
    changed = False
    specs = self._analysis_specs_for_today(tz_name)

    def _current_report(request_type: str, start_date, end_date, date) -> Optional[AnalysisTextReport]:
      for r in reports[request_type]:
        if start_date is not None:
          if r.start_date == start_date and r.end_date == end_date:
            return r
        elif r.date == date and r.start_date is None:
          return r
      return None

    if not profile.sleep_data:
      # 零睡眠记录：为缺失的当前周期 upsert 模板兜底报告（llm_used=False），不调用 LLM。
      # 幂等只补缺；兜底报告语言漂移则按新语言重建（模板零成本，切语言即时生效）；
      # 真实 LLM 报告无数据无法重建，保留。数据到达后由下方分支替换为真实报告。
      for request_type, start_date, end_date, date, _m in specs:
        current = _current_report(request_type, start_date, end_date, date)
        if current is not None and (
          current.llm_used or current.language == _fallback_lang(language)
        ):
          continue
        report = build_fallback_report(
          request_type, profile, language,
          date=date, start_date=start_date, end_date=end_date, now=now,
        )
        reports[request_type] = self._upsert_analysis_report(
          reports[request_type], report, ANALYSIS_REPORT_RETENTION[request_type],
        )
        changed = True
      return reports if changed else None

    if not self.llm or not self.llm.enabled:
      return None

    for request_type, start_date, end_date, date, modules in specs:
      # 当前周期已有同语言真实报告 → 复用，不重复调 LLM；语言漂移视为过期，重生成。
      # 兜底报告（llm_used=False）不算——有数据了就生成真报告替换之（同周期 upsert 覆盖）
      current = _current_report(request_type, start_date, end_date, date)
      if current is not None and current.llm_used and current.language == language:
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

  def ensure_fallback_content(self, uid: str, profile: UserProfile) -> bool:
    """读路径（/analysis）零睡眠记录用户：即时补齐缺失的兜底内容（模板，无 LLM 成本），
    让首次请求就能见到建议；有真实数据后由 calc_* 的 LLM 分支自然替换。

    幂等只补缺：已有兜底/真实内容一概不动。返回是否有改动（调用方决定是否落库）。
    """
    if profile.sleep_data:
      return False
    changed = False
    insight = self.calc_sleep_insight(uid, profile)
    if insight is not None and insight is not profile.sleep_insight:
      profile.sleep_insight = insight
      changed = True
    reports = self.calc_analysis_reports(uid, profile)
    if reports is not None:
      profile.analysis_reports = reports
      changed = True
    return changed

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
    profile_tz = getattr(profile, "last_request_timezone", None)
    current = {
      rt: (s, e, d)
      for rt, s, e, d, _m in AnalysisContentService._analysis_specs_for_today(profile_tz)
    }
    if request_type in current:
      c_start, c_end, c_date = current[request_type]
      is_current_period = (
        (start_date is not None and start_date == c_start and end_date == c_end)
        or (start_date is None and end_date is None and (date is None or date == c_date))
      )
      if is_current_period:
        latest = reports[-1]
        # 报告自身也必须属于当前周期：否则过期的兜底报告会盖住最新骨架
        # （如 9/2 的请求合并进 8/21 生成的"还没有睡眠数据"兜底文案）
        if latest.start_date is not None:
          if latest.start_date == c_start and latest.end_date == c_end:
            return latest
        elif latest.date == c_date:
          return latest
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
