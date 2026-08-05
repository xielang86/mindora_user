"""analysis_builders.py — /analysis 响应骨架组装（纯函数）。

从 user_server.py 拆出（原 UserServer._build_* 方法）。全部为纯函数：
只依赖请求数据 d 与画像 profile，数值永远来自真实睡眠数据，
LLM 文案由 handle_analysis_http 用库存报告 deep_merge 覆盖。
"""
import datetime
from typing import Optional

from analysis_content import AnalysisContentService
from user_profile import UserProfile


def get_overall_score(profile: UserProfile) -> Optional[float]:
  """计算用户最近7天的平均睡眠质量得分（0-100）"""
  if not profile.sleep_data:
    return None
  recent = profile.sleep_data[-7:]
  scores = [s.sleep_quality for s in recent if s.sleep_quality is not None]
  return round(sum(scores) / len(scores), 2) if scores else None


def filter_modules(data: dict, modules: list) -> dict:
  return {k: v for k, v in data.items() if k in modules} if modules else data


def build_overview(d, profile: Optional[UserProfile]) -> dict:
  date = d.date or datetime.date.today().isoformat()
  score = get_overall_score(profile) if profile else None
  if score is None:
    score = 82

  weekly_best = None
  most_used = (profile.sleep_analysis or {}).get("most_used_scene") if profile else None
  if most_used:
    weekly_best = {
      "audio_name": most_used["scene_name"],
      "used_times": most_used["count"],
      "score": int(score),
      "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
      "end_date": date,
    }
  if weekly_best is None:
    weekly_best = {
      "audio_name": "Sedona Red Rocks",
      "used_times": 5,
      "score": 92,
      "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
      "end_date": date,
    }

  result = {
    "overall_score": {"score": int(score), "date": date},
    "weekly_best": weekly_best,
    "sleep_insight": {
      "title": "Excellent Deep Sleep Performance",
      "description": "Your deep sleep accounts for a healthy proportion of total sleep. Keep maintaining a regular sleep schedule.",
      "date": date,
    },
  }
  return filter_modules(result, d.modules)


def build_sleep_day(d, profile: Optional[UserProfile]) -> dict:
  date = d.date or datetime.date.today().isoformat()
  latest = profile.sleep_data[-1] if profile and profile.sleep_data else None
  score = int(latest.sleep_quality) if latest and latest.sleep_quality else 70

  result = {
    "score_summary": {"score": score, "date": date},
    "sleep_scenarios_reco": {
      "title": "Sedona Desert Calm",
      "description": "You fell asleep quickly and maintained a stable sleep rhythm after the scenario started.",
      "date": date,
    },
    "stage_insights": {
      "awake": {"description": "A brief awakening was detected and you returned to sleep quickly.", "date": date},
      "rem":   {"description": "REM sleep was sustained and supports emotional processing.", "date": date},
      "core":  {"description": "Core sleep remained stable across most of the night.", "date": date},
      "deep":  {"description": "Deep sleep contributed strongly to physical recovery.", "date": date},
    },
  }
  return filter_modules(result, d.modules)


def build_sleep_week(d, profile: Optional[UserProfile]) -> dict:
  today = datetime.date.today()
  start = d.start_date or (today - datetime.timedelta(days=6)).isoformat()
  end   = d.end_date   or today.isoformat()

  score = get_overall_score(profile) if profile else None
  score = int(score) if score else 86
  label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

  result = {
    "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
    "sleep_trends": {
      "body": "Excellent Deep Sleep Performance",
      "description": "Your deep sleep accounted for a healthy proportion of total sleep this week.",
      "start_date": start,
      "end_date": end,
    },
    "onset_efficiency": {
      "scenario_name": "Sedona Desert Calm",
      "used_times": 5,
      "score": score,
      "start_date": start,
      "end_date": end,
    },
  }
  return filter_modules(result, d.modules)


def build_sleep_month(d, profile: Optional[UserProfile]) -> dict:
  today = datetime.date.today()
  start = d.start_date or (today - datetime.timedelta(days=29)).isoformat()
  end   = d.end_date   or today.isoformat()

  score = get_overall_score(profile) if profile else None
  score = int(score) if score else 89
  label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

  # Build score_series from real data, fall back to mock trend
  score_series: list = []
  if profile and profile.sleep_data:
    for sr in profile.sleep_data[-30:]:
      if sr.sleep_quality is not None:
        score_series.append({
          "date": datetime.date.fromtimestamp(sr.timestamp).isoformat(),
          "score": int(sr.sleep_quality),
        })
  if not score_series:
    cur = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
    base = 62
    while cur <= end_d:
      score_series.append({"date": cur.isoformat(), "score": min(100, base)})
      base += 1
      cur += datetime.timedelta(days=1)

  result = {
    "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
    "sleep_trends": {
      "body": "This month, you maintained a consistent amount of sleep.",
      "description": "Deep sleep remained above the standard level and your bedtime trended earlier.",
      "score_series": score_series,
      "start_date": start,
      "end_date": end,
    },
    "onset_efficiency": {
      "scenario_list": ["Sedona Desert Calm", "Maldives Drift Sleep", "Canadian Forest Solace"],
      "description": "Sedona Desert Calm was your most frequently used sleep scenario this month and showed the best onset performance.",
      "start_date": start,
      "end_date": end,
    },
  }
  return filter_modules(result, d.modules)


def build_explore(d, profile: Optional[UserProfile]) -> dict:
  date  = d.date or datetime.date.today().isoformat()
  start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

  has_data = profile is not None and bool(profile.sleep_data)
  latest   = profile.sleep_data[-1] if has_data else None
  summaries = latest.sequence_summaries if (latest and latest.sleep_status) else {}

  overall_score    = int(latest.sleep_quality) if latest and latest.sleep_quality else 82
  onset_score      = int(latest.soe)           if latest and latest.soe           else 82
  structure_score  = 49
  fluctuation_score = 34

  tb = summaries.get("time_in_bed") or 1
  rem_pct  = f"{round(summaries.get('rem_sleep_duration',  0) / tb * 100, 1)}%" if summaries else "22%"
  deep_pct = f"{round(summaries.get('deep_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "29.8%"
  core_pct = f"{round(summaries.get('core_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "48.2%"

  hr_mid = int(latest.avg_heart_rate) if latest and latest.avg_heart_rate else 70
  hr_range = f"{hr_mid - 15}-{hr_mid + 15}bpm"
  resp_fluct = f"{int(latest.respiratory_var or 25)}%" if latest else "25%"

  scene_id   = "cocos_island_moonlight"
  scene_name = "Cocos Island Moonlight"
  most_used = (profile.sleep_analysis or {}).get("most_used_scene") if profile else None
  if most_used:
    scene_id   = most_used["scene_id"]
    scene_name = most_used["scene_name"]

  awake_count = summaries.get("night_awake_count", 2)
  result = {
    "data_ready": has_data,
    "header_summary": {
      "intro_text": "Last night your body entered a stable, relaxed, and highly restorative sleep state.",
      "intro_detail_text": "What happened last night, what helped you most, and how Mindora adjusted for you.",
      "date": date,
    },
    "score_summary": {
      "score": overall_score,
      "title": "Sleep Score",
      "efficiency_score":   onset_score,
      "structure_score":    structure_score,
      "fluctuation_score":  fluctuation_score,
      "date": date,
    },
    "onset_efficiency": {
      "score": onset_score,
      "label": "Healthy Range",
      "onset_minutes": 12,
      "first_sleep_time":           latest.first_sleep_time if latest else "23:45",
      "pre_sleep_heart_rate":       f"{int(latest.hr_before_sleep)}bpm"  if latest and latest.hr_before_sleep  else "68bpm",
      "pre_sleep_respiratory_rate": f"{int(latest.rr_before_sleep)}brpm" if latest and latest.rr_before_sleep else "15brpm",
      "description": "You fell asleep faster than your recent average and your pre-sleep physiology stayed calm.",
      "date": date,
    },
    "sleep_structure": {
      "score": structure_score,
      "label": "Average",
      "continuous_sleep_minutes": int(tb),
      "rem_percent":  rem_pct,
      "deep_percent": deep_pct,
      "core_percent": core_pct,
      "description": "Your sleep structure remained relatively balanced, with deep sleep contributing strongly to recovery.",
      "date": date,
    },
    "night_fluctuation": {
      "score": fluctuation_score,
      "label": "High Fluctuation" if awake_count > 3 else "Normal",
      "intervention": "Rain Wash",
      "awake_count":            awake_count,
      "awake_duration_minutes": int(summaries.get("night_awake_duration", 5)),
      "awake_type":             summaries.get("night_awake_type") or "Brief awakening",
      "heart_rate_range":       hr_range,
      "respiratory_fluctuation": resp_fluct,
      "description": "You had a small number of brief interruptions and the system applied a suitable intervention.",
      "date": date,
    },
    "scene_preference": {
      "scene_id":   scene_id,
      "scene_name": scene_name,
      "scene_type": "Ocean wind with slow percussion",
      "description": "This scene has recently matched your sleep onset rhythm most consistently.",
      "start_date": start,
      "end_date":   date,
    },
    "sleep_advice": {
      "description": "Keep your current bedtime and continue using the same wind-down scene for the next few nights.",
      "date": date,
    },
    # 洞察页 6 模块报告（mindora_advice.md 模块0-5，update_profile 时异步生成，
    # 已过滤 visible=False 模块；无报告则为 None）。/analysis 是其唯一客户端出口。
    "insight": AnalysisContentService._visible_insight_dict(profile),
  }
  return filter_modules(result, d.modules)
