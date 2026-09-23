"""analysis_fallback.py — 零睡眠记录用户的兜底分析内容（模板生成，不烧 LLM）。

背景：新用户还没用 Mindora 设备睡过一晚时，/analysis 与洞察页没有任何可分析的数据。
这里按用户基础信息（昵称/年龄段）生成通用的睡眠卫生建议 + 引导使用 Mindora 的 CTA，
以与 LLM 报告完全相同的结构落库（llm_used=False 标记），客户端无感展示；
等真实睡眠数据到达后，由 calc_sleep_insight / calc_analysis_reports 的
「兜底不拦截重生成」逻辑自然替换为真实 LLM 分析。

语言：zh-Hans / zh-Hant / en 三份模板，其他语言一律回退 en。
"""
import logging
import time
from typing import Optional

from user_profile import (
  UserProfile, SleepInsightReport, AnalysisTextReport,
  ANALYSIS_REPORT_KEYS, ANALYSIS_REPORT_RETENTION,
)


def _canonical_lang(lang: Optional[str]) -> str:
  return lang if lang in ("zh-Hans", "zh-Hant", "en") else "en"


def _nickname(profile: UserProfile) -> str:
  p = profile.profile
  name = (getattr(p, "nickname", None) or "").strip() if p else ""
  return name


def _age_group(profile: UserProfile) -> str:
  """按生日粗分年龄段（young/middle/senior/unknown），用于微调建议口吻。"""
  p = profile.profile
  birthday = (getattr(p, "birthday", None) or "") if p else ""
  try:
    year = int(str(birthday)[:4])
    age = 2026 - year
  except (ValueError, TypeError):
    return "unknown"
  if age < 30:
    return "young"
  if age < 55:
    return "middle"
  return "senior"


# ── 模板文案 ─────────────────────────────────────────────────────────────────
# {name} = 昵称（可能为空串，模板里已按有无昵称各写一份或天然兼容）

_T = {
  "zh-Hans": {
    "greeting_title": "开启你的睡眠之旅",
    "greeting_title_named": "{name}，开启你的睡眠之旅",
    "greeting_content": "这里还没有你的睡眠记录。今晚戴上 Mindora 设备入睡，明早醒来就能看到专属于你的睡眠分析。",
    "greeting_action": "今晚睡前戴上 Mindora，让它陪你度过第一晚。",
    "onset_title": "规律作息是最好的开始",
    "onset_content": "固定的入睡时间能训练身体的生物钟，让入睡越来越快。目前还没有你的入睡数据，先从设定一个固定的上床时间开始吧。",
    "onset_action": "选一个你能在大多数日子遵守的上床时间，并坚持一周。",
    "architecture_title": "了解你的睡眠结构",
    "architecture_content": "一晚好睡眠由深睡、浅睡和快速眼动（REM）交替组成：深睡修复身体，REM 巩固记忆与情绪。戴上 Mindora 睡一觉，就能看到你自己的睡眠结构图。",
    "architecture_action": "保证每晚 7-9 小时的睡眠机会，给深睡和 REM 留足时间。",
    "intervention_title": "夜里醒来很正常",
    "intervention_content": "几乎每个人夜里都会短暂醒来几次，关键是能否快速重新入睡。Mindora 会在监测到夜间波动时用声光帮你平稳度过，有数据后这里会展示它为你做了什么。",
    "intervention_action": "夜里醒来时不要看时间和手机，深呼吸几次，让自己自然重新入睡。",
    "scene_title": "找到你的专属助眠场景",
    "scene_content": "雨声、海浪、森林……不同的人对助眠声音的反应差异很大。Mindora 内置多套助眠场景，用一段时间后这里会告诉你哪一种最适合你。",
    "scene_action": "今晚从「智能推荐」场景开始，让 Mindora 帮你选。",
    "edu_title": "睡眠小知识",
    "edu_content": "睡前一小时调暗灯光、远离屏幕，可以让褪黑素自然分泌，入睡更快。卧室温度稍凉（约 18-20°C）也有助于深睡。",
    "edu_action": "今晚试试提前一小时放下手机。",
    # /analysis 各报告
    "overview_title": "开始你的第一次睡眠记录",
    "overview_desc": "还没有睡眠数据。戴上 Mindora 睡一晚，这里会生成你的第一份睡眠小结。",
    "scenario_title": "助眠场景等你体验",
    "scenario_desc": "佩戴 Mindora 入睡后，这里会告诉你助眠场景如何影响了你这一晚。",
    "stage_awake": "暂无数据——有记录后，这里会解读你夜里的清醒模式。",
    "stage_rem": "暂无数据——有记录后，这里会分析你的 REM（做梦期）质量。",
    "stage_core": "暂无数据——有记录后，这里会评估你的核心睡眠是否平稳。",
    "stage_deep": "暂无数据——有记录后，这里会告诉你深睡对身体的修复贡献。",
    "score_label": "暂无评分",
    "week_body": "一周睡眠趋势，从第一晚开始",
    "week_desc": "连续佩戴 Mindora 几天后，这里会展示你一周的睡眠走势和规律。",
    "month_body": "长期趋势，值得等待",
    "month_desc": "积累一个月的记录后，这里会总结你的长期睡眠变化。",
    "month_onset": "有数据后，这里会告诉你哪类助眠场景帮你入睡最快。",
    "explore_intro": "还没有睡眠记录，一切从第一晚开始。",
    "explore_intro_detail": "戴上 Mindora 入睡，明早这里会有你的专属分析。",
    "onset_label": "待记录",
    "onset_desc": "有数据后，这里会评估你的入睡速度和睡前身体状态。",
    "structure_label": "待记录",
    "structure_desc": "有数据后，这里会分析你的睡眠阶段构成和恢复质量。",
    "fluct_label": "待记录",
    "fluct_desc": "有数据后，这里会展示你夜间的睡眠波动和 Mindora 的调节。",
    "scene_pref_desc": "使用 Mindora 一段时间后，这里会告诉你哪种助眠场景最配你的睡眠节奏。",
    "advice_generic": "今晚试着在固定时间上床，睡前一小时远离屏幕，让 Mindora 陪你入睡。",
    "advice_young": "睡前刷手机是入睡的大敌——今晚提前半小时放下手机，戴上 Mindora 试试。",
    "advice_middle": "工作压力容易带到床上。睡前做几分钟深呼吸，让 Mindora 的场景帮你切换到睡眠模式。",
    "advice_senior": "白天小睡别超过 20 分钟，傍晚适当散步，晚上更容易一觉到天亮。",
  },

  "zh-Hant": {
    "greeting_title": "開啟你的睡眠之旅",
    "greeting_title_named": "{name}，開啟你的睡眠之旅",
    "greeting_content": "這裡還沒有你的睡眠記錄。今晚戴上 Mindora 裝置入睡，明早醒來就能看到專屬於你的睡眠分析。",
    "greeting_action": "今晚睡前戴上 Mindora，讓它陪你度過第一晚。",
    "onset_title": "規律作息是最好的開始",
    "onset_content": "固定的入睡時間能訓練身體的生理時鐘，讓入睡越來越快。目前還沒有你的入睡數據，先從設定一個固定的上床時間開始吧。",
    "onset_action": "選一個你能在大多數日子遵守的上床時間，並堅持一週。",
    "architecture_title": "了解你的睡眠結構",
    "architecture_content": "一晚好睡眠由深睡、淺睡和快速動眼（REM）交替組成：深睡修復身體，REM 鞏固記憶與情緒。戴上 Mindora 睡一覺，就能看到你自己的睡眠結構圖。",
    "architecture_action": "保證每晚 7-9 小時的睡眠機會，給深睡和 REM 留足時間。",
    "intervention_title": "夜裡醒來很正常",
    "intervention_content": "幾乎每個人夜裡都會短暫醒來幾次，關鍵是能否快速重新入睡。Mindora 會在監測到夜間波動時用聲光幫你平穩度過，有數據後這裡會展示它為你做了什麼。",
    "intervention_action": "夜裡醒來時不要看時間和手機，深呼吸幾次，讓自己自然重新入睡。",
    "scene_title": "找到你的專屬助眠場景",
    "scene_content": "雨聲、海浪、森林……不同的人對助眠聲音的反應差異很大。Mindora 內建多套助眠場景，用一段時間後這裡會告訴你哪一種最適合你。",
    "scene_action": "今晚從「智能推薦」場景開始，讓 Mindora 幫你選。",
    "edu_title": "睡眠小知識",
    "edu_content": "睡前一小時調暗燈光、遠離螢幕，可以讓褪黑素自然分泌，入睡更快。臥室溫度稍涼（約 18-20°C）也有助於深睡。",
    "edu_action": "今晚試試提前一小時放下手機。",
    "overview_title": "開始你的第一次睡眠記錄",
    "overview_desc": "還沒有睡眠數據。戴上 Mindora 睡一晚，這裡會生成你的第一份睡眠小結。",
    "scenario_title": "助眠場景等你體驗",
    "scenario_desc": "佩戴 Mindora 入睡後，這裡會告訴你助眠場景如何影響了你這一晚。",
    "stage_awake": "暫無數據——有記錄後，這裡會解讀你夜裡的清醒模式。",
    "stage_rem": "暫無數據——有記錄後，這裡會分析你的 REM（做夢期）質量。",
    "stage_core": "暫無數據——有記錄後，這裡會評估你的核心睡眠是否平穩。",
    "stage_deep": "暫無數據——有記錄後，這裡會告訴你深睡對身體的修復貢獻。",
    "score_label": "暫無評分",
    "week_body": "一週睡眠趨勢，從第一晚開始",
    "week_desc": "連續佩戴 Mindora 幾天後，這裡會展示你一週的睡眠走勢和規律。",
    "month_body": "長期趨勢，值得等待",
    "month_desc": "積累一個月的記錄後，這裡會總結你的長期睡眠變化。",
    "month_onset": "有數據後，這裡會告訴你哪類助眠場景幫你入睡最快。",
    "explore_intro": "還沒有睡眠記錄，一切從第一晚開始。",
    "explore_intro_detail": "戴上 Mindora 入睡，明早這裡會有你的專屬分析。",
    "onset_label": "待記錄",
    "onset_desc": "有數據後，這裡會評估你的入睡速度和睡前身體狀態。",
    "structure_label": "待記錄",
    "structure_desc": "有數據後，這裡會分析你的睡眠階段構成和恢復質量。",
    "fluct_label": "待記錄",
    "fluct_desc": "有數據後，這裡會展示你夜間的睡眠波動和 Mindora 的調節。",
    "scene_pref_desc": "使用 Mindora 一段時間後，這裡會告訴你哪種助眠場景最配你的睡眠節奏。",
    "advice_generic": "今晚試著在固定時間上床，睡前一小時遠離螢幕，讓 Mindora 陪你入睡。",
    "advice_young": "睡前滑手機是入睡的大敵——今晚提前半小時放下手機，戴上 Mindora 試試。",
    "advice_middle": "工作壓力容易帶到床上。睡前做幾分鐘深呼吸，讓 Mindora 的場景幫你切換到睡眠模式。",
    "advice_senior": "白天小睡別超過 20 分鐘，傍晚適當散步，晚上更容易一覺到天亮。",
  },

  "en": {
    "greeting_title": "Start your sleep journey",
    "greeting_title_named": "{name}, start your sleep journey",
    "greeting_content": "There are no sleep records yet. Wear your Mindora device tonight, and wake up to your first personalised sleep analysis.",
    "greeting_action": "Put on Mindora before bed tonight and let it accompany your first night.",
    "onset_title": "Consistency is the best start",
    "onset_content": "A fixed bedtime trains your body clock and makes falling asleep easier over time. There's no onset data yet — start by picking a regular bedtime.",
    "onset_action": "Choose a bedtime you can keep most days and stick to it for a week.",
    "architecture_title": "Know your sleep architecture",
    "architecture_content": "A good night alternates deep, light and REM sleep: deep sleep repairs the body, REM consolidates memory and mood. Sleep one night with Mindora to see your own architecture chart.",
    "architecture_action": "Give yourself 7–9 hours of sleep opportunity so deep sleep and REM get their time.",
    "intervention_title": "Night wakings are normal",
    "intervention_content": "Almost everyone wakes briefly a few times per night — what matters is falling back asleep quickly. Mindora smooths night fluctuations with sound and light; once there's data, this module shows what it did for you.",
    "intervention_action": "When you wake at night, don't check the time or your phone — breathe slowly and drift back naturally.",
    "scene_title": "Find your sleep scene",
    "scene_content": "Rain, waves, forest… people respond very differently to sleep sounds. Mindora ships many sleep scenes; after a few nights, this module tells you which one suits you best.",
    "scene_action": "Start with the Smart Recommendation scene tonight and let Mindora choose for you.",
    "edu_title": "Sleep tip",
    "edu_content": "Dim the lights and stay off screens an hour before bed so melatonin can rise naturally. A slightly cool bedroom (around 18–20°C) also helps deep sleep.",
    "edu_action": "Try putting your phone away an hour earlier tonight.",
    "overview_title": "Start your first sleep record",
    "overview_desc": "No sleep data yet. Sleep one night with Mindora and your first sleep summary will appear here.",
    "scenario_title": "Sleep scenes await",
    "scenario_desc": "After your first night with Mindora, this card shows how the sleep scene influenced your night.",
    "stage_awake": "No data yet — once recorded, this explains your night-time awakening pattern.",
    "stage_rem": "No data yet — once recorded, this analyses your REM (dream) quality.",
    "stage_core": "No data yet — once recorded, this rates how stable your core sleep is.",
    "stage_deep": "No data yet — once recorded, this shows how much deep sleep repaired your body.",
    "score_label": "No score yet",
    "week_body": "Weekly trends start with night one",
    "week_desc": "Wear Mindora for a few days and this shows your weekly sleep pattern.",
    "month_body": "Long-term trends are worth the wait",
    "month_desc": "After a month of records, this summarises your long-term sleep changes.",
    "month_onset": "With data, this tells you which sleep scenes help you fall asleep fastest.",
    "explore_intro": "No sleep records yet — everything starts with night one.",
    "explore_intro_detail": "Sleep with Mindora tonight and your personalised analysis appears here tomorrow.",
    "onset_label": "Pending",
    "onset_desc": "With data, this rates how fast you fall asleep and your pre-sleep body state.",
    "structure_label": "Pending",
    "structure_desc": "With data, this analyses your sleep-stage composition and recovery.",
    "fluct_label": "Pending",
    "fluct_desc": "With data, this shows night fluctuations and how Mindora responded.",
    "scene_pref_desc": "After a few nights with Mindora, this tells you which scene best matches your sleep rhythm.",
    "advice_generic": "Try a fixed bedtime tonight, stay off screens for the last hour, and let Mindora accompany you to sleep.",
    "advice_young": "Late-night scrolling is the enemy of falling asleep — put the phone down 30 minutes early tonight and try Mindora.",
    "advice_middle": "Work stress follows you to bed. A few minutes of deep breathing before sleep, plus a Mindora scene, helps switch into sleep mode.",
    "advice_senior": "Keep daytime naps under 20 minutes and take an early-evening walk — it makes sleeping through the night easier.",
  },
}


def _texts(profile: UserProfile, lang: str) -> dict:
  return _T[_canonical_lang(lang)]


def _greeting_title(t: dict, profile: UserProfile) -> str:
  name = _nickname(profile)
  if name:
    return t["greeting_title_named"].format(name=name)
  return t["greeting_title"]


def _advice(t: dict, profile: UserProfile) -> str:
  group = _age_group(profile)
  key = {"young": "advice_young", "middle": "advice_middle", "senior": "advice_senior"}.get(group)
  return t[key] if key else t["advice_generic"]


# ── 构建入口 ─────────────────────────────────────────────────────────────────

_INSIGHT_MODULE_IDS = {
  "greeting": 0, "onset": 1, "architecture": 2,
  "intervention": 3, "scene_preference": 4, "micro_education": 5,
}


def build_fallback_insight(profile: UserProfile, lang: str, date: str, now: Optional[int] = None) -> SleepInsightReport:
  """零睡眠记录时的 6 模块兜底洞察（结构与 LLM 报告一致，llm_used=False）。"""
  t = _texts(profile, lang)
  now = now or int(time.time())
  modules = {
    "greeting": {"title": _greeting_title(t, profile), "content": t["greeting_content"], "action": t["greeting_action"]},
    "onset": {"title": t["onset_title"], "content": t["onset_content"], "action": t["onset_action"]},
    "architecture": {"title": t["architecture_title"], "content": t["architecture_content"], "action": t["architecture_action"]},
    "intervention": {"title": t["intervention_title"], "content": t["intervention_content"], "action": t["intervention_action"]},
    "scene_preference": {"title": t["scene_title"], "content": t["scene_content"], "action": t["scene_action"]},
    "micro_education": {"title": t["edu_title"], "content": t["edu_content"], "action": t["edu_action"]},
  }
  data = {
    "date": date,
    "language": _canonical_lang(lang),
    "generated_at": now,
    "llm_used": False,
  }
  for key, module_id in _INSIGHT_MODULE_IDS.items():
    m = modules[key]
    data[key] = {
      "module_id": module_id,
      "title": m["title"],
      "content": m["content"],
      "evidence": [],
      "action": m["action"],
    }
  return SleepInsightReport(**data)


def fallback_modules_for(request_type: str, profile: UserProfile, lang: str) -> dict:
  """各 /analysis 报告类型的兜底 modules（结构与 LLM 输出一致）。"""
  t = _texts(profile, lang)
  if request_type == "analysis_overview":
    return {"sleep_insight": {"title": t["overview_title"], "description": t["overview_desc"]}}
  if request_type == "analysis_sleep_day":
    return {
      "sleep_scenarios": {"title": t["scenario_title"], "description": t["scenario_desc"]},
      "stage_insights": {
        "awake": {"description": t["stage_awake"]},
        "rem": {"description": t["stage_rem"]},
        "core": {"description": t["stage_core"]},
        "deep": {"description": t["stage_deep"]},
      },
    }
  if request_type == "analysis_sleep_week":
    return {
      "score_summary": {"label": t["score_label"]},
      "sleep_trends": {"body": t["week_body"], "description": t["week_desc"]},
    }
  if request_type == "analysis_sleep_month":
    return {
      "score_summary": {"label": t["score_label"]},
      "sleep_trends": {"body": t["month_body"], "description": t["month_desc"]},
      "onset_efficiency": {"description": t["month_onset"]},
    }
  if request_type == "analysis_explore":
    return {
      "header_summary": {"intro_text": t["explore_intro"], "intro_detail_text": t["explore_intro_detail"]},
      "onset_efficiency": {"label": t["onset_label"], "description": t["onset_desc"]},
      "sleep_structure": {"label": t["structure_label"], "description": t["structure_desc"]},
      "night_fluctuation": {"label": t["fluct_label"], "description": t["fluct_desc"]},
      "scene_preference": {"description": t["scene_pref_desc"]},
      "sleep_advice": {"description": _advice(t, profile)},
    }
  logging.warning("no fallback modules for request_type=%s", request_type)
  return {}


def build_fallback_report(
  request_type: str,
  profile: UserProfile,
  lang: str,
  *,
  date: str,
  start_date: Optional[str] = None,
  end_date: Optional[str] = None,
  now: Optional[int] = None,
) -> AnalysisTextReport:
  return AnalysisTextReport(
    request_type=request_type,
    date=date,
    start_date=start_date,
    end_date=end_date,
    language=_canonical_lang(lang),
    generated_at=now or int(time.time()),
    llm_used=False,
    modules=fallback_modules_for(request_type, profile, lang),
  )
