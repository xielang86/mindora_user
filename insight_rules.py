"""insight_rules.py — 洞察规则引擎：按《Mindora_App睡眠数据展示与分析对照规范_v3》§4
从画像数据计算结构化洞察结论（规则选结论 + 模板填空；LLM 仅可在既定事实上润色）。

设计原则（规范 §1/§4）：
- 只与个人基线比较，不下诊断 / 因果 / 有效性结论（禁用词表见 config.INSIGHT_RULES）；
- 长短期记忆：自然日窗口（7d / 前7d / 30d / 前30d）、SOL 个人基线（BASE_SOL）、
  建议历史与昨日首页主题（insight_memory，持久化在 profile.sleep_analysis）；
- 数据状态分级（AN_DATA_STATE）：single_night 只描述事实，基线就绪后才做相对判断。

本模块为纯函数，不 import profile_service / analysis_content（避免循环依赖）。
"""
from __future__ import annotations

import datetime
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from zoneinfo import ZoneInfo

from analysis_fallback import _canonical_lang, _nickname
from insight_rules_config import get_insight_rules
from user_profile import UserProfile, SleepResult, short_scene_id


class _RulesProxy:
  """阈值配置代理：所有 R["..."] 读取在调用时实时取当前生效配置
  （内置默认 ← 运营 JSON 覆盖，热加载），import 期绑定不会 stale。"""

  def __getitem__(self, key):
    return get_insight_rules()[key]

  def get(self, key, default=None):
    return get_insight_rules().get(key, default)


R = _RulesProxy()

# 场景 key 关键词 → 内容类型（SC_CONTENT 降级方案：无内容库时从 scene_id 粗分，
# 规范 M31 的 noise_tags/acoustic_traits 段无数据来源，v1 省略）
_SCENE_TYPE_KEYWORDS = [
    ("ocean wave sea beach coast", "ocean"),
    ("rain drizzle storm", "rain"),
    ("forest jungle tree woodland", "forest"),
    ("wind breeze", "wind"),
    ("moon night star lullaby", "night"),
    ("river brook stream water", "water"),
]
_SCENE_TYPE_LABEL = {
    "ocean": {"zh-Hans": "海浪声", "zh-Hant": "海浪聲", "en": "ocean waves"},
    "rain":  {"zh-Hans": "雨声", "zh-Hant": "雨聲", "en": "rain sounds"},
    "forest": {"zh-Hans": "森林声", "zh-Hant": "森林聲", "en": "forest sounds"},
    "wind":  {"zh-Hans": "风声", "zh-Hant": "風聲", "en": "wind sounds"},
    "night": {"zh-Hans": "夜晚氛围", "zh-Hant": "夜晚氛圍", "en": "night ambience"},
    "water": {"zh-Hans": "流水声", "zh-Hant": "流水聲", "en": "flowing water"},
}

# 洞察模块 key → (SleepInsightReport module_id, theme id)
MODULE_META = {
    "greeting": (0, "greeting"),
    "onset": (1, "onset"),
    "architecture": (2, "structure"),
    "intervention": (3, "fluctuation"),
    "scene_preference": (4, "scene"),
    "micro_education": (5, "education"),
}

# ── 结论数据类 ──────────────────────────────────────────────────────────────


@dataclass
class RuleConclusion:
    key: str            # MODULE_META 的 key
    theme: str          # 机器主题 id（首页摘要去重用）
    title: str = ""     # 模块标题（已本地化）
    text: str = ""      # 模块正文（模板渲染结果）
    evidence: list = field(default_factory=list)
    action: str = ""
    visible: bool = True
    state: str = "facts_only"   # faster/stable/slower/near_normal/mild_change/marked_change/stable_night/...
    template_key: str = ""
    variables: dict = field(default_factory=dict)
    facts_only: bool = False
    valid_nights: int = 0   # 数据完整度（供 AN_HOME_SUMMARY 排序）
    magnitude: float = 0.0  # 变化幅度（供排序）
    actionability: int = 0  # 0..2（供排序）
    advice_types: list = field(default_factory=list)  # rule_advice 选中的建议类型（写记忆用）
    home_theme: str = ""    # rule_home_summary 选中的主题（写记忆用）

    def to_llm_dict(self) -> dict:
        """给 LLM 润色用的紧凑结构化输入（含事实与已渲染文案，禁止改数字/事实）。"""
        return {
            "key": self.key, "state": self.state, "facts_only": self.facts_only,
            "title": self.title, "text": self.text,
            "evidence": self.evidence, "action": self.action,
            "variables": self.variables,
        }


class TemplateRenderError(ValueError):
    pass


# ── 模板与本地化 ────────────────────────────────────────────────────────────

_TEMPLATES: dict[str, dict[str, str]] = {
    # AN_DATA_STATE 问候（:3137）
    "greeting": {
        "zh-Hans": "Hi，{nickname}。Mindora 会结合你授权的睡眠数据与已记录的场景使用，解释每晚发生了什么。{state_clause}",
        "zh-Hant": "Hi，{nickname}。Mindora 會結合你授權的睡眠數據與已記錄的場景使用，解釋每晚發生了什麼。{state_clause}",
        "en": "Hi, {nickname}. Mindora combines your authorized sleep data with recorded scene usage to explain what happened each night. {state_clause}",
    },
    "greeting_state_single": {
        "zh-Hans": "目前已有第一晚记录，这里先描述昨晚的事实，积累几晚后会出现趋势判断。",
        "zh-Hant": "目前已有第一晚記錄，這裡先描述昨晚的事實，積累幾晚後會出現趨勢判斷。",
        "en": "You have one night recorded so far — only last night's facts for now. Trends appear after a few more nights.",
    },
    "greeting_state_7d": {
        "zh-Hans": "近 7 天已有足够记录，洞察将结合你个人的近期基线。",
        "zh-Hant": "近 7 天已有足夠記錄，洞察將結合你個人的近期基線。",
        "en": "The last 7 days give enough data, so insights now compare against your personal recent baseline.",
    },
    "greeting_state_30d": {
        "zh-Hans": "近 30 天记录已较完整，洞察会同时参考你的长期基线。",
        "zh-Hant": "近 30 天記錄已較完整，洞察會同時參考你的長期基線。",
        "en": "With about 30 nights recorded, insights also reference your long-term baseline.",
    },
    "greeting_state_empty": {
        "zh-Hans": "数据不足时不会做趋势判断。",
        "zh-Hant": "數據不足時不會做趨勢判斷。",
        "en": "No trend judgments are made until enough data is available.",
    },
    # AN_ONSET（:3307）
    "onset_faster": {
        "zh-Hans": "昨晚你比平时更快入睡（少 {delta} 分钟）。近 7 天平均入睡用时 {baseline7} 分钟，昨晚 {last_sol} 分钟。{vitals_clause}{scene_clause}",
        "zh-Hant": "昨晚你平時更快入睡（少 {delta} 分鐘）。近 7 天平均入睡用時 {baseline7} 分鐘，昨晚 {last_sol} 分鐘。{vitals_clause}{scene_clause}",
        "en": "You fell asleep faster than usual last night ({delta} min quicker). Your 7-day average sleep onset is {baseline7} min; last night it was {last_sol} min. {vitals_clause}{scene_clause}",
    },
    "onset_slower": {
        "zh-Hans": "昨晚入睡用时比平时略长（多 {delta} 分钟）。近 7 天平均入睡用时 {baseline7} 分钟，昨晚 {last_sol} 分钟。{vitals_clause}{scene_clause}",
        "zh-Hant": "昨晚入睡用時平時略長（多 {delta} 分鐘）。近 7 天平均入睡用時 {baseline7} 分鐘，昨晚 {last_sol} 分鐘。{vitals_clause}{scene_clause}",
        "en": "It took a bit longer to fall asleep than usual last night ({delta} min more). Your 7-day average sleep onset is {baseline7} min; last night it was {last_sol} min. {vitals_clause}{scene_clause}",
    },
    "onset_stable": {
        "zh-Hans": "昨晚入睡用时与近 7 天平均水平基本持平（{last_sol} 分钟，基线 {baseline7} 分钟）。{vitals_clause}{scene_clause}",
        "zh-Hant": "昨晚入睡用時與近 7 天平均水平基本持平（{last_sol} 分鐘，基線 {baseline7} 分鐘）。{vitals_clause}{scene_clause}",
        "en": "Sleep onset was in line with your 7-day average last night ({last_sol} min vs baseline {baseline7} min). {vitals_clause}{scene_clause}",
    },
    "onset_facts": {
        "zh-Hans": "昨晚入睡用时 {last_sol} 分钟。{vitals_clause}",
        "zh-Hant": "昨晚入睡用時 {last_sol} 分鐘。{vitals_clause}",
        "en": "Sleep onset took {last_sol} min last night. {vitals_clause}",
    },
    "onset_no_measure": {
        "zh-Hans": "昨晚未能测得入睡用时（记录从入睡后才开始），连续记录几晚后即可比较。",
        "zh-Hant": "昨晚未能測得入睡用時（記錄從入睡後才開始），連續記錄幾晚後即可比較。",
        "en": "Sleep onset wasn't measurable last night (recording started after you fell asleep). It becomes comparable after a few nights of continuous recording.",
    },
    "vitals_clause": {
        "zh-Hans": "入睡前{hr_direction_zh}。",
        "zh-Hant": "入睡前{hr_direction_zh}。",
        "en": "Pre-sleep vitals were {hr_direction_en}.",
    },
    "scene_clause_assoc": {
        "zh-Hans": "过去一周你更常选择「{scene}」，可继续观察它与你入睡表现的关联。",
        "zh-Hant": "過去一週你更常選擇「{scene}」，可繼續觀察它與你入睡表現的關聯。",
        "en": "You have mostly chosen \"{scene}\" this week — keep observing how it relates to your onset.",
    },
    "scene_clause_pref": {
        "zh-Hans": "过去一周你较常选择「{scene}」（样本尚少，暂不作关联判断）。",
        "zh-Hant": "過去一週你較常選擇「{scene}」（樣本尚少，暫不作關聯判斷）。",
        "en": "You have often chosen \"{scene}\" this week (too few samples for association yet).",
    },
    # AN_STRUCTURE（:3341）
    "structure_near_normal": {
        "zh-Hans": "昨晚的睡眠结构与过去一周基本接近。睡眠阶段每天都会自然波动，继续保持规律作息即可。",
        "zh-Hant": "昨晚的睡眠結構與過去一週基本接近。睡眠階段每天都會自然波動，繼續保持規律作息即可。",
        "en": "Last night's sleep structure was close to your week-long pattern. Sleep stages naturally fluctuate daily — just keep a regular schedule.",
    },
    "structure_mild": {
        "zh-Hans": "昨晚的睡眠结构与过去一周相比有轻微变化（{stage_sentence}）。睡眠阶段每天都会自然波动，继续保持规律作息即可。",
        "zh-Hant": "昨晚的睡眠結構與過去一週相比有輕微變化（{stage_sentence}）。睡眠階段每天都會自然波動，繼續保持規律作息即可。",
        "en": "Last night's sleep structure changed slightly compared with your week-long pattern ({stage_sentence}). Sleep stages naturally fluctuate daily — keep a regular schedule.",
    },
    "structure_marked": {
        "zh-Hans": "昨晚的睡眠结构与过去一周相比变化较明显（{stage_sentence}）。睡眠阶段每天都会自然波动，若持续出现可关注入睡与起床时间是否稳定。",
        "zh-Hant": "昨晚的睡眠結構與過去一週相比變化較明顯（{stage_sentence}）。睡眠階段每天都會自然波動，若持續出現可關注入睡與起床時間是否穩定。",
        "en": "Last night's sleep structure differed noticeably from your week-long pattern ({stage_sentence}). Sleep stages naturally fluctuate daily; if this keeps happening, check whether your bedtime and wake time are stable.",
    },
    "structure_facts": {
        "zh-Hans": "昨晚总睡眠 {tst}，最长连续睡眠 {continuous} 分钟。积累更多夜晚后，这里会给出与你自己常态的比较。",
        "zh-Hant": "昨晚總睡眠 {tst}，最長連續睡眠 {continuous} 分鐘。積累更多夜晚後，這裡會給出與你自己常態的比較。",
        "en": "Total sleep was {tst} last night, with a longest continuous block of {continuous} min. After more nights, this will compare against your own pattern.",
    },
    "stage_sentence": {
        "zh-Hans": "{stage}睡眠较平时{direction}{pct}%",
        "zh-Hant": "{stage}睡眠較平時{direction}{pct}%",
        "en": "{stage} sleep was {pct}% {direction_en} than usual",
    },
    # AN_FLUCTUATION（:3375）
    "fluctuation_stable": {
        "zh-Hans": "昨晚睡眠较为平稳，没有记录到需要说明的夜间清醒。",
        "zh-Hant": "昨晚睡眠較為平穩，沒有記錄到需要說明的夜間清醒。",
        "en": "Your sleep was steady last night with no notable nighttime awakenings.",
    },
    "fluctuation_summary": {
        "zh-Hans": "昨晚出现 {count} 次需要说明的夜间波动，累计 {waso} 分钟。{events}",
        "zh-Hant": "昨晚出現 {count} 次需要說明的夜間波動，累計 {waso} 分鐘。{events}",
        "en": "There were {count} notable nighttime disturbances last night, totaling {waso} min. {events}",
    },
    "fluctuation_event": {
        "zh-Hans": "在 {time}，一次觉醒持续约 {awake_min} 分钟{vital_clause}。{intervention_clause}随后约 {resleep} 分钟再次记录到睡眠。",
        "zh-Hant": "在 {time}，一次覺醒持續約 {awake_min} 分鐘{vital_clause}。{intervention_clause}隨後約 {resleep} 分鐘再次記錄到睡眠。",
        "en": "At {time}, an awakening lasted about {awake_min} min{vital_clause}. {intervention_clause}Sleep was recorded again about {resleep} min later.",
    },
    "intervention_clause": {
        "zh-Hans": "Mindora 于 {itime} 执行了 {iaction}；",
        "zh-Hant": "Mindora 於 {itime} 執行了 {iaction}；",
        "en": "Mindora ran {iaction} at {itime}; ",
    },
    # AN_SCENE（:3409 五段结构，按可用数据组装）
    "scene_facts": {
        "zh-Hans": "昨晚你在「{scene}」的环境中进入睡眠。",
        "zh-Hant": "昨晚你在「{scene}」的環境中進入睡眠。",
        "en": "You fell asleep in the \"{scene}\" environment last night.",
    },
    "scene_content": {
        "zh-Hans": "它以{scene_type}为主。",
        "zh-Hant": "它以{scene_type}為主。",
        "en": "It is mainly {scene_type}.",
    },
    "scene_assoc": {
        "zh-Hans": "在最近使用它的 {nights} 个夜晚，你的平均睡眠评分为 {score}，平均入睡用时 {sol} 分钟。",
        "zh-Hant": "在最近使用它的 {nights} 個夜晚，你的平均睡眠評分為 {score}，平均入睡用時 {sol} 分鐘。",
        "en": "Across the recent {nights} nights you used it, your average sleep score was {score}, with average onset of {sol} min.",
    },
    "scene_pref": {
        "zh-Hans": "近期你更常选择它（近 7 天 {uses} 次，样本积累中，暂不作关联判断）。",
        "zh-Hant": "近期你更常選擇它（近 7 天 {uses} 次，樣本積累中，暫不作關聯判斷）。",
        "en": "You have chosen it often recently ({uses} times in 7 days; still building samples, no association judgment yet).",
    },
    "scene_recommend": {
        "zh-Hans": "如果想尝试相近路径，可选择「{recommend}」。",
        "zh-Hant": "如果想嘗試相近路徑，可選擇「{recommend}」。",
        "en": "For a similar path, you can try \"{recommend}\".",
    },
    "scene_no_scene": {
        "zh-Hans": "昨晚没有记录到场景使用。近期你最常选择「{scene}」（近 7 天 {uses} 次）。",
        "zh-Hant": "昨晚沒有記錄到場景使用。近期你最常選擇「{scene}」（近 7 天 {uses} 次）。",
        "en": "No scene usage was recorded last night. Recently you most often chose \"{scene}\" ({uses} times in 7 days).",
    },
    # AN_ADVICE（:3443）
    "advice_record_more": {
        "zh-Hans": "继续佩戴 Mindora 记录睡眠，连续记录几晚后，这里会出现更有针对性的建议。",
        "zh-Hant": "繼續佩戴 Mindora 記錄睡眠，連續記錄幾晚後，這裡會出現更有針對性的建議。",
        "en": "Keep wearing Mindora to record sleep. After a few more continuous nights, more specific advice will appear here.",
    },
    "advice_onset_routine": {
        "zh-Hans": "近几晚入睡用时有所上升。今晚可尝试固定睡前放松流程：提前调暗灯光、远离屏幕，并使用你熟悉的助眠场景。",
        "zh-Hant": "近幾晚入睡用時有所上升。今晚可嘗試固定睡前放鬆流程：提前調暗燈光、遠離屏幕，並使用你熟悉的助眠場景。",
        "en": "Onset has lengthened over recent nights. Tonight, try a fixed wind-down routine: dim the lights early, stay away from screens, and use a familiar sleep scene.",
    },
    "advice_structure_regular": {
        "zh-Hans": "睡眠结构近期波动较明显。保持固定的入睡与起床时间，有助于睡眠结构稳定。",
        "zh-Hant": "睡眠結構近期波動較明顯。保持固定的入睡與起床時間，有助於睡眠結構穩定。",
        "en": "Your sleep structure has varied noticeably lately. Keeping consistent bed and wake times helps stabilize it.",
    },
    "advice_fluctuation_winddown": {
        "zh-Hans": "近期夜间清醒次数偏多。睡前避免大量饮水与酒精，卧室保持稍凉（约 18-20°C）。",
        "zh-Hant": "近期夜間清醒次數偏多。睡前避免大量飲水與酒精，臥室保持稍涼（約 18-20°C）。",
        "en": "Nighttime awakenings have been frequent lately. Avoid heavy drinking and alcohol before bed, and keep the bedroom slightly cool (around 18-20°C).",
    },
    "advice_scene_consistency": {
        "zh-Hans": "你近期常在「{scene}」陪伴下入睡。保持一致的睡前声音环境，对入睡节奏有帮助。",
        "zh-Hant": "你近期常在「{scene}」陪伴下入睡。保持一致的睡前聲音環境，對入睡節奏有幫助。",
        "en": "You have often fallen asleep with \"{scene}\" lately. A consistent bedtime sound environment helps your sleep rhythm.",
    },
    # AN_HOME_SUMMARY（:3035，引用已选洞察，不造新结论）
    "home_onset": {
        "zh-Hans": "昨晚入睡用时较近期基线{direction}，整体更接近你近期的平均水平。查看完整洞察了解发生了什么。",
        "zh-Hant": "昨晚入睡用時較近期基線{direction}，整體更接近你近期的平均水平。查看完整洞察了解發生了什麼。",
        "en": "Last night's onset was {direction_en} your recent baseline — overall close to your usual level. See the full insight for details.",
    },
    "home_structure": {
        "zh-Hans": "昨晚睡眠结构与近期常态相比{level}。查看完整洞察了解发生了什么。",
        "zh-Hant": "昨晚睡眠結構與近期常態相比{level}。查看完整洞察了解發生了什麼。",
        "en": "Last night's sleep structure was {level_en} your recent norm. See the full insight for details.",
    },
    "home_fluctuation": {
        "zh-Hans": "昨晚出现 {count} 次需要说明的夜间清醒。查看完整洞察了解发生了什么。",
        "zh-Hant": "昨晚出現 {count} 次需要說明的夜間清醒。查看完整洞察了解發生了什麼。",
        "en": "There were {count} notable awakenings last night. See the full insight for details.",
    },
    "home_scene": {
        "zh-Hans": "昨晚你在「{scene}」陪伴下入睡。查看完整洞察了解发生了什么。",
        "zh-Hant": "昨晚你在「{scene}」陪伴下入睡。查看完整洞察了解發生了什麼。",
        "en": "You fell asleep with \"{scene}\" last night. See the full insight for details.",
    },
    # AN_TREND_7D/30D（:3069 / :3103）
    "trend_item": {
        "zh-Hans": "本周期{metric}平均为{current}，较前一周期{direction}{delta}；{stability_sentence}。",
        "zh-Hant": "本週期{metric}平均為{current}，較前一週期{direction}{delta}；{stability_sentence}。",
        "en": "Average {metric_en} this period is {current}, {direction_en} by {delta} vs the previous period; {stability_sentence}.",
    },
    "trend_stable": {
        "zh-Hans": "近 {days} 天整体睡眠指标保持稳定，没有超过最小变化阈值的波动。",
        "zh-Hant": "近 {days} 天整體睡眠指標保持穩定，沒有超過最小變化閾值的波動。",
        "en": "Overall sleep metrics stayed stable over the last {days} days, with no changes beyond the minimum threshold.",
    },
    "trend_dist_30d": {
        "zh-Hans": "近 30 天平均{metric}为{current}（{n} 个有效夜晚），期间{stability}。数据积累满两个周期后，这里会给出与上一周期的对比。",
        "zh-Hant": "近 30 天平均{metric}為{current}（{n} 個有效夜晚），期間{stability}。數據積累滿兩個週期後，這裡會給出與上一週期的對比。",
        "en": "Average {metric_en} over the last 30 days is {current} ({n} valid nights), with {stability} fluctuation. A comparison against the previous period appears once two full periods are recorded.",
    },
    "metric_tst": {"zh-Hans": "总睡眠时长", "zh-Hant": "總睡眠時長", "en": "total sleep time"},
    "metric_sol": {"zh-Hans": "入睡用时", "zh-Hant": "入睡用時", "en": "sleep onset latency"},
    "metric_waso": {"zh-Hans": "夜间清醒时长", "zh-Hant": "夜間清醒時長", "en": "time awake after sleep onset"},
    "metric_awake_count": {"zh-Hans": "夜间清醒次数", "zh-Hant": "夜間清醒次數", "en": "nighttime awakenings"},
    "metric_first_sleep": {"zh-Hans": "首次入睡时间", "zh-Hant": "首次入睡時間", "en": "first sleep time"},
    "dir_up": {"zh-Hans": "增加", "zh-Hant": "增加", "en": "up"},
    "dir_down": {"zh-Hans": "减少", "zh-Hant": "減少", "en": "down"},
    "dir_higher": {"zh-Hans": "偏晚", "zh-Hant": "偏晚", "en": "later"},
    "dir_lower": {"zh-Hans": "偏早", "zh-Hant": "偏早", "en": "earlier"},
    "stability_high": {"zh-Hans": "波动较小", "zh-Hant": "波動較小", "en": "low variability"},
    "stability_low": {"zh-Hans": "波动较大", "zh-Hant": "波動較大", "en": "high variability"},
}

# 标题模板（模块标题，短标签）
_TITLES: dict[str, dict[str, str]] = {
    "title_greeting": {"zh-Hans": "开启你的睡眠之旅", "zh-Hant": "開啟你的睡眠之旅", "en": "Start your sleep journey"},
    "title_single": {"zh-Hans": "第一晚记录", "zh-Hant": "第一晚記錄", "en": "First night recorded"},
    "title_baseline7": {"zh-Hans": "近期基线已建立", "zh-Hant": "近期基線已建立", "en": "Recent baseline ready"},
    "title_baseline30": {"zh-Hans": "长期基线已建立", "zh-Hant": "長期基線已建立", "en": "Long-term baseline ready"},
    "title_onset_faster": {"zh-Hans": "入睡较快", "zh-Hant": "入睡較快", "en": "Faster onset"},
    "title_onset_slower": {"zh-Hans": "入睡偏慢", "zh-Hant": "入睡偏慢", "en": "Slower onset"},
    "title_onset_stable": {"zh-Hans": "入睡平稳", "zh-Hant": "入睡平穩", "en": "Steady onset"},
    "title_onset_facts": {"zh-Hans": "入睡用时", "zh-Hant": "入睡用時", "en": "Sleep onset"},
    "title_structure_normal": {"zh-Hans": "结构接近常态", "zh-Hant": "結構接近常態", "en": "Structure near normal"},
    "title_structure_mild": {"zh-Hans": "结构轻微变化", "zh-Hant": "結構輕微變化", "en": "Slight structural change"},
    "title_structure_marked": {"zh-Hans": "结构明显变化", "zh-Hant": "結構明顯變化", "en": "Noticeable structural change"},
    "title_structure_facts": {"zh-Hans": "睡眠结构", "zh-Hant": "睡眠結構", "en": "Sleep structure"},
    "title_fluctuation_stable": {"zh-Hans": "夜间平稳", "zh-Hant": "夜間平穩", "en": "Steady night"},
    "title_fluctuation_events": {"zh-Hans": "夜间波动", "zh-Hant": "夜間波動", "en": "Nighttime disturbances"},
    "title_scene": {"zh-Hans": "场景偏好", "zh-Hant": "場景偏好", "en": "Scene preference"},
    "title_advice": {"zh-Hans": "睡眠建议", "zh-Hant": "睡眠建議", "en": "Sleep advice"},
    "title_trend7": {"zh-Hans": "周趋势", "zh-Hant": "週趨勢", "en": "Weekly trend"},
    "title_trend30": {"zh-Hans": "月趋势", "zh-Hant": "月趨勢", "en": "Monthly trend"},
}

# 轻量睡眠知识（micro_education 模块，规则轮换，替代 LLM 自由发挥）
_EDU_FACTS = [
    {"zh-Hans": "睡前一小时调暗灯光、远离屏幕，有助于身体自然进入睡眠状态。卧室稍凉（约 18-20°C）通常更利于深睡。",
     "zh-Hant": "睡前一小時調暗燈光、遠離屏幕，有助於身體自然進入睡眠狀態。臥室稍涼（約 18-20°C）通常更利於深睡。",
     "en": "Dimming lights and avoiding screens an hour before bed helps your body wind down naturally. A slightly cool bedroom (around 18-20°C) usually supports deeper sleep."},
    {"zh-Hans": "固定的入睡和起床时间能稳定生物钟，让入睡 progressively 更顺畅。",
     "zh-Hant": "固定的入睡和起床時間能穩定生物鐘，讓入睡 progressively 更順暢。",
     "en": "Consistent bed and wake times stabilize your body clock and make falling asleep progressively smoother."},
    {"zh-Hans": "夜里短暂醒来几次很常见，关键是能否很快重新入睡。醒来后看时间或手机反而容易让大脑清醒。",
     "zh-Hant": "夜裡短暫醒來幾次很常見，關鍵是能否很快重新入睡。醒來後看時間或手機反而容易讓大腦清醒。",
     "en": "Brief awakenings at night are common — what matters is falling back asleep quickly. Checking the time or your phone can actually wake your brain up."},
]


def _t(key: str, lang: str) -> str:
    entry = _TEMPLATES.get(key) or _TITLES.get(key) or {}
    return entry.get(_canonical_lang(lang)) or entry.get("en", key)


def render(template_key: str, variables: dict, lang: str) -> str:
    """模板渲染 + 变量校验：占位符缺失/为 None 抛 TemplateRenderError，
    由调用方降级到事实句模板（规范：变量缺失时退回完整洞察的事实句）。"""
    tpl = _t(template_key, lang)
    import string
    placeholders = [f[1] for f in string.Formatter().parse(tpl) if f[1]]
    for ph in placeholders:
        if ph not in variables or variables[ph] is None:
            raise TemplateRenderError(f"template {template_key!r} missing variable {ph!r}")
    return tpl.format(**{ph: variables.get(ph, "") for ph in placeholders})


def _safe_render(template_key: str, variables: dict, lang: str, facts_fallback: str) -> str:
    try:
        return render(template_key, variables, lang)
    except (TemplateRenderError, KeyError):
        return facts_fallback


# ── 窗口与基线（长短期记忆核心）─────────────────────────────────────────────


def resolve_tz(tz_name: Optional[str]) -> datetime.tzinfo:
    try:
        return ZoneInfo(tz_name) if tz_name else datetime.timezone.utc
    except Exception:
        return datetime.timezone.utc


def night_date(record: SleepResult, tz) -> datetime.date:
    return datetime.datetime.fromtimestamp(record.timestamp, tz).date()


def is_valid_night(r: SleepResult) -> bool:
    return bool(r.sleep_status) or r.sleep_quality is not None


def records_in_window(profile: UserProfile, *, end_date: datetime.date, days: int, tz) -> list[SleepResult]:
    """自然日窗口 [end_date-days+1, end_date] 内的有效夜晚（区别于 [-days:] 条数切片）。"""
    start = end_date - datetime.timedelta(days=days - 1)
    out = []
    for r in profile.sleep_data or []:
        d = night_date(r, tz)
        if start <= d <= end_date and is_valid_night(r):
            out.append(r)
    return out


def _mean(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _mad(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    med = sorted(vals)[len(vals) // 2] if len(vals) % 2 else (sorted(vals)[len(vals) // 2 - 1] + sorted(vals)[len(vals) // 2]) / 2
    return _mean([abs(v - med) for v in vals])


def _circular_mean_hhmm(times: list[str]) -> Optional[str]:
    """时钟 HH:MM 圆周均值（跨午夜安全，如 23:50 + 00:10 → 00:00）。"""
    pts = []
    for t in times or []:
        try:
            h, m = t.split(":")
            pts.append((int(h) * 60 + int(m)) % 1440)
        except (ValueError, AttributeError):
            continue
    if not pts:
        return None
    rad = [2 * math.pi * p / 1440.0 for p in pts]
    x, y = sum(math.cos(a) for a in rad), sum(math.sin(a) for a in rad)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    mean = (math.degrees(math.atan2(y, x)) % 360) / 360.0 * 1440.0
    mean_min = int(round(mean)) % 1440
    return f"{mean_min // 60:02d}:{mean_min % 60:02d}"


@dataclass
class Baseline:
    tz: Any
    today: datetime.date
    latest: Optional[SleepResult]
    nights_7d: list           # 含昨晚
    nights_30d: list
    nights_prev_7d: list
    nights_prev_30d: list
    sol7: Optional[float]     # 近7日基线（不含昨晚，≥baseline7_min_nights 可测夜晚才有）
    sol30: Optional[float]
    stage7: dict              # stage → 平均分钟（不含昨晚）
    hr_base7: Optional[float]
    rr_base7: Optional[float]
    tst7: Optional[float]
    waso7: Optional[float]
    awake7: Optional[float]

    @property
    def baseline_ready(self) -> bool:
        return self.sol7 is not None


def compute_data_state(profile: UserProfile, tz) -> str:
    """AN_DATA_STATE：empty / single_night / baseline7_ready / baseline30_ready。"""
    today = datetime.datetime.now(tz).date()
    n30 = len(records_in_window(profile, end_date=today, days=30, tz=tz))
    if n30 == 0:
        return "empty"
    if n30 == 1:
        return "single_night"
    if n30 >= R["data_state"]["baseline30_min_nights"]:
        return "baseline30_ready"
    if len(records_in_window(profile, end_date=today, days=7, tz=tz)) >= R["data_state"]["baseline7_min_nights"]:
        return "baseline7_ready"
    return "single_night"


def compute_baselines(profile: UserProfile, tz) -> Baseline:
    today = datetime.datetime.now(tz).date()
    nights_7d = records_in_window(profile, end_date=today, days=7, tz=tz)
    nights_30d = records_in_window(profile, end_date=today, days=30, tz=tz)
    nights_prev_7d = records_in_window(profile, end_date=today - datetime.timedelta(days=7), days=7, tz=tz)
    nights_prev_30d = records_in_window(profile, end_date=today - datetime.timedelta(days=30), days=30, tz=tz)
    latest = nights_7d[-1] if nights_7d else (nights_30d[-1] if nights_30d else None)
    # 基线不含昨晚（昨晚是被比较对象）
    base_nights = [r for r in nights_7d if r is not latest]
    sol_meas = [r.onset for r in base_nights if r.onset is not None]
    sol7 = _mean(sol_meas) if len(sol_meas) >= R["data_state"]["baseline7_min_nights"] else None
    sol30_meas = [r.onset for r in nights_30d if r is not latest and r.onset is not None]
    sol30 = _mean(sol30_meas) if len(sol30_meas) >= R["data_state"]["baseline30_min_nights"] else None

    stage7: dict[str, float] = {}
    for stage in ("rem", "core", "deep", "awake"):
        vals = []
        for r in base_nights:
            s = r.sequence_summaries if r.sleep_status else {}
            if s:
                key = {"rem": "rem_sleep_duration", "core": "core_sleep_duration",
                       "deep": "deep_sleep_duration", "awake": "night_awake_duration"}[stage]
                vals.append(s.get(key) or 0)
        stage7[stage] = _mean(vals) if vals else None

    def _n(key):
        return _mean([getattr(r, key) for r in base_nights if getattr(r, key) is not None])

    tst = []
    waso = []
    for r in base_nights:
        s = r.sequence_summaries if r.sleep_status else {}
        if s:
            tst.append((s.get("time_in_bed") or 0) - (s.get("night_awake_duration") or 0))
            waso.append(s.get("night_awake_duration") or 0)
    return Baseline(
        tz=tz, today=today, latest=latest,
        nights_7d=nights_7d, nights_30d=nights_30d,
        nights_prev_7d=nights_prev_7d, nights_prev_30d=nights_prev_30d,
        sol7=sol7, sol30=sol30, stage7=stage7,
        hr_base7=_n("hr_before_sleep"), rr_base7=_n("rr_before_sleep"),
        tst7=_mean(tst) if tst else None,
        waso7=_mean(waso) if waso else None,
        awake7=_mean([r.sequence_summaries.get("night_awake_count") for r in base_nights
                      if r.sleep_status and r.sequence_summaries.get("night_awake_count") is not None]),
    )


# ── 场景归因（与 profile_service 16h 回溯同口径，本地实现避免循环依赖）──────────

SCENE_LOOKBACK_SEC = 16 * 3600


def attribute_scene_to_night(mindora_record: dict, night_ts: Optional[int]) -> Optional[str]:
    """夜间记录时间戳往前回溯 16h 内、最接近记录时间的一次场景使用。"""
    if not night_ts:
        return None
    best_scene, best_delta = None, None
    for scene_id, records in (mindora_record or {}).items():
        for entry in records or []:
            if not isinstance(entry, (list, tuple)) or not entry:
                continue
            try:
                ts = int(entry[0])
            except (TypeError, ValueError):
                continue
            delta = night_ts - ts
            if 0 <= delta <= SCENE_LOOKBACK_SEC and (best_delta is None or delta < best_delta):
                best_scene, best_delta = scene_id, delta
    return best_scene


def scene_uses_in_window(mindora_record: dict, scene_id: str, *, end_ts: int, days: int) -> int:
    cutoff = end_ts - days * 86400
    count = 0
    for entry in (mindora_record or {}).get(scene_id) or []:
        if isinstance(entry, (list, tuple)) and entry:
            try:
                if int(entry[0]) >= cutoff:
                    count += 1
            except (TypeError, ValueError):
                continue
    return count


def top_scenes_in_window(mindora_record: dict, *, end_ts: int, days: int, limit: int = 3) -> list[tuple[str, int]]:
    cutoff = end_ts - days * 86400
    counts: dict[str, int] = {}
    for scene_id, records in (mindora_record or {}).items():
        n = 0
        for entry in records or []:
            if isinstance(entry, (list, tuple)) and entry:
                try:
                    if int(entry[0]) >= cutoff:
                        n += 1
                except (TypeError, ValueError):
                    continue
        if n:
            counts[short_scene_id(scene_id)] = n
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]


def scene_night_stats(profile: UserProfile, scene_id: str, *, end_date: datetime.date, days: int, tz) -> dict:
    """该场景在窗口内被归因到的夜晚：样本数、平均评分/入睡用时（SC_ASSOC 降级口径：
    对每个夜晚做 16h 回溯归因，与该场景匹配才计入）。"""
    short = short_scene_id(scene_id)
    scores, onsets, n = [], [], 0
    today_ts = int(datetime.datetime.combine(end_date, datetime.time.max, tzinfo=tz).timestamp())
    for r in records_in_window(profile, end_date=end_date, days=days, tz=tz):
        s = attribute_scene_to_night(profile.mindora_record, r.timestamp)
        if s and short_scene_id(s) == short:
            n += 1
            if r.sleep_quality is not None:
                scores.append(r.sleep_quality)
            if r.onset is not None:
                onsets.append(r.onset)
    return {"nights": n, "avg_score": _mean(scores), "avg_sol": _mean(onsets),
            "end_ts": today_ts}


def scene_display_name(scene_id: str) -> str:
    return short_scene_id(scene_id).replace("_", " ").title()


def scene_type_label(scene_id: str, lang: str) -> Optional[str]:
    sid = short_scene_id(scene_id).lower()
    for keywords, kind in _SCENE_TYPE_KEYWORDS:
        if any(k in sid for k in keywords.split()):
            return _SCENE_TYPE_LABEL[kind].get(_canonical_lang(lang)) or _SCENE_TYPE_LABEL[kind]["en"]
    return None


# ── 夜间规则 ────────────────────────────────────────────────────────────────


def rule_greeting(profile: UserProfile, data_state: str, lang: str) -> RuleConclusion:
    state_key = {"single_night": "greeting_state_single", "baseline7_ready": "greeting_state_7d",
                 "baseline30_ready": "greeting_state_30d"}.get(data_state, "greeting_state_empty")
    title_key = {"single_night": "title_single", "baseline7_ready": "title_baseline7",
                 "baseline30_ready": "title_baseline30"}.get(data_state, "title_greeting")
    nickname = _nickname(profile)
    text = _safe_render("greeting", {"nickname": nickname, "state_clause": _t(state_key, lang)}, lang,
                        _t(state_key, lang))
    return RuleConclusion(key="greeting", theme="greeting", title=_t(title_key, lang), text=text,
                          state=data_state, template_key="greeting", facts_only=data_state in ("empty", "single_night"),
                          valid_nights=0, magnitude=0.0, actionability=0)


def _vitals_direction(last_val, base_val) -> Optional[tuple[str, str]]:
    """降级口径：昨晚值 vs 基线均值 → (zh方向词, en方向词)。"""
    if last_val is None or base_val in (None, 0):
        return None
    dev = (last_val - base_val) / base_val
    if abs(dev) < 0.05:
        return ("心率与呼吸接近近期平均水平", "close to your recent average")
    if dev > 0:
        return ("心率与呼吸略高于近期平均水平", "slightly above your recent average")
    return ("心率与呼吸略低于近期平均水平", "slightly below your recent average")


def rule_onset(profile: UserProfile, base: Baseline, data_state: str, lang: str) -> RuleConclusion:
    latest = base.latest
    if latest is None or latest.onset is None:
        text = _t("onset_no_measure", lang)
        return RuleConclusion(key="onset", theme="onset", title=_t("title_onset_facts", lang),
                              text=text, state="facts_only", template_key="onset_no_measure",
                              facts_only=True, valid_nights=1)
    last_sol = int(round(latest.onset))
    scene_id = attribute_scene_to_night(profile.mindora_record, latest.timestamp)
    scene_uses = scene_uses_in_window(profile.mindora_record, scene_id, end_ts=latest.timestamp, days=7) if scene_id else 0
    scene_clause = ""
    if scene_id:
        scene_clause = _t("scene_clause_assoc" if scene_uses >= R["scene"]["assoc_min_uses_7d"]
                          else "scene_clause_pref", lang).format(scene=scene_display_name(scene_id))
    hr_dir = _vitals_direction(latest.hr_before_sleep, base.hr_base7)
    vitals_clause = ""
    if hr_dir:
        vitals_clause = _t("vitals_clause", lang).format(hr_direction_zh=hr_dir[0], hr_direction_en=hr_dir[1])

    if base.sol7 is None:
        text = _safe_render("onset_facts", {"last_sol": last_sol, "vitals_clause": vitals_clause}, lang,
                            f"Last night's sleep onset was {last_sol} min.")
        return RuleConclusion(key="onset", theme="onset", title=_t("title_onset_facts", lang), text=text,
                              state="facts_only", template_key="onset_facts", facts_only=True,
                              valid_nights=len(base.nights_7d),
                              variables={"last_sol": last_sol})

    delta = latest.onset - base.sol7
    thr = R["onset"]["delta_stable_min"]
    baseline7 = int(round(base.sol7))
    if delta <= -thr:
        state, tpl, title_key = "faster", "onset_faster", "title_onset_faster"
        delta_txt, direction, direction_en = int(round(abs(delta))), "更快", "faster"
    elif delta >= thr:
        state, tpl, title_key = "slower", "onset_slower", "title_onset_slower"
        delta_txt, direction, direction_en = int(round(delta)), "更慢", "slower"
    else:
        state, tpl, title_key = "stable", "onset_stable", "title_onset_stable"
        delta_txt, direction, direction_en = int(round(abs(delta))), "基本持平", "in line with"
    variables = {"delta": delta_txt, "baseline7": baseline7, "last_sol": last_sol,
                 "vitals_clause": vitals_clause, "scene_clause": scene_clause,
                 "direction": direction, "direction_en": direction_en}
    facts = f"Last night's onset {last_sol} min vs 7-day baseline {baseline7} min."
    text = _safe_render(tpl, variables, lang, facts)
    action = ""
    if state == "slower":
        action = {"zh-Hans": "今晚提前调暗灯光、远离屏幕，用熟悉的场景放松。",
                  "zh-Hant": "今晚提前調暗燈光、遠離屏幕，用熟悉的場景放鬆。",
                  "en": "Tonight, dim the lights early, avoid screens, and unwind with a familiar scene."}.get(
                      _canonical_lang(lang), "Tonight, dim the lights early, avoid screens, and unwind with a familiar scene.")
    return RuleConclusion(key="onset", theme="onset", title=_t(title_key, lang), text=text,
                          evidence=[f"SOL {last_sol}min vs baseline7 {baseline7}min"],
                          action=action, state=state, template_key=tpl, variables=variables,
                          facts_only=False, valid_nights=len(base.nights_7d),
                          magnitude=abs(delta), actionability=2 if action else 1)


_STAGE_LABEL = {
    "rem":   {"zh-Hans": "REM", "zh-Hant": "REM", "en": "REM"},
    "core":  {"zh-Hans": "核心", "zh-Hant": "核心", "en": "core"},
    "deep":  {"zh-Hans": "深睡", "zh-Hant": "深睡", "en": "deep"},
    "awake": {"zh-Hans": "清醒", "zh-Hant": "清醒", "en": "awake"},
}


def rule_structure(profile: UserProfile, base: Baseline, data_state: str, lang: str) -> RuleConclusion:
    latest = base.latest
    stage_labels = {"zh-Hans": {"rem": "REM", "core": "核心", "deep": "深睡", "awake": "清醒"},
                    "zh-Hant": {"rem": "REM", "core": "核心", "deep": "深睡", "awake": "清醒"},
                    "en": {"rem": "REM", "core": "core", "deep": "deep", "awake": "awake"}}
    SL = stage_labels.get(_canonical_lang(lang), stage_labels["en"])
    if latest is None:
        return RuleConclusion(key="architecture", theme="structure", title=_t("title_structure_facts", lang),
                              text="", visible=False, state="facts_only", facts_only=True)
    summ = latest.sequence_summaries if latest.sleep_status else {}
    # 基线门槛：近 7 日有效夜晚数（不含昨晚做基线的夜晚由 compute_baselines 控制；
    # 这里只要求窗口内总夜晚够门槛且昨晚有阶段数据），与 SOL 是否可测无关
    if len(base.nights_7d) < R["data_state"]["baseline7_min_nights"] or not summ:
        tst_min = int(round((summ.get("time_in_bed", 0) - summ.get("night_awake_duration", 0)))) if summ else 0
        continuous = _longest_continuous(latest)
        hours, mins = divmod(max(tst_min, 0), 60)
        variables = {"tst": f"{hours}h{mins:02d}m" if hours else f"{mins}m", "continuous": continuous or 0}
        text = _safe_render("structure_facts", variables, lang,
                            f"Total sleep {variables['tst']}, longest continuous block {variables['continuous']} min.")
        return RuleConclusion(key="architecture", theme="structure", title=_t("title_structure_facts", lang),
                              text=text, state="facts_only", template_key="structure_facts",
                              variables=variables, facts_only=True, valid_nights=len(base.nights_7d))

    minor, major = R["structure"]["minor_delta_pct"], R["structure"]["major_delta_pct"]
    last_dur = {"rem": summ.get("rem_sleep_duration", 0), "core": summ.get("core_sleep_duration", 0),
                "deep": summ.get("deep_sleep_duration", 0), "awake": summ.get("night_awake_duration", 0)}
    deltas: dict[str, float] = {}
    for stage, base_mean in base.stage7.items():
        if not base_mean:
            continue
        deltas[stage] = (last_dur.get(stage, 0) - base_mean) / base_mean * 100
    n_major = sum(1 for d in deltas.values() if abs(d) > major)
    n_minor = sum(1 for d in deltas.values() if minor < abs(d) <= major)
    if n_major >= 2:
        state, tpl, title_key = "marked_change", "structure_marked", "title_structure_marked"
    elif n_major == 1 or n_minor >= 1:
        state, tpl, title_key = "mild_change", "structure_mild", "title_structure_mild"
    else:
        state, tpl, title_key = "near_normal", "structure_near_normal", "title_structure_normal"

    stage_sentence = ""
    if state != "near_normal" and deltas:
        top = max(deltas.items(), key=lambda kv: abs(kv[1]))
        if abs(top[1]) > minor:
            direction = "偏多" if top[1] > 0 else "偏少"
            direction_en = "more" if top[1] > 0 else "less"
            stage_sentence = _t("stage_sentence", lang).format(
                stage=SL[top[0]], direction=direction, direction_en=direction_en,
                pct=int(round(abs(top[1]))))
    variables = {"stage_sentence": stage_sentence}
    text = _t(tpl, lang).format(**variables)
    evidence = [f"{SL[s]} Δ{d:+.0f}%" for s, d in deltas.items()]
    return RuleConclusion(key="architecture", theme="structure", title=_t(title_key, lang), text=text,
                          evidence=evidence, state=state, template_key=tpl, variables=variables,
                          facts_only=False, valid_nights=len(base.nights_7d),
                          magnitude=max((abs(d) for d in deltas.values()), default=0.0),
                          actionability=1 if state == "marked_change" else 0)


def _longest_continuous(record: SleepResult) -> Optional[int]:
    best = cur = 0.0
    for e in record.sleep_status or []:
        if e.sleep_type == "awake":
            best, cur = max(best, cur), 0.0
        else:
            cur += e.duration
    best = max(best, cur)
    return int(best) if best > 0 else None


def _awake_events(record: SleepResult) -> list:
    min_min = R["fluctuation"]["awake_min_minutes"]
    return [e for e in record.sleep_status or [] if e.sleep_type == "awake" and e.duration >= min_min]


def _interventions_for_awake(record: SleepResult, awake_start_ts: int) -> list:
    """觉醒开始后 intervention_window_min 内的设备干预事件。"""
    window = R["fluctuation"]["intervention_window_min"] * 60
    events = list(record.night_events or [])
    for seq in record.sleep_status or []:
        events.extend(seq.events or [])
    out = []
    for e in events:
        if getattr(e, "event_type", None) == "intervention" and awake_start_ts <= e.timestamp <= awake_start_ts + window:
            out.append(e)
    return out


def rule_fluctuation(profile: UserProfile, base: Baseline, data_state: str, lang: str) -> RuleConclusion:
    latest = base.latest
    if latest is None:
        return RuleConclusion(key="intervention", theme="fluctuation", visible=False,
                              title=_t("title_fluctuation_stable", lang), text="", state="facts_only")
    events = _awake_events(latest)
    if not events:
        text = _t("fluctuation_stable", lang)
        return RuleConclusion(key="intervention", theme="fluctuation", title=_t("title_fluctuation_stable", lang),
                              text=text, state="stable_night", template_key="fluctuation_stable",
                              facts_only=True, valid_nights=len(base.nights_7d), magnitude=0.0, actionability=0)
    tz = base.tz
    parts = []
    for e in events[:R["fluctuation"]["expand_max"]]:
        start_ts = int(e.start_time)
        hhmm = datetime.datetime.fromtimestamp(start_ts, tz).strftime("%H:%M")
        # 干预配对
        ivs = _interventions_for_awake(latest, start_ts)
        iv_clause = ""
        if ivs:
            iv = ivs[0]
            itime = datetime.datetime.fromtimestamp(iv.timestamp, tz).strftime("%H:%M")
            iv_clause = _t("intervention_clause", lang).format(itime=itime, iaction=iv.detail or "intervention")
        # 再次入睡：该觉醒段结束后到下一非 awake 段开始的分钟数
        resleep = 0
        segs = latest.sleep_status or []
        for i, seg in enumerate(segs):
            if seg is e and i + 1 < len(segs) and segs[i + 1].sleep_type != "awake":
                gap = int(segs[i + 1].start_time) - int(e.start_time + e.duration * 60)
                resleep = max(int(round(gap / 60)), 0)
                break
        parts.append(_t("fluctuation_event", lang).format(
            time=hhmm, awake_min=int(round(e.duration)), resleep=resleep,
            vital_clause="", intervention_clause=iv_clause))
    waso = int(round(latest.sequence_summaries.get("night_awake_duration", 0))) if latest.sleep_status else 0
    if len(events) > R["fluctuation"]["list_max"]:
        events_text = " ".join(parts)
        text = _t("fluctuation_summary", lang).format(count=len(events), waso=waso, events=events_text)
        tpl = "fluctuation_summary"
    else:
        text = " ".join(parts)
        tpl = "fluctuation_event"
    return RuleConclusion(key="intervention", theme="fluctuation",
                          title=_t("title_fluctuation_events", lang), text=text,
                          evidence=[f"awake×{len(events)}, WASO {waso}min"],
                          state="events" if len(events) < 3 else "frequent_events",
                          template_key=tpl, facts_only=False,
                          valid_nights=len(base.nights_7d), magnitude=float(waso), actionability=0)


def rule_scene(profile: UserProfile, base: Baseline, data_state: str, lang: str) -> RuleConclusion:
    latest = base.latest
    scene_id = attribute_scene_to_night(profile.mindora_record, latest.timestamp) if latest else None
    top = top_scenes_in_window(profile.mindora_record, end_ts=int(time.time()), days=7, limit=3)

    def _pref_fallback():
        if top:
            name, uses = top[0]
            text = _t("scene_no_scene", lang).format(scene=name.replace("_", " ").title(), uses=uses)
        else:
            text = ""
        return RuleConclusion(key="scene_preference", theme="scene", title=_t("title_scene", lang),
                              text=text, state="facts_only", facts_only=True,
                              valid_nights=len(base.nights_7d))

    if not scene_id:
        return _pref_fallback()

    name = scene_display_name(scene_id)
    uses_7d = scene_uses_in_window(profile.mindora_record, scene_id, end_ts=latest.timestamp, days=7)
    stats = scene_night_stats(profile, scene_id, end_date=base.today, days=7, tz=base.tz)
    assoc_ok = stats["nights"] >= R["scene"]["assoc_min_uses_7d"] and stats["avg_score"] is not None

    segments = [_t("scene_facts", lang).format(scene=name)]
    stype = scene_type_label(scene_id, lang)
    if stype:
        segments.append(_t("scene_content", lang).format(scene_type=stype))
    if assoc_ok:
        segments.append(_t("scene_assoc", lang).format(
            nights=stats["nights"], score=round(stats["avg_score"], 1),
            sol=int(round(stats["avg_sol"])) if stats["avg_sol"] is not None else "-"))
    else:
        segments.append(_t("scene_pref", lang).format(uses=uses_7d))
    # 推荐 = 近7天第二常用场景（SC_RECOMMEND 降级，无风险标签库，待产品确认）
    others = [(s, c) for s, c in top if s != short_scene_id(scene_id)]
    if others:
        segments.append(_t("scene_recommend", lang).format(
            recommend=others[0][0].replace("_", " ").title()))
    text = "".join(segments)
    evidence = ([f"{name}: {stats['nights']} nights, avg score {round(stats['avg_score'],1)}"]
                if assoc_ok else [f"{name}: {uses_7d} uses in 7d"])
    return RuleConclusion(key="scene_preference", theme="scene", title=_t("title_scene", lang), text=text,
                          evidence=evidence, state="association" if assoc_ok else "preference",
                          template_key="scene_facts", facts_only=not assoc_ok,
                          valid_nights=len(base.nights_7d),
                          magnitude=0.0, actionability=1,
                          variables={"scene": name, "uses": uses_7d})


def build_night_conclusions(profile: UserProfile, lang: str) -> tuple:
    """一次算出数据状态 + 基线 + 5 个夜间模块结论（greeting/onset/architecture/intervention/scene）。"""
    tz = resolve_tz(getattr(profile, "last_request_timezone", None))
    data_state = compute_data_state(profile, tz)
    base = compute_baselines(profile, tz)
    conclusions = [
        rule_greeting(profile, data_state, lang),
        rule_onset(profile, base, data_state, lang),
        rule_structure(profile, base, data_state, lang),
        rule_fluctuation(profile, base, data_state, lang),
        rule_scene(profile, base, data_state, lang),
    ]
    return data_state, base, conclusions


def micro_education(profile: UserProfile, lang: str) -> RuleConclusion:
    """模块5 轻量睡眠知识：按自然日轮换，不依赖 LLM。"""
    fact = _EDU_FACTS[int(time.time()) // 86400 % len(_EDU_FACTS)]
    canonical = _canonical_lang(lang)
    text = fact.get(canonical) or fact["en"]
    titles = {"zh-Hans": "睡眠小知识", "zh-Hant": "睡眠小知識", "en": "Sleep tip"}
    return RuleConclusion(key="micro_education", theme="education",
                          title=titles.get(canonical, titles["en"]), text=text,
                          evidence=[], action="", state="edu", facts_only=True)


# ── 长期记忆（建议历史 / 昨日首页主题）──────────────────────────────────────


def insight_memory(profile: UserProfile) -> dict:
    """profile.sleep_analysis['insight_memory']：建议历史 + 昨日首页主题（长期记忆）。"""
    sa = profile.sleep_analysis if isinstance(profile.sleep_analysis, dict) else {}
    mem = sa.get("insight_memory")
    if not isinstance(mem, dict):
        mem = {"advice_history": [], "last_home_theme": None, "last_home_date": None}
        sa["insight_memory"] = mem
    mem.setdefault("advice_history", [])
    return mem


def record_generation_memory(profile: UserProfile, *, advice_types: list[str],
                             home_theme: Optional[str], date_str: str, now: Optional[int] = None) -> None:
    mem = insight_memory(profile)
    today = date_str
    existing = {(item.get("type"), item.get("date")) for item in mem["advice_history"]}
    for t in advice_types:
        if (t, today) not in existing:
            mem["advice_history"].append({"type": t, "date": today})
    max_entries = R["advice"]["history_max_entries"]
    if len(mem["advice_history"]) > max_entries:
        mem["advice_history"] = mem["advice_history"][-max_entries:]
    if home_theme:
        mem["last_home_theme"] = home_theme
        mem["last_home_date"] = today
    mem["updated_at"] = now or int(time.time())


def _advice_recently_given(mem: dict, advice_type: str, today: datetime.date, tz) -> bool:
    cooldown = R["advice"]["same_type_cooldown_days"]
    for item in mem.get("advice_history", []):
        if item.get("type") != advice_type:
            continue
        try:
            d = datetime.date.fromisoformat(item.get("date", ""))
        except ValueError:
            continue
        if 0 <= (today - d).days <= cooldown:
            return True
    return False


# ── 建议（AN_ADVICE）────────────────────────────────────────────────────────


def rule_advice(profile: UserProfile, base: Baseline, data_state: str,
                conclusions: list, lang: str) -> RuleConclusion:
    """规则建议库：按已生成结论触发，7 天同类去重，每日最多 max_per_day 条；
    数据不足时只给「继续记录」建议（规范 :3447）。"""
    canonical = _canonical_lang(lang)
    tz = base.tz
    today = datetime.datetime.now(tz).date()
    mem = insight_memory(profile)
    by_key = {c.key: c for c in conclusions}

    picked: list[str] = []
    variables = {"scene": ""}
    scene_c = by_key.get("scene_preference")
    if scene_c and scene_c.variables.get("scene"):
        variables["scene"] = scene_c.variables["scene"]

    def _text(tpl_key: str) -> str:
        return _t(tpl_key, lang).format(**variables)

    if data_state in ("empty", "single_night"):
        picked = ["advice_record_more"]
    else:
        onset_c = by_key.get("onset")
        if onset_c and onset_c.state == "slower":
            picked.append("advice_onset_routine")
        struct_c = by_key.get("architecture")
        if struct_c and struct_c.state == "marked_change":
            picked.append("advice_structure_regular")
        fluc_c = by_key.get("intervention")
        if fluc_c and fluc_c.state == "frequent_events":
            picked.append("advice_fluctuation_winddown")
        if scene_c and scene_c.state == "association":
            picked.append("advice_scene_consistency")
        # 数据质量门 + 去重 + 条数上限
        picked = [p for p in picked if not _advice_recently_given(mem, p, today, tz)]
        picked = picked[:R["advice"]["max_per_day"]]
        if not picked:
            picked = ["advice_record_more"] if not _advice_recently_given(mem, "advice_record_more", today, tz) else []

    sentences = [_text(p) for p in picked]
    titles = {"zh-Hans": "睡眠建议", "zh-Hant": "睡眠建議", "en": "Sleep advice"}
    return RuleConclusion(key="advice", theme="advice", title=titles.get(canonical, titles["en"]),
                          text=" ".join(s for s in sentences if s),
                          evidence=[f"advice:{p}" for p in picked],
                          action="", state="advice", template_key=picked[0] if picked else "",
                          variables=variables, facts_only=False,
                          valid_nights=len(base.nights_7d),
                          magnitude=0.0, actionability=2,
                          advice_types=picked)  # type: ignore[attr-defined]


# ── 首页摘要（AN_HOME_SUMMARY）──────────────────────────────────────────────


_HOME_TPL = {"onset": "home_onset", "structure": "home_structure",
             "fluctuation": "home_fluctuation", "scene": "home_scene"}
_HOME_THEME_ORDER = ["onset", "structure", "fluctuation", "scene"]


def rule_home_summary(conclusions: list, memory: dict, lang: str,
                      today: datetime.date) -> RuleConclusion:
    """按 数据完整度→变化幅度→可行动性→最近性 选 1 条；避免连续两天同主题。"""
    candidates = [c for c in conclusions
                  if c.theme in _HOME_TPL and c.visible and c.text]
    if not candidates:
        return RuleConclusion(key="home_summary", theme="none", title="", text="", visible=False)
    last_theme = memory.get("last_home_theme")
    last_date = memory.get("last_home_date")
    cooldown = R["home_summary"]["theme_cooldown_days"]
    if last_theme and last_date:
        try:
            d = datetime.date.fromisoformat(last_date)
            if (today - d).days <= cooldown:
                candidates = [c for c in candidates if c.theme != last_theme] or candidates
        except ValueError:
            pass

    def _rank(c: RuleConclusion):
        return (-c.valid_nights, -c.magnitude, -c.actionability,
                _HOME_THEME_ORDER.index(c.theme) if c.theme in _HOME_THEME_ORDER else 99)

    chosen = sorted(candidates, key=_rank)[0]
    v: dict = {}
    if chosen.theme == "onset":
        v = {"direction": "更快" if chosen.state == "faster" else "更慢" if chosen.state == "slower" else "基本持平",
             "direction_en": "faster than" if chosen.state == "faster" else "slower than" if chosen.state == "slower" else "in line with"}
    elif chosen.theme == "structure":
        level_map = {"near_normal": ("基本接近", "close to"), "mild_change": ("有轻微变化", "slightly changed from"),
                     "marked_change": ("变化较明显", "noticeably different from")}
        v = {"level": level_map.get(chosen.state, ("基本接近", "close to"))[0] if _canonical_lang(lang).startswith("zh") else level_map.get(chosen.state, ("基本接近", "close to"))[1],
             "level_en": level_map.get(chosen.state, ("基本接近", "close to"))[1]}
        if _canonical_lang(lang).startswith("zh"):
            v["level_en"] = v["level"]
    elif chosen.theme == "fluctuation":
        n = len(_awake_events_convenient(chosen))
        v = {"count": n}
    elif chosen.theme == "scene":
        v = {"scene": chosen.variables.get("scene", "")}
    text = _safe_render(_HOME_TPL[chosen.theme], v, lang, chosen.text[:120])
    return RuleConclusion(key="home_summary", theme=chosen.theme, title=chosen.title, text=text,
                          evidence=chosen.evidence, action=chosen.action, visible=True,
                          state=chosen.state, template_key=_HOME_TPL[chosen.theme],
                          variables=v, facts_only=False, valid_nights=chosen.valid_nights,
                          magnitude=chosen.magnitude, actionability=chosen.actionability,
                          home_theme=chosen.theme)  # type: ignore[attr-defined]


def _awake_events_convenient(c: RuleConclusion) -> list:
    """从结论 evidence 中取夜间清醒次数（'awake×N'），首页摘要变量用。"""
    for e in c.evidence or []:
        if isinstance(e, str) and e.startswith("awake×"):
            try:
                return [0] * int(e.split("×")[1].split(",")[0])
            except (ValueError, IndexError):
                return []
    return []


# ── 趋势（AN_TREND_7D / AN_TREND_30D）──────────────────────────────────────


def _fmt_minutes(mins: Optional[float]) -> str:
    if mins is None:
        return "-"
    mins = int(round(mins))
    h, m = divmod(max(mins, 0), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def rule_trend(profile: UserProfile, base: Baseline, lang: str, days: int) -> RuleConclusion:
    """当前窗口 vs 前一窗口（同口径）对比 + MAD 稳定性 + 最小变化阈值过滤。
    30 天前一窗口不可得（sleep_data 只保留 30 晚）时按规范降级为分布+稳定性描述。"""
    tz = base.tz
    canonical = _canonical_lang(lang)
    cur = base.nights_7d if days == 7 else base.nights_30d
    prev = base.nights_prev_7d if days == 7 else base.nights_prev_30d
    min_valid = R["trend"]["min_valid_7d" if days == 7 else "min_valid_30d"]
    max_items = R["trend"]["max_items_7d" if days == 7 else "max_items_30d"]
    title_key = "title_trend7" if days == 7 else "title_trend30"

    if len(cur) < min_valid:
        return RuleConclusion(key=f"trend{days}", theme=f"trend{days}", title=_t(title_key, lang),
                              text=_t("trend_stable", lang).format(days=days) if cur else "",
                              visible=False, state="insufficient_data", facts_only=True,
                              valid_nights=len(cur))

    def _metrics(nights):
        tst, sol, waso, awc, fst = [], [], [], [], []
        for r in nights:
            s = r.sequence_summaries if r.sleep_status else {}
            if s:
                tst.append((s.get("time_in_bed") or 0) - (s.get("night_awake_duration") or 0))
                waso.append(s.get("night_awake_duration") or 0)
                awc.append(s.get("night_awake_count") or 0)
            if r.onset is not None:
                sol.append(r.onset)
            if r.first_sleep_time:
                fst.append(r.first_sleep_time)
        return {"tst": _mean(tst), "sol": _mean(sol), "waso": _mean(waso),
                "awake_count": _mean(awc), "first_sleep": _circular_mean_hhmm(fst),
                "series": {"tst": tst, "sol": sol, "waso": waso, "awake_count": awc}}

    cur_m = _metrics(cur)
    min_change = R["trend"]["min_change"]

    # 30d 前一窗口不足 → 分布 + 稳定性描述（规范 :3107）
    if days == 30 and len(prev) < R["trend"]["min_valid_30d"]:
        mad = _mad([(r.sequence_summaries.get("time_in_bed", 0) - r.sequence_summaries.get("night_awake_duration", 0))
                    for r in cur if r.sleep_status])
        stability = _t("stability_high" if (mad or 0) < 45 else "stability_low", lang)
        text = _t("trend_dist_30d", lang).format(
            metric=_t("metric_tst", lang), current=_fmt_minutes(cur_m["tst"]),
            n=len(cur), stability=stability, metric_en=_t("metric_tst", "en"))
        return RuleConclusion(key="trend30", theme="trend30", title=_t(title_key, lang), text=text,
                              evidence=[f"valid nights {len(cur)}"],
                              state="distribution_only", template_key="trend_dist_30d",
                              facts_only=True, valid_nights=len(cur))

    prev_m = _metrics(prev)
    items = []

    def _add(metric_key, tpl_key, cur_v, prev_v, min_chg, fmt, later_is_up=True):
        if cur_v is None or prev_v is None:
            return
        delta = cur_v - prev_v
        if abs(delta) < min_chg:
            return
        up = delta > 0
        direction = _t("dir_up" if up else "dir_down", lang) if metric_key != "first_sleep" else _t("dir_higher" if up else "dir_lower", lang)
        # 稳定性：窗口内 MAD 不超过最小变化阈值 → 波动较小
        mad = _mad(cur_m["series"].get(metric_key, []))
        stability = _t("stability_high" if (mad is None or mad <= min_chg) else "stability_low", lang)
        items.append((abs(delta), _t("trend_item", lang).format(
            metric=_t(tpl_key, lang), metric_en=_t(tpl_key, "en"),
            current=fmt(cur_v),
            direction=direction, direction_en=_t("dir_up" if up else "dir_down", "en") if metric_key != "first_sleep" else _t("dir_higher" if up else "dir_lower", "en"),
            delta=fmt(abs(delta)),
            stability_sentence=stability)))

    _add("tst", "metric_tst", cur_m["tst"], prev_m["tst"], min_change["tst_min"], _fmt_minutes)
    _add("sol", "metric_sol", cur_m["sol"], prev_m["sol"], min_change["sol_min"], _fmt_minutes)
    _add("waso", "metric_waso", cur_m["waso"], prev_m["waso"], min_change["waso_min"], _fmt_minutes)
    _add("awake_count", "metric_awake_count", cur_m["awake_count"], prev_m["awake_count"],
         min_change["awake_count"], lambda v: f"{v:.1f}" if v is not None else "-")
    # 时钟指标：圆周均值差（分钟）
    if cur_m["first_sleep"] and prev_m["first_sleep"]:
        def _to_min(hhmm):
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        c, p = _to_min(cur_m["first_sleep"]), _to_min(prev_m["first_sleep"])
        delta = ((c - p + 720) % 1440) - 720  # 跨午夜安全差值
        if abs(delta) >= min_change["clock_min"]:
            direction = _t("dir_higher" if delta > 0 else "dir_lower", lang)
            items.append((abs(delta), _t("trend_item", lang).format(
                metric=_t("metric_first_sleep", lang), metric_en=_t("metric_first_sleep", "en"),
                current=cur_m["first_sleep"], direction=direction,
                direction_en=_t("dir_higher" if delta > 0 else "dir_lower", "en"),
                delta=f"{abs(delta)}min", stability_sentence=_t("stability_high", lang))))

    items.sort(key=lambda x: -x[0])
    if not items:
        text = _t("trend_stable", lang).format(days=days)
        tpl = "trend_stable"
    else:
        text = " ".join(s for _, s in items[:max_items])
        tpl = "trend_item"
    return RuleConclusion(key=f"trend{days}", theme=f"trend{days}", title=_t(title_key, lang),
                          text=text, evidence=[f"valid nights {len(cur)} vs {len(prev)}"],
                          state="trend", template_key=tpl, facts_only=False,
                          valid_nights=len(cur), magnitude=items[0][0] if items else 0.0,
                          actionability=0)


# ── 展示指数（AN_ONSET_INDEX / AN_STRUCTURE_INDEX / AN_STABILITY_INDEX）──────


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def compute_insight_indices(profile: UserProfile, base: Baseline) -> dict:
    """三个展示指数（建议 v1 公式，权重/常量在 config.INSIGHT_RULES['indices']）。
    缺失子分按剩余权重归一；全部缺失返回 None。不动现有 sleep_quality 总分。"""
    p = R["indices"]["subscore_params"]
    latest = base.latest
    sub: dict[str, Optional[float]] = {}

    # 入睡表现：SOL 分 + 入睡前心率/呼吸趋势分（降级口径：昨晚 vs 基线 7 偏差）
    sol_s = hr_s = rr_s = None
    if latest is not None and latest.onset is not None:
        sol_s = _clamp(100 - max(0.0, latest.onset - p["sol_full_min"]) * p["sol_penalty_per_min"])
        if base.hr_base7:
            dev = abs((latest.hr_before_sleep or base.hr_base7) - base.hr_base7) / base.hr_base7 * 100
            hr_s = _clamp(100 - dev / p["trend_dev_full_pct"] * 100)
        if base.rr_base7:
            dev = abs((latest.rr_before_sleep or base.rr_base7) - base.rr_base7) / base.rr_base7 * 100
            rr_s = _clamp(100 - dev / p["trend_dev_full_pct"] * 100)
    sub["onset"] = {"sol": sol_s, "hr_trend": hr_s, "rr_trend": rr_s}

    # 睡眠结构：时长分 + 连续性分 + 阶段稳定分
    dur_s = cont_s = stage_s = None
    plan = getattr(profile, "sleep_plan", None)
    target = getattr(plan, "target_duration_min", None) if plan else None
    summ = latest.sequence_summaries if (latest and latest.sleep_status) else {}
    tst = (summ.get("time_in_bed", 0) - summ.get("night_awake_duration", 0)) if summ else None
    if tst is not None and target:
        dur_s = _clamp(tst / target * 100)
    waso = summ.get("night_awake_duration") if summ else None
    awc = summ.get("night_awake_count") if summ else None
    if waso is not None and awc is not None:
        cont_s = _clamp(100 - waso * p["waso_penalty_per_min"] - p["awake_extra_penalty"] * max(awc - 1, 0))
    deltas = []
    if latest is not None and summ:
        for stage, base_mean in base.stage7.items():
            if base_mean:
                key = {"rem": "rem_sleep_duration", "core": "core_sleep_duration",
                       "deep": "deep_sleep_duration", "awake": "night_awake_duration"}[stage]
                deltas.append(abs((summ.get(key, 0) - base_mean) / base_mean * 100))
    if deltas:
        stage_s = _clamp(100 - _mean([min(d, p["stage_delta_full_pct"]) for d in deltas]))
    sub["structure"] = {"duration": dur_s, "continuity": cont_s, "stage_stability": stage_s}

    # 夜间稳定度：连续性分 + 心率稳定分 + 呼吸稳定分
    hr_stab = rr_stab = None
    if latest is not None and latest.hr_min is not None and latest.hr_max and latest.hr_max > 0:
        hr_stab = _clamp(100 - (latest.hr_max - latest.hr_min) / latest.hr_max * 100 / p["hr_range_full_pct"] * 100)
    if latest is not None and latest.respiratory_var is not None:
        rr_stab = _clamp(100 - latest.respiratory_var * 100 / p["resp_var_full_pct"])
    sub["stability"] = {"continuity": cont_s, "hr_stability": hr_stab, "rr_stability": rr_stab}

    out = {}
    for kind, weights in (("onset", R["indices"]["onset"]["weights"]),
                          ("structure", R["indices"]["structure"]["weights"]),
                          ("stability", R["indices"]["stability"]["weights"])):
        vals = [(weights[k], v) for k, v in sub[kind].items() if v is not None]
        w_sum = sum(w for w, _ in vals)
        out[f"{kind}_index"] = int(round(sum(w * v for w, v in vals) / w_sum)) if w_sum > 0 else None
    out["state"] = compute_data_state(profile, base.tz)
    out["valid_nights"] = len(base.nights_7d)
    return out


def index_label(score: Optional[int], lang: str) -> Optional[str]:
    """AN_OVERVIEW 产品状态标签（非医学等级）；None → None。"""
    if score is None:
        return None
    for threshold, label in R["indices"]["label_bands"]:
        if score >= threshold:
            labels = {"excellent": {"zh-Hans": "优秀", "zh-Hant": "優秀", "en": "Excellent"},
                      "good": {"zh-Hans": "良好", "zh-Hant": "良好", "en": "Good"},
                      "fair": {"zh-Hans": "一般", "zh-Hant": "一般", "en": "Fair"}}[label]
            return labels.get(_canonical_lang(lang)) or labels["en"]
    return None
