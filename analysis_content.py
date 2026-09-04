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
from llm.analysis import polish_output_ok as _polish_output_ok
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
    """Generate the 6-module insight report (mindora_advice.md 模块0-5).

    生成方式（对照规范 v3 §4）：规则引擎先算结构化结论并填模板（llm_used=False，
    立即可用、LLM 挂了也不影响输出）；LLM 启用时仅在既定事实上润色 title/content
    （evidence/action 保留规则值），校验失败保留模板文案。

    复用策略（醒后每日更新机制）：有比 generated_at 更新的 sleep_data 夜晚就重算；
    没有新夜晚时 7 天内复用（慢速刷新兜底），超过 7 天也重算。
    """
    import insight_rules as ir  # 延迟 import 避免顶层循环依赖

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
        logging.info(f"sleep_insight still fresh for uid={uid}, skipping regeneration")
        return existing
    # existing 是兜底（llm_used=False）或语言已漂移 → 有真实夜晚了，重新生成替换之

    report_tz = profile.last_request_timezone
    today_str = _today_in_tz(report_tz).isoformat()

    # ── 规则引擎：结构化结论 + 模板渲染（兜底文案，LLM 不可用也能输出）──
    _, _, night_conclusions = ir.build_night_conclusions(profile, lang)
    edu = ir.micro_education(profile, lang)
    by_key = {c.key: c for c in night_conclusions}
    by_key[edu.key] = edu

    report_data: dict[str, Any] = {
      "date": today_str, "language": lang, "generated_at": now, "llm_used": False,
    }
    for key, module_id in self._INSIGHT_MODULE_KEYS:
      c = by_key.get(key)
      if c is None:
        report_data[key] = {"module_id": module_id}
        continue
      report_data[key] = {
        "module_id": module_id,
        "title": c.title,
        "content": c.text,
        "evidence": c.evidence or [],
        "action": c.action or "",
        "visible": c.visible,
      }
    try:
      report = SleepInsightReport(**report_data)
    except ValidationError as e:
      logging.error(f"invalid rule-built insight report for uid={uid}: {e}")
      return existing

    # ── LLM 润色（可选）：只重写 title/content，校验失败保留模板 ──
    if self.llm and self.llm.enabled:
      class _FakeData:
        date = today_str
        start_date = None
        end_date = None
        language = lang
      ctx = extract_sleep_context(profile, _FakeData())
      ctx["conclusions"] = [c.to_llm_dict() for c in night_conclusions]
      llm_result = self.llm.generate_sync("sleep_insight_polish", ctx, lang, [])
      if llm_result and _polish_output_ok(llm_result, night_conclusions, lang):
        for key, _mid in self._INSIGHT_MODULE_KEYS:
          m = llm_result.get(key) or {}
          module = getattr(report, key, None)
          if module is None:
            continue
          if m.get("title"):
            module.title = m["title"]
          if m.get("content"):
            module.content = m["content"]
        report.llm_used = True
      elif llm_result:
        logging.info(f"sleep_insight polish rejected by validator for uid={uid}, keeping rule text")

    # 模块3 展示：规则结论为准；旧 avg_awake_count 检查过渡期保留为 OR 兜底
    fluc = by_key.get("intervention")
    if fluc is not None and fluc.visible:
      report.intervention.visible = True
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
        "header_summary", "score_summary", "insight_overview", "onset_efficiency",
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

    # ── 规则引擎：结构化结论（LLM 不可用时模板报告立即可用，llm_used=False）──
    import insight_rules as ir
    tz = _resolve_tz(profile.last_request_timezone)
    data_state = ir.compute_data_state(profile, tz)
    base = ir.compute_baselines(profile, tz)
    _, _, night_conclusions = ir.build_night_conclusions(profile, language)
    mem = ir.insight_memory(profile)
    today = base.today

    # 规则结论 → 报告 modules 的映射（结论 key → (模块, 标题字段, 正文字段)）
    _POLISH_MAP = {
      "analysis_overview": {"home_summary": ("sleep_insight", "title", "description")},
      "analysis_sleep_week": {"trend7": ("sleep_trends", "body", "description")},
      "analysis_sleep_month": {"trend30": ("sleep_trends", "body", "description")},
      "analysis_explore": {
        "greeting": ("header_summary", None, "intro_text"),
        "onset": ("onset_efficiency", "label", "description"),
        "architecture": ("sleep_structure", "label", "description"),
        "intervention": ("night_fluctuation", "label", "description"),
        "scene_preference": ("scene_preference", None, "description"),
        "advice": ("sleep_advice", None, "description"),
      },
    }

    def _rule_group(request_type: str):
      """返回 (该类型的规则结论列表, modules dict)；日视图无规则 → None。"""
      by_key = {c.key: c for c in night_conclusions}
      if request_type == "analysis_overview":
        home = ir.rule_home_summary(night_conclusions, mem, language, today)
        return [home], {"sleep_insight": {"title": home.title, "description": home.text}}
      if request_type == "analysis_sleep_week":
        t = ir.rule_trend(profile, base, language, 7)
        return [t], {"sleep_trends": {"body": t.title, "description": t.text}}
      if request_type == "analysis_sleep_month":
        t = ir.rule_trend(profile, base, language, 30)
        return [t], {"sleep_trends": {"body": t.title, "description": t.text}}
      if request_type == "analysis_explore":
        adv = ir.rule_advice(profile, base, data_state, night_conclusions, language)
        modules = {
          "header_summary": {"intro_text": by_key["greeting"].text, "intro_detail_text": ""},
          "onset_efficiency": {"label": by_key["onset"].title, "description": by_key["onset"].text},
          "sleep_structure": {"label": by_key["architecture"].title, "description": by_key["architecture"].text},
          "night_fluctuation": {"label": by_key["intervention"].title, "description": by_key["intervention"].text},
          "scene_preference": {"description": by_key["scene_preference"].text},
          "sleep_advice": {"description": adv.text},
        }
        return [*night_conclusions, adv], modules
      return None, None  # analysis_sleep_day：本阶段沿用 LLM 路径

    llm_on = bool(self.llm and self.llm.enabled)

    for request_type, start_date, end_date, date, modules in specs:
      # 当前周期已有同语言真实报告 → 复用，不重复调 LLM；语言漂移视为过期，重生成。
      # 规则模板报告（llm_used=False）不算——下次周期自然重算并尝试 LLM 润色
      current = _current_report(request_type, start_date, end_date, date)
      if current is not None and current.llm_used and current.language == language:
        continue

      rule_conclusions, rule_modules = _rule_group(request_type)
      if rule_modules is None:
        # ── 日视图：沿用 LLM 自由生成（本阶段无对应规则）──
        if not llm_on:
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
          request_type=request_type, date=date, start_date=start_date, end_date=end_date,
          language=language, generated_at=now, llm_used=True, modules=llm_result,
        )
        reports[request_type] = self._upsert_analysis_report(
          reports[request_type], report, ANALYSIS_REPORT_RETENTION[request_type])
        changed = True
        continue

      # ── 规则模板报告（兜底，llm_used=False，先 upsert 保证可用）──
      report = AnalysisTextReport(
        request_type=request_type, date=date, start_date=start_date, end_date=end_date,
        language=language, generated_at=now, llm_used=False, modules=rule_modules,
      )

      # ── LLM 润色（可选）：只重写文本字段，校验失败保留模板 ──
      if llm_on:
        class _FakeData:
          pass
        fake = _FakeData()
        fake.date = date
        fake.start_date = start_date
        fake.end_date = end_date
        fake.language = language
        fake.modules = modules
        ctx = extract_sleep_context(profile, fake)
        ctx["conclusions"] = [c.to_llm_dict() for c in rule_conclusions]
        try:
          llm_result = self.llm.generate_sync("insight_polish", ctx, language, modules)
        except Exception as e:
          logging.error(f"insight polish failed for {request_type}: {e}")
          llm_result = None
        if llm_result and _polish_output_ok(llm_result, rule_conclusions, language):
          polish_map = _POLISH_MAP.get(request_type, {})
          for c in rule_conclusions:
            entry = polish_map.get(c.key)
            m = llm_result.get(c.key) or {}
            if not entry or not isinstance(m, dict):
              continue
            mod_key, title_field, text_field = entry
            mod = report.modules.get(mod_key) or {}
            if title_field and m.get("title"):
              mod[title_field] = m["title"]
            if m.get("text"):
              mod[text_field] = m["text"]
            report.modules[mod_key] = mod
          report.llm_used = True
        elif llm_result:
          logging.info(f"insight polish rejected for {request_type}, keeping rule templates")

      reports[request_type] = self._upsert_analysis_report(
        reports[request_type], report, ANALYSIS_REPORT_RETENTION[request_type])
      changed = True

    if changed:
      # 长期记忆：建议历史（7 天同类去重依据）+ 昨日首页主题（避免连续重复）。
      # 直接从规则函数重算（本调用内记忆尚未更新，结果与刚 upsert 的报告一致）
      adv_c = ir.rule_advice(profile, base, data_state, night_conclusions, language)
      home_c = ir.rule_home_summary(night_conclusions, mem, language, today)
      ir.record_generation_memory(
        profile, advice_types=adv_c.advice_types, home_theme=home_c.home_theme or None,
        date_str=today.isoformat(), now=now)

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
