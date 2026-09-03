"""analysis_builders.py — /analysis 响应骨架组装（纯函数）。

从 user_server.py 拆出（原 UserServer._build_* 方法）。全部为纯函数：
只依赖请求数据 d 与画像 profile。字段口径对齐《服务端分析接口.md》：
  - 数值永远来自真实睡眠数据；算不出真实值的模块/字段直接省略，
    由客户端按 md 的「空值与降级约定」显示 --/空态（不编造假值）
  - 文案（title/description/label）默认空串，LLM 库存报告由
    handle_analysis_http 用 deep_merge 覆盖；md 允许文案降级
"""
import datetime
from typing import Optional

from analysis_content import AnalysisContentService
from user_profile import UserProfile, compute_recent_sleep_stats, short_scene_id


def get_overall_score(profile: UserProfile) -> Optional[float]:
  """计算用户最近7天的平均睡眠质量得分（0-100）"""
  if not profile.sleep_data:
    return None
  recent = profile.sleep_data[-7:]
  scores = [s.sleep_quality for s in recent if s.sleep_quality is not None]
  return round(sum(scores) / len(scores), 2) if scores else None


def _window_avg_score(profile: Optional[UserProfile], start: str, end: str) -> Optional[int]:
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
    if s.sleep_quality is not None and start_d <= datetime.date.fromtimestamp(s.timestamp) <= end_d
  ]
  return int(round(sum(scores) / len(scores))) if scores else None


def _window_avg_onset(profile: Optional[UserProfile], start: str, end: str) -> Optional[int]:
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
    if s.onset is not None and start_d <= datetime.date.fromtimestamp(s.timestamp) <= end_d
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
# insight：6 模块洞察报告，/analysis 是其唯一客户端出口，同样不在 md 模块列表里
RESPONSE_META_KEYS = {"data_ready", "insight"}


def filter_modules(data: dict, modules: list) -> dict:
  if not modules:
    return data
  return {k: v for k, v in data.items() if k in modules or k in RESPONSE_META_KEYS}


def _scene_stats(days: int, profile: Optional[UserProfile]) -> dict:
  """最近 days 天的场景使用统计 {scene_id: {count, total_duration}}（复用 profile_service 口径）。"""
  if not profile:
    return {}
  from profile_service import UserProfileServ  # 延迟 import 避免循环依赖
  return UserProfileServ._calc_scene_stats(profile.mindora_record, days=days)


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
  """周最佳场景的效果分（best_sleep_quality_scene_7d.avg_sleep_quality）。"""
  best = (profile.sleep_analysis or {}).get("best_sleep_quality_scene_7d") if profile else None
  if best and best.get("avg_sleep_quality") is not None:
    return int(round(best["avg_sleep_quality"]))
  return None


def _most_used_scene_entry(profile: Optional[UserProfile]) -> Optional[dict]:
  """场景卡数据来源：优先 7 天最常用，退回全时段最常用；都没有返回 None。"""
  sa = (profile.sleep_analysis or {}) if profile else {}
  return sa.get("most_used_scene_7d") or sa.get("most_used_scene")


def build_overview(d, profile: Optional[UserProfile]) -> dict:
  date = d.date or datetime.date.today().isoformat()
  start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

  result: dict = {}

  # overall_score：最近 7 天平均得分；无数据省略（客户端显示 --）
  score = _window_avg_score(profile, start, date)
  if score is not None:
    result["overall_score"] = {"score": score, "date": date}

  # weekly_best：7 天最常用音频（退回全时段）；效果分取 best_sleep_quality_scene_7d
  scene = _most_used_scene_entry(profile)
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
  date = d.date or datetime.date.today().isoformat()
  latest = profile.sleep_data[-1] if profile and profile.sleep_data else None

  result: dict = {}

  # 顶部睡眠效率评分；无数据省略（客户端显示 --）
  if latest and latest.sleep_quality is not None:
    result["score_summary"] = {"score": int(latest.sleep_quality), "date": date}

  # sleep_scenarios：标题取当天最近使用场景（无则空串），描述文案 LLM 报告覆盖
  stats = compute_recent_sleep_stats(profile, days=1) if profile else {}
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


def build_sleep_week(d, profile: Optional[UserProfile]) -> dict:
  today = datetime.date.today()
  start = d.start_date or (today - datetime.timedelta(days=6)).isoformat()
  end   = d.end_date   or today.isoformat()

  result: dict = {}

  # 周窗口平均评分 + 评价；无数据省略（md：顶部评分由服务端按周窗口数据返回）
  score = _window_avg_score(profile, start, end)
  if score is not None:
    result["score_summary"] = {
      "score": score, "label": _localize(_score_label(score), d.language), "start_date": start, "end_date": end,
    }

  # sleep_trends：纯文案模块（LLM 报告覆盖）
  result["sleep_trends"] = {"body": "", "description": "", "start_date": start, "end_date": end}

  # onset_efficiency：本周最常用场景 + 周平均入睡用时（两者独立填充，任一存在即返回模块）；
  # 场景效果分取 best_sleep_quality_scene_7d
  scene = _most_used_scene_entry(profile)
  avg_onset = _window_avg_onset(profile, start, end)
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
  return filter_modules(result, d.modules)


def build_sleep_month(d, profile: Optional[UserProfile]) -> dict:
  today = datetime.date.today()
  start = d.start_date or (today - datetime.timedelta(days=29)).isoformat()
  end   = d.end_date   or today.isoformat()

  result: dict = {}

  # 月窗口平均评分 + 评价
  score = _window_avg_score(profile, start, end)
  if score is not None:
    result["score_summary"] = {
      "score": score, "label": _localize(_score_label(score), d.language), "start_date": start, "end_date": end,
    }

  # sleep_trends：body/description 为 LLM 文案；score_series 取窗口内真实逐日评分，无数据为空序列
  score_series: list = []
  if profile and profile.sleep_data:
    try:
      start_d = datetime.date.fromisoformat(start)
      end_d = datetime.date.fromisoformat(end)
    except ValueError:
      start_d = end_d = None
    if start_d is not None:
      for sr in profile.sleep_data:
        if sr.sleep_quality is None:
          continue
        day = datetime.date.fromtimestamp(sr.timestamp)
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
  top = _top_scenes(profile, days=30, limit=3)
  avg_onset = _window_avg_onset(profile, start, end)
  if top or avg_onset is not None:
    onset: dict = {"start_date": start, "end_date": end}
    if top:
      onset["scenario_list"] = [name for _sid, name, _c in top]
      onset["description"] = ""
    if avg_onset is not None:
      onset["avg_onset_minutes"] = avg_onset
    result["onset_efficiency"] = onset
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
  date  = d.date or datetime.date.today().isoformat()
  start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

  has_data = profile is not None and bool(profile.sleep_data)
  latest   = profile.sleep_data[-1] if has_data else None
  summaries = latest.sequence_summaries if (latest and latest.sleep_status) else {}

  # 无睡眠数据：只回 data_ready=False + insight（md：客户端进入空态展示）
  if not has_data:
    result = {
      "data_ready": False,
      "insight": AnalysisContentService._visible_insight_dict(profile),
    }
    return filter_modules(result, d.modules)

  result: dict = {"data_ready": True}

  # 顶部摘要：纯文案（LLM 报告覆盖）
  result["header_summary"] = {"intro_text": "", "intro_detail_text": "", "date": date}

  # 顶部总分环：总分=当夜得分；三段分值 = soe / sleep_arch_index / night_var_index（缺哪个省哪个）
  score_summary: dict = {"title": _localize("Sleep Score", d.language), "date": date}
  if latest.sleep_quality is not None:
    score_summary["score"] = int(latest.sleep_quality)
  if latest.soe is not None:
    score_summary["efficiency_score"] = int(latest.soe)
  if latest.sleep_arch_index is not None:
    score_summary["structure_score"] = int(latest.sleep_arch_index)
  if latest.night_var_index is not None:
    score_summary["fluctuation_score"] = int(latest.night_var_index)
  result["score_summary"] = score_summary

  # Sleep Onset Efficiency 卡
  onset: dict = {"label": "", "description": "", "date": date}
  if latest.soe is not None:
    onset["score"] = int(latest.soe)
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
  if latest.sleep_arch_index is not None:
    structure["score"] = int(latest.sleep_arch_index)
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

  # Scene Preference 卡（scene_type 暂无元数据来源，留空待音频库分类表接入）
  scene = _most_used_scene_entry(profile)
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

  # 洞察页 6 模块报告（mindora_advice.md 模块0-5，update_profile 时异步生成，
  # 已过滤 visible=False 模块；无报告则为 None）。/analysis 是其唯一客户端出口。
  result["insight"] = AnalysisContentService._visible_insight_dict(profile)
  return filter_modules(result, d.modules)
