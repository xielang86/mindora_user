"""analysis_builders.py — /analysis 响应骨架组装（纯函数）。

从 user_server.py 拆出（原 UserServer._build_* 方法）。全部为纯函数：
只依赖请求数据 d 与画像 profile。字段口径对齐《服务端分析接口.md》：
  - 数值永远来自真实睡眠数据；算不出真实值的模块/字段直接省略，
    由客户端按 md 的「空值与降级约定」显示 --/空态（不编造假值）
  - Explore 文案由当前规则生成，并按数据指纹复用匹配的库存文案；
    其他视图的库存文案由 handle_analysis_http 合并。
"""
import datetime
import time
from typing import Optional

from analysis_content import AnalysisContentService
from user_profile import UserProfile, compute_recent_sleep_stats, short_scene_id


# ── 数据锚定（有效时间口径）：洞察与分析锚定最近一条有效夜晚的日期 ──────────
# 窗口交集（_clamp_window）以 anchor 为上界：日/周级最近 7 天、月级起点最多 30 天前，
# 均相对 anchor 而非自然今天；停戴超期不再降级为无数据，锚定最后一夜展示。
DAY_LOOKBACK_DAYS = 7
WEEK_LOOKBACK_DAYS = 7
MONTH_LOOKBACK_DAYS = 30


def _anchor_date(profile: Optional[UserProfile], tz: datetime.tzinfo) -> datetime.date:
  """有效时间锚点：最近有效夜日期，无数据回退 today（insight_rules 同口径）。"""
  import insight_rules as ir  # 延迟 import（insight_rules 不反向依赖本模块）
  return ir.anchor_date(profile, tz)


def _fresh_latest(profile: Optional[UserProfile], tz: datetime.tzinfo):
  """最新一个有效夜（时间戳最大）；零记录返回 None，调用方按无数据降级（走 analysis_fallback 兜底）。

  锚定口径：不再因停戴超期（超 lookback）返回 None——只要存在有效夜就返回它。
  tz 保留在签名中与其他读路径函数一致（归日口径在 is_valid_night 之外不依赖时区）。
  """
  if not profile or not profile.sleep_data:
    return None
  import insight_rules as ir  # 延迟 import（insight_rules 不反向依赖本模块）
  best = None
  for r in profile.sleep_data:
    if r is not None and r.timestamp and ir.is_valid_night(r):
      if best is None or r.timestamp > best.timestamp:
        best = r
  return best


def _req_tzinfo(d, profile: Optional[UserProfile]) -> datetime.tzinfo:
  """请求时区优先，画像最近请求时区兜底，缺省 UTC（与生成口径一致）。"""
  tz_name = getattr(d, "timezone", None) or getattr(profile, "last_request_timezone", None)
  if tz_name:
    try:
      from zoneinfo import ZoneInfo
      return ZoneInfo(tz_name)
    except Exception:
      pass
  return datetime.timezone.utc


def _clamp_window(start: str, end: str, today: datetime.date,
                  lookback_days: int) -> tuple[Optional[str], Optional[str]]:
  """请求窗口 ∩ [today-(lookback-1), today]；无交集返回 (None, None)。"""
  try:
    s = datetime.date.fromisoformat(start)
    e = datetime.date.fromisoformat(end)
  except (TypeError, ValueError):
    return None, None
  lo = today - datetime.timedelta(days=lookback_days - 1)
  s, e = max(s, lo), min(e, today)
  if s > e:
    return None, None
  return s.isoformat(), e.isoformat()


def _current_window(start: Optional[str], end: Optional[str], anchor: datetime.date,
                    lookback_days: int) -> tuple[Optional[str], Optional[str]]:
  """Return a stable current-period window anchored to the latest valid night.

  A client normally sends its current calendar period. After a user wakes up,
  that end date advances even though no new sleep result exists; retaining the
  request's moving start would silently shrink the sample. Treat a range that
  reaches past the anchor as the current period and pin both bounds to it.
  Fully historical ranges remain selectable.
  """
  try:
    requested_end = datetime.date.fromisoformat(end) if end else anchor
  except (TypeError, ValueError):
    requested_end = anchor
  if requested_end >= anchor:
    start_d = anchor - datetime.timedelta(days=lookback_days - 1)
    return start_d.isoformat(), anchor.isoformat()
  default_start = requested_end - datetime.timedelta(days=lookback_days - 1)
  return _clamp_window(start or default_start.isoformat(), requested_end.isoformat(), anchor, lookback_days)


def get_overall_score(profile: UserProfile, tz: Optional[datetime.tzinfo] = None) -> Optional[float]:
  """最近 7 天（时间窗，含 anchor 当日）平均睡眠质量得分；窗口内无数据返回 None。"""
  if not profile or not profile.sleep_data:
    return None
  tz = tz or datetime.timezone.utc
  today = _anchor_date(profile, tz)
  lo = today - datetime.timedelta(days=DAY_LOOKBACK_DAYS - 1)
  scores = [
    s.sleep_quality for s in profile.sleep_data
    if s.sleep_quality is not None
    and lo <= datetime.datetime.fromtimestamp(s.timestamp, tz).date() <= today
  ]
  return round(sum(scores) / len(scores), 2) if scores else None


def _window_avg_score(profile: Optional[UserProfile], start: str, end: str,
                      tz: datetime.tzinfo) -> Optional[int]:
  """窗口 [start, end]（yyyy-MM-dd 闭区间）内的平均睡眠得分；无数据返回 None。"""
  if not profile or not profile.sleep_data:
    return None
  try:
    start_d = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
  except ValueError:
    return None
  scores = [
    s.sleep_quality for s in profile.sleep_data
    if s.sleep_quality is not None
    and start_d <= datetime.datetime.fromtimestamp(s.timestamp, tz).date() <= end_d
  ]
  return int(round(sum(scores) / len(scores))) if scores else None


def _window_avg_onset(profile: Optional[UserProfile], start: str, end: str,
                      tz: datetime.tzinfo) -> Optional[int]:
  """窗口 [start, end]（yyyy-MM-dd 闭区间）内的平均入睡用时（分钟）；无数据返回 None。

  onset 只在一部分夜晚可测（会话首段为 awake 才可测，见 sleep_session_builder），
  只对可测夜晚取平均；全部不可测时省略该字段（客户端显示 --）。
  """
  if not profile or not profile.sleep_data:
    return None
  try:
    start_d = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
  except ValueError:
    return None
  onsets = [
    s.onset for s in profile.sleep_data
    if s.onset is not None
    and start_d <= datetime.datetime.fromtimestamp(s.timestamp, tz).date() <= end_d
  ]
  return int(round(sum(onsets) / len(onsets))) if onsets else None


def _score_label(score: int) -> str:
  """评分评价文案（md：由服务端统一返回，不做客户端本地阈值映射）。"""
  return "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"


# ── 骨架文案本地化（md 文本返回约定：文本字段优先按 data.language 返回）──────
# 只覆盖骨架里的确定性标签枚举；LLM 报告文案按请求语言生成后由 deep_merge 覆盖。
# key 为英文枚举值，value 为各语言译文。
# 翻译策略：目前只配置已确认的中文（简/繁）与英文；其余语言（de/es/fr/it/ja/ko/id）
# 暂不凑机器翻译，由 _localize 统一降级英文（md 允许降级英文、结构不变），
# 待翻译确认后按同样格式补进对应条目即可。
_LABEL_I18N: dict[str, dict[str, str]] = {
  "Excellent": {"zh-Hans": "优秀", "zh-Hant": "優秀", "en": "Excellent"},
  "Good":      {"zh-Hans": "良好", "zh-Hant": "良好", "en": "Good"},
  "Fair":      {"zh-Hans": "一般", "zh-Hant": "一般", "en": "Fair"},
  "Sleep Score": {"zh-Hans": "睡眠得分", "zh-Hant": "睡眠得分", "en": "Sleep Score"},
  "Normal":    {"zh-Hans": "正常", "zh-Hant": "正常", "en": "Normal"},
  "High Fluctuation": {"zh-Hans": "波动较大", "zh-Hant": "波動較大", "en": "High Fluctuation"},
  "Brief awakening":      {"zh-Hans": "短暂觉醒", "zh-Hant": "短暫覺醒", "en": "Brief awakening"},
  "Frequent awakenings":  {"zh-Hans": "频繁觉醒", "zh-Hant": "頻繁覺醒", "en": "Frequent awakenings"},
  "Prolonged awakening":  {"zh-Hans": "长时间觉醒", "zh-Hant": "長時間覺醒", "en": "Prolonged awakening"},
  "Moderate awakening":   {"zh-Hans": "中度觉醒", "zh-Hant": "中度覺醒", "en": "Moderate awakening"},
}


def _localize(text: Optional[str], language: str) -> Optional[str]:
  """骨架标签按请求语言取译文；无译文降级英文（md：降级英文但结构不变）。"""
  if not text:
    return text
  entry = _LABEL_I18N.get(text)
  if not entry:
    return text
  return entry.get(language) or entry.get("en", text)


# 响应级 meta 字段：不属于可请求模块，modules 过滤时始终保留。
# data_ready：探索页空态开关（md 的 modules 列表不含它，按列表过滤会把它误删）；
RESPONSE_META_KEYS = {"data_ready"}


def filter_modules(data: dict, modules: list) -> dict:
  if not modules:
    return data
  return {k: v for k, v in data.items() if k in modules or k in RESPONSE_META_KEYS}


def _anchor_ts(profile: Optional[UserProfile]) -> int:
  """场景使用窗口的终点：最近有效夜时间戳，零记录回退当前时刻（与 _anchor_date 同口径）。

  停戴用户的窗口应覆盖最后一夜之前的使用记录；按自然当前时刻开窗会把
  窗内记录全空，与已锚定的日期窗口（_clamp_window）口径不一致。
  """
  latest = _fresh_latest(profile, datetime.timezone.utc)
  return int(latest.timestamp) if latest and latest.timestamp else int(time.time())


def _scene_stats(days: int, profile: Optional[UserProfile]) -> dict:
  """最近 days 天（锚定最近有效夜）的场景使用统计 {scene_id: {count, total_duration}}。"""
  if not profile:
    return {}
  from profile_service import UserProfileServ  # 延迟 import 避免循环依赖
  return UserProfileServ._calc_scene_stats(profile.mindora_record, days=days, end_ts=_anchor_ts(profile))


def _scene_display(scene_id: str) -> tuple[str, str]:
  """scene_id（可带 sleep.scene. 前缀）→ (short_id, 展示名)。"""
  short_id = short_scene_id(scene_id)
  return short_id, short_id.replace("_", " ").title()


def _top_scenes(profile: Optional[UserProfile], days: int, limit: int) -> list[tuple[str, str, int]]:
  """最近 days 天使用次数最多的场景 [(short_id, scene_name, count)]，按次数降序。"""
  stats = _scene_stats(days, profile)
  ranked = sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:limit]
  return [(sid, _scene_display(sid)[1], s["count"]) for sid, s in ranked]


def _best_scene_score(profile: Optional[UserProfile]) -> Optional[int]:
  """周最佳场景的效果分（best_sleep_quality_scene_7d.avg_sleep_quality）。

  快照在 update_profile 时按当时 7 天窗计算并持久化；新鲜度相对 anchor
  （最近有效夜）判定——停戴用户锚定最后一夜，7 天内快照仍随锚定窗口展示，
  更旧的快照按无数据降级，不把过期分数带出来。
  """
  best = (profile.sleep_analysis or {}).get("best_sleep_quality_scene_7d") if profile else None
  if best and best.get("avg_sleep_quality") is not None:
    updated = best.get("updated_at") or 0
    if _anchor_ts(profile) - updated <= WEEK_LOOKBACK_DAYS * 86400:
      return int(round(best["avg_sleep_quality"]))
  return None


def _week_top_scene(profile: Optional[UserProfile]) -> Optional[dict]:
  """最近 7 天（锚定最近有效夜）使用最多的场景 {scene_id, scene_name, count}；窗内无记录返回 None。

  读路径现算（mindora_record 带时间戳），不读持久化快照——快照只在写入时刷新，
  停戴用户的 most_used_scene（全时段口径）会把过期场景漏进响应。
  """
  top = _top_scenes(profile, days=WEEK_LOOKBACK_DAYS, limit=1)
  if not top:
    return None
  sid, name, count = top[0]
  return {"scene_id": sid, "scene_name": name, "count": count}


def build_overview(d, profile: Optional[UserProfile]) -> dict:
  tz = _req_tzinfo(d, profile)
  anchor = _anchor_date(profile, tz)
  date = anchor.isoformat()
  start = (anchor - datetime.timedelta(days=DAY_LOOKBACK_DAYS - 1)).isoformat()

  result: dict = {}

  # 首页总分直接展示最新有效夜的 sleep_quality，不取 7 天平均。
  latest = _fresh_latest(profile, tz)
  if latest and latest.sleep_quality is not None:
    result["overall_score"] = {"score": int(latest.sleep_quality), "date": date}

  # weekly_best：7 天时间窗最常用音频；效果分取 best_sleep_quality_scene_7d（7 天新鲜度）。
  # 窗口内无场景记录则整卡省略——停戴超期不回退全时段快照（新鲜度门）
  scene = _week_top_scene(profile)
  if scene:
    weekly_best = {
      "audio_name": scene["scene_name"],
      "used_times": scene["count"],
      "start_date": start,
      "end_date": date,
    }
    best_score = _best_scene_score(profile)
    if best_score is not None:
      weekly_best["score"] = best_score
    result["weekly_best"] = weekly_best

  # sleep_insight：纯文案模块，默认值空串（LLM 报告覆盖；md 降级约定显示空字符串）
  result["sleep_insight"] = {"title": "", "description": "", "date": date}
  return filter_modules(result, d.modules)


def build_sleep_day(d, profile: Optional[UserProfile]) -> dict:
  tz = _req_tzinfo(d, profile)
  date = d.date or _anchor_date(profile, tz).isoformat()
  # 有效时间锚定：展示最新有效夜；停戴超期不再降级，零记录才按无数据
  # 回空态（title/description 空串，客户端按 md 空态显示，文案走兜底）
  latest = _fresh_latest(profile, tz)

  result: dict = {}

  # 顶部为入睡效率（SOE），不是 sleep_quality；缺失时由客户端显示 --。
  from sleep_session_builder import resolve_sleep_onset_efficiency
  soe = resolve_sleep_onset_efficiency(latest) if latest else None
  if soe is not None:
    result["score_summary"] = {"score": int(soe), "date": date}

  # sleep_scenarios：标题取当天最近使用场景（无则空串），描述文案 LLM 报告覆盖
  stats = compute_recent_sleep_stats(profile, days=1) if (profile and latest) else {}
  result["sleep_scenarios"] = {
    "title": stats.get("recent_scene_title") or "",
    "description": "",
    "date": date,
  }

  # stage_insights：阶段数值客户端本地取 HealthKit，这里只有文案（LLM 报告覆盖）
  result["stage_insights"] = {
    stage: {"description": "", "date": date} for stage in ("awake", "rem", "core", "deep")
  }
  return filter_modules(result, d.modules)


def _fill_period_text(result: dict, d, profile: Optional[UserProfile], start, end, tz, days: int) -> None:
  if profile is None or start is None or end is None:
    return
  text_profile = profile.model_copy(update={"last_request_timezone": str(tz)})
  modules = AnalysisContentService.period_text_modules(
    text_profile, d.language, str(tz), start, end, days, result.get("onset_efficiency"),
  )
  for key, fields in modules.items():
    if key in result:
      result[key].update(fields)


def build_sleep_week(d, profile: Optional[UserProfile]) -> dict:
  tz = _req_tzinfo(d, profile)
  anchor = _anchor_date(profile, tz)
  # 当前周固定锚定最近有效夜；明确选择的历史周仍按请求区间返回。
  eff_start, eff_end = _current_window(d.start_date, d.end_date, anchor, WEEK_LOOKBACK_DAYS)
  start, end = eff_start, eff_end

  result: dict = {}

  # 周窗口平均评分 + 评价；无数据省略（md：顶部评分由服务端按周窗口数据返回）
  score = _window_avg_score(profile, eff_start, eff_end, tz) if eff_start else None
  if score is not None:
    result["score_summary"] = {
      "score": score, "label": _localize(_score_label(score), d.language), "start_date": start, "end_date": end,
    }

  # sleep_trends：纯文案模块（LLM 报告覆盖）
  result["sleep_trends"] = {"body": "", "description": "", "start_date": start, "end_date": end}

  # onset_efficiency：本周（时间窗）最常用场景 + 周平均入睡用时（两者独立填充，任一存在即返回模块）；
  # 场景效果分取 best_sleep_quality_scene_7d（7 天新鲜度）
  scene = _week_top_scene(profile) if eff_start else None
  avg_onset = _window_avg_onset(profile, eff_start, eff_end, tz) if eff_start else None
  if scene or avg_onset is not None:
    onset: dict = {"start_date": start, "end_date": end}
    if scene:
      onset["scenario_name"] = scene["scene_name"]
      onset["used_times"] = scene["count"]
      best_score = _best_scene_score(profile)
      if best_score is not None:
        onset["score"] = best_score
    if avg_onset is not None:
      onset["avg_onset_minutes"] = avg_onset
    result["onset_efficiency"] = onset
  _fill_period_text(result, d, profile, start, end, tz, 7)
  return filter_modules(result, d.modules)


def build_sleep_month(d, profile: Optional[UserProfile]) -> dict:
  tz = _req_tzinfo(d, profile)
  anchor = _anchor_date(profile, tz)
  # 当前月固定锚定最近有效夜；明确选择的历史月仍按请求区间返回。
  eff_start, eff_end = _current_window(d.start_date, d.end_date, anchor, MONTH_LOOKBACK_DAYS)
  start, end = eff_start, eff_end

  result: dict = {}

  # 月窗口平均评分 + 评价
  score = _window_avg_score(profile, eff_start, eff_end, tz) if eff_start else None
  if score is not None:
    result["score_summary"] = {
      "score": score, "label": _localize(_score_label(score), d.language), "start_date": start, "end_date": end,
    }

  # sleep_trends：body/description 为 LLM 文案；score_series 取窗口内真实逐日评分，无数据为空序列
  score_series: list = []
  if profile and profile.sleep_data and eff_start:
    start_d = datetime.date.fromisoformat(eff_start)
    end_d = datetime.date.fromisoformat(eff_end)
    for sr in profile.sleep_data:
      if sr.sleep_quality is None:
        continue
      day = datetime.datetime.fromtimestamp(sr.timestamp, tz).date()
      if start_d <= day <= end_d:
        score_series.append({"date": day.isoformat(), "score": int(sr.sleep_quality)})
  result["sleep_trends"] = {
    "body": "",
    "description": "",
    "score_series": score_series,
    "start_date": start,
    "end_date": end,
  }

  # onset_efficiency：月窗口使用次数 top3 场景 + 月平均入睡用时（任一存在即返回模块）
  top = _top_scenes(profile, days=MONTH_LOOKBACK_DAYS, limit=3) if eff_start else []
  avg_onset = _window_avg_onset(profile, eff_start, eff_end, tz) if eff_start else None
  if top or avg_onset is not None:
    onset: dict = {"start_date": start, "end_date": end}
    if top:
      onset["scenario_list"] = [name for _sid, name, _c in top]
      onset["description"] = ""
    if avg_onset is not None:
      onset["avg_onset_minutes"] = avg_onset
    result["onset_efficiency"] = onset
  _fill_period_text(result, d, profile, start, end, tz, 30)
  return filter_modules(result, d.modules)


def _longest_continuous_sleep_minutes(sleep_status) -> Optional[int]:
  """最长连续睡眠时长（连续非 awake 段的时长之和的最大值），单位分钟。"""
  best = cur = 0.0
  for e in sleep_status or []:
    if e.sleep_type == "awake":
      best, cur = max(best, cur), 0.0
    else:
      cur += e.duration
  best = max(best, cur)
  return int(best) if best > 0 else None


def _latest_intervention(latest) -> Optional[str]:
  """当夜最近一次设备干预动作名（night_events + 各阶段 events 中 event_type=intervention）。"""
  if latest is None:
    return None
  events = list(latest.night_events or [])
  for seq in latest.sleep_status or []:
    events.extend(seq.events or [])
  interventions = [e for e in events if e.event_type == "intervention"]
  if not interventions:
    return None
  last = max(interventions, key=lambda e: e.timestamp)
  return last.detail or None


def build_explore(d, profile: Optional[UserProfile]) -> dict:
  tz = _req_tzinfo(d, profile)
  date  = d.date or _anchor_date(profile, tz).isoformat()
  start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

  # 有效时间锚定：洞察页内容全部来自最新有效夜/最近窗口；停戴超期不再降级，
  # 只有零有效夜才 data_ready=False，客户端进空态（md 空值与降级约定）
  latest   = _fresh_latest(profile, tz)
  summaries = latest.sequence_summaries if (latest and latest.sleep_status) else {}

  # 无有效夜：不返回分数卡，只保留引导与建议。
  if latest is None:
    result = {"data_ready": False}
    if profile is not None:
      from analysis_fallback import build_fallback_report
      fallback = build_fallback_report("analysis_explore", profile, d.language, date=date)
      for key in ("header_summary", "sleep_advice"):
        result[key] = {**fallback.modules[key], "date": date}
    return filter_modules(result, d.modules)

  date = datetime.datetime.fromtimestamp(latest.timestamp, tz).date().isoformat()
  start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()
  result: dict = {"data_ready": True}

  # 顶部摘要：纯文案（LLM 报告覆盖）
  result["header_summary"] = {"intro_text": "", "intro_detail_text": "", "date": date}

  # 顶部总分环：总分=当夜得分；三段分值 = soe / sleep_arch_index / night_var_index（缺哪个省哪个）
  from sleep_session_builder import resolve_sleep_onset_efficiency, resolve_sleep_structure_score
  soe = resolve_sleep_onset_efficiency(latest)
  structure_score = resolve_sleep_structure_score(latest)
  score_summary: dict = {"title": _localize("Sleep Score", d.language), "date": date}
  if latest.sleep_quality is not None:
    score_summary["score"] = int(latest.sleep_quality)
  if soe is not None:
    score_summary["efficiency_score"] = int(soe)
  if structure_score is not None:
    score_summary["structure_score"] = int(structure_score)
  if latest.night_var_index is not None:
    score_summary["fluctuation_score"] = int(latest.night_var_index)
  result["score_summary"] = score_summary

  # Sleep Onset Efficiency 卡
  onset: dict = {"label": "", "description": "", "date": date}
  if soe is not None:
    onset["score"] = int(soe)
  if latest.onset is not None:
    onset["onset_minutes"] = int(latest.onset)
  if latest.first_sleep_time:
    onset["first_sleep_time"] = latest.first_sleep_time
  if latest.hr_before_sleep is not None:
    onset["pre_sleep_heart_rate"] = f"{int(latest.hr_before_sleep)}bpm"
  if latest.rr_before_sleep is not None:
    onset["pre_sleep_respiratory_rate"] = f"{int(latest.rr_before_sleep)}brpm"
  result["onset_efficiency"] = onset

  # Sleep Structure 卡
  structure: dict = {"label": "", "description": "", "date": date}
  if structure_score is not None:
    structure["score"] = int(structure_score)
  continuous = _longest_continuous_sleep_minutes(latest.sleep_status)
  if continuous is not None:
    structure["continuous_sleep_minutes"] = continuous
  tb = summaries.get("time_in_bed") or 0
  if tb:
    structure["rem_percent"]  = f"{round(summaries.get('rem_sleep_duration', 0) / tb * 100, 1)}%"
    structure["deep_percent"] = f"{round(summaries.get('deep_sleep_duration', 0) / tb * 100, 1)}%"
    structure["core_percent"] = f"{round(summaries.get('core_sleep_duration', 0) / tb * 100, 1)}%"
  result["sleep_structure"] = structure

  # Night Fluctuation 卡
  awake_count = summaries.get("night_awake_count", 0)
  fluctuation: dict = {
    "label": _localize("High Fluctuation" if awake_count > 3 else "Normal", d.language),
    "description": "",
    "date": date,
  }
  if latest.night_var_index is not None:
    fluctuation["score"] = int(latest.night_var_index)
  intervention = _latest_intervention(latest)
  if intervention:
    fluctuation["intervention"] = intervention
  if latest.sleep_status:
    fluctuation["awake_count"] = awake_count
    fluctuation["awake_duration_minutes"] = int(summaries.get("night_awake_duration", 0))
    if summaries.get("night_awake_type"):
      fluctuation["awake_type"] = _localize(summaries["night_awake_type"], d.language)
  if latest.hr_min is not None and latest.hr_max is not None:
    fluctuation["heart_rate_range"] = f"{int(latest.hr_min)}-{int(latest.hr_max)}bpm"
  if latest.respiratory_var is not None:
    fluctuation["respiratory_fluctuation"] = f"{int(latest.respiratory_var)}%"
  result["night_fluctuation"] = fluctuation

  # Scene Preference 卡（scene_type 暂无元数据来源，留空待音频库分类表接入）；
  # 7 天时间窗最常用场景，窗内无记录省略整卡（新鲜度门）
  scene = _week_top_scene(profile)
  if scene:
    result["scene_preference"] = {
      "scene_id": scene["scene_id"],
      "scene_name": scene["scene_name"],
      "scene_type": "",
      "description": "",
      "start_date": start,
      "end_date": date,
    }

  # Sleep Advice 卡：纯文案（LLM 报告覆盖）
  result["sleep_advice"] = {"description": "", "date": date}

  # 文案按当前请求语言/时区、当前有效夜映射到文档规定的卡片。
  text_profile = profile.model_copy(update={
    "last_request_timezone": str(tz), "last_request_language": d.language,
  })
  for module, fields in AnalysisContentService.explore_text_modules(text_profile, d.language).items():
    if module in result:
      result[module].update(fields)
  return filter_modules(result, d.modules)
