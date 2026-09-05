"""
llm/analysis.py — LLM-powered sleep analysis text generation.

Wraps VolcEngineArkChat (llm/ark_chat.py) and generates the TEXT fields
for each /analysis response type.  Numeric fields (scores, durations, counts)
are always computed from real sensor data by user_server._build_* methods;
LLM only fills in human-readable titles, descriptions, labels, and advice.

Usage (from user_server.py):
    from llm import SleepAnalysisLLM, extract_sleep_context
    self.llm = SleepAnalysisLLM()

    # after building default response_data:
    if self.llm.enabled:
        ctx = extract_sleep_context(profile, req.data)
        llm_text = await self.llm.generate(req.request_type, ctx,
                                           req.data.language, req.data.modules)
        if llm_text:
            deep_merge(response_data, llm_text)
"""

import asyncio
import json
import logging
import os
import re
from functools import lru_cache
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from llm.ark_chat import VolcEngineArkChat
from llm.router import ModelRouter
from config import Config
from insight_rules_config import get_insight_rules as _get_insight_rules


from user_profile import compute_recent_sleep_stats


# ──────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────

def extract_sleep_context(profile, data) -> dict:
    """
    Pull key sleep metrics from UserProfile into a flat dict
    that can be embedded in an LLM prompt.

    Raw sleep sequences and behavior series are intentionally excluded.
    Sleep stage statistics are aggregated locally; only the stats are
    exposed, along with the ``sleep_analysis`` fields stored in the profile.

    Key 命名约定：
      - avg_* / typical_* / weekly_top_* : compute_recent_sleep_stats 的 7 天聚合
      - latest_*                          : 当夜（sleep_data 最后一条）明细
    prompt 模板只允许引用这里产出的 key，避免出现 None/'—' 占位符。
    """
    ctx: dict[str, Any] = {
        "date":       getattr(data, "date", None) or "",
        "start_date": getattr(data, "start_date", None) or "",
        "end_date":   getattr(data, "end_date", None) or "",
        "language":   getattr(data, "language", "en"),
    }

    if not profile:
        return ctx

    # Locally computed 7-day sleep statistics (no raw sequences).
    stats = compute_recent_sleep_stats(profile, days=7)
    ctx.update(stats)

    # 当夜明细（sleep_data 最后一条）——日视图 / 探索页的 prompt 需要夜级数据。
    latest = profile.sleep_data[-1] if profile.sleep_data else None
    if latest is not None:
        summaries = latest.sequence_summaries if latest.sleep_status else {}
        tb = summaries.get("time_in_bed") or 0
        ctx.update({
            "latest_score":           latest.sleep_quality,
            "latest_soe":             latest.soe,
            "latest_onset_min":       latest.onset,
            "latest_sleep_arch":      latest.sleep_arch_index,
            "latest_night_var":       latest.night_var_index,
            "latest_first_sleep_time": latest.first_sleep_time,
            "latest_hr_before_sleep": latest.hr_before_sleep,
            "latest_rr_before_sleep": latest.rr_before_sleep,
            "latest_hrv":             latest.hrv,
            "latest_hr_min":          latest.hr_min,
            "latest_hr_max":          latest.hr_max,
            "latest_deep_min":        summaries.get("deep_sleep_duration"),
            "latest_rem_min":         summaries.get("rem_sleep_duration"),
            "latest_core_min":        summaries.get("core_sleep_duration"),
            "latest_awake_count":     summaries.get("night_awake_count"),
            "latest_awake_min":       summaries.get("night_awake_duration"),
            "latest_awake_type":      summaries.get("night_awake_type"),
            "latest_deep_pct":        round(summaries.get("deep_sleep_duration", 0) / tb * 100, 1) if tb else None,
            "latest_rem_pct":         round(summaries.get("rem_sleep_duration", 0) / tb * 100, 1) if tb else None,
            "latest_core_pct":        round(summaries.get("core_sleep_duration", 0) / tb * 100, 1) if tb else None,
        })

    # 场景别名：weekly_top_scene_* → prompt 里的 scene_name / used_times
    # （旧 sleep_trend_week/month、scene.title/text 为死字段已移除——
    #   趋势由 insight_rules.rule_trend 现算，场景由 weekly_top_scene_* 覆盖）
    ctx["scene_name"] = stats.get("weekly_top_scene_title")
    ctx["used_times"] = stats.get("weekly_top_scene_count")

    # 去掉 None 值，让 prompt 模板里 ctx.get(key, '—') 的默认值真正生效
    # （key 存在但值为 None 时 .get 不会用默认值，会把 None 渲染进 prompt）
    return {k: v for k, v in ctx.items() if v is not None}


def deep_merge(base: dict, updates: dict) -> None:
    """
    Recursively merge `updates` into `base`, overwriting only non-empty string
    values.  Numeric / boolean values in `base` are never overwritten.
    Non-empty lists (e.g. LLM-localized scenario_list) overwrite list values.
    """
    for k, v in updates.items():
        if k not in base:
            base[k] = v
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        elif isinstance(v, list) and v and isinstance(base.get(k), list):
            base[k] = v
        elif isinstance(v, str) and v.strip():
            base[k] = v
        # skip None, empty strings, and non-string overrides of existing data


# ──────────────────────────────────────────────────────────────
# Prompt templates  (text fields only — no numeric placeholders)
# ──────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = (
    "You are a professional sleep health analyst for the Mindora app. "
    "Analyze the provided sleep data and return ONLY a JSON object — "
    "no markdown fences, no explanation, no trailing text. "
    "All string values must be written in {lang}. "
    "This overrides the language of the prompt itself. "
    "Keep every description to 1–2 sentences, warm and encouraging in tone."
)

_LANG_NAMES = {
    "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
    "en": "English", "ja": "日本語", "ko": "한국어",
    "de": "Deutsch", "fr": "Français", "it": "Italiano",
    "es": "Español", "id": "Bahasa Indonesia",
}

def _lang_name(language: str) -> str:
    return _LANG_NAMES.get(language, "English")

def _lang_instruction(language: str) -> str:
    return f"Language for all text values: {_lang_name(language)} ({language})."

def _system_prompt(language: str) -> str:
    """system prompt 里点名具体语言（比泛泛的 'the language specified' 约束力强）。"""
    return _SYSTEM_TEMPLATE.format(lang=f"{_lang_name(language)} ({language})")

def _lang_closing(language: str) -> str:
    """prompt 末尾重申输出语言：prompt 主体（数据/规则/schema 示例）全是英文，
    仅靠开头一行指令模型偶尔会整篇漂成英文，利用近因效应在末尾再压一次。"""
    return (
        f"\n\nIMPORTANT: Write ALL text values in {_lang_name(language)} ({language}), "
        "including titles, content and evidence."
    )

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

def _language_matches(result: dict, language: str) -> bool:
    """输出语言校验：CJK 语言请求的结果必须含 CJK 字符。

    K3 在英文主导的 prompt 下会随机整篇漂成英文（prompt 加固只能降低概率），
    服务端校验 + 重试兜底；非 CJK 语言不校验（拉丁/英文之间漂移不判）。
    """
    if not language.startswith(("zh", "ja", "ko")):
        return True
    return bool(_CJK_RE.search(json.dumps(result, ensure_ascii=False)))


def _prompt_overview(ctx: dict) -> str:
    return f"""{_lang_instruction(ctx.get('language','en'))}

Sleep data summary:
- Date: {ctx.get('date')}
- 7-day average sleep quality: {ctx.get('avg_sleep_quality','—')} / 100
- Last night's quality: {ctx.get('latest_score','—')} / 100
- Most-used scene (7 days): {ctx.get('scene_name','—')} × {ctx.get('used_times','—')} times
- Weekly-best audio (raw name): {ctx.get('weekly_best_audio_name') or ctx.get('scene_name','—')}

Return JSON with exactly these keys:
{{
  "sleep_insight": {{
    "title": "<8 words or fewer>",
    "description": "<1–2 sentences>"
  }},
  "weekly_best": {{
    "audio_name": "<the given weekly-best audio name, localized into the target language; keep it a short display name>"
  }}
}}"""


def _prompt_sleep_day(ctx: dict) -> str:
    return f"""{_lang_instruction(ctx.get('language','en'))}

Sleep data for {ctx.get('date')}:
- Quality score: {ctx.get('latest_score','—')} / 100
- First sleep time: {ctx.get('latest_first_sleep_time','—')}
- Pre-sleep HR: {ctx.get('latest_hr_before_sleep','—')} bpm  RR: {ctx.get('latest_rr_before_sleep','—')} brpm
- Deep: {ctx.get('latest_deep_min','—')} min ({ctx.get('latest_deep_pct','—')}%)  REM: {ctx.get('latest_rem_min','—')} min ({ctx.get('latest_rem_pct','—')}%)
- Core: {ctx.get('latest_core_min','—')} min  Night wakings: {ctx.get('latest_awake_count','—')} × {ctx.get('latest_awake_min','—')} min
- Scene used: {ctx.get('recent_scene_title','—')}

Return JSON with exactly these keys:
{{
  "sleep_scenarios": {{
    "title": "<scene card title, ≤6 words>",
    "description": "<1 sentence about how the scene influenced sleep>"
  }},
  "stage_insights": {{
    "awake": {{"description": "<1 sentence about awakening pattern>"}},
    "rem":   {{"description": "<1 sentence about REM quality>"}},
    "core":  {{"description": "<1 sentence about core sleep stability>"}},
    "deep":  {{"description": "<1 sentence about deep sleep contribution>"}}
  }}
}}"""


def _prompt_sleep_week(ctx: dict) -> str:
    score = ctx.get('avg_sleep_quality')
    label = "Excellent" if score and score >= 80 else "Good" if score and score >= 60 else "Fair"
    score = score if score is not None else '—'
    return f"""{_lang_instruction(ctx.get('language','en'))}

Weekly sleep summary ({ctx.get('start_date')} – {ctx.get('end_date')}):
- Average quality score: {score} / 100  (baseline label: {label})
- Most-used scene: {ctx.get('scene_name','—')} × {ctx.get('used_times','—')} times
- Best onset-efficiency scene this week (raw name): {ctx.get('onset_scenario_name') or ctx.get('scene_name','—')}
- Typical first-sleep time: {ctx.get('typical_first_sleep_time','—')}
- Deep sleep proportion: {ctx.get('avg_deep_pct','—')}%  REM: {ctx.get('avg_rem_pct','—')}%

Return JSON with exactly these keys:
{{
  "score_summary": {{
    "label": "<one word: Excellent | Good | Fair | Poor>"
  }},
  "sleep_trends": {{
    "body":        "<headline ≤8 words>",
    "description": "<1–2 sentences summarising the week's pattern>"
  }},
  "onset_efficiency": {{
    "scenario_name": "<the given best onset-efficiency scene name, localized into the target language; keep it a short display name>"
  }}
}}"""


def _prompt_sleep_month(ctx: dict) -> str:
    score = ctx.get('avg_sleep_quality')
    scene_name = ctx.get('scene_name') or '—'
    scene_list = ctx.get('onset_scenario_list') or '—'
    score = score if score is not None else '—'
    return f"""{_lang_instruction(ctx.get('language','en'))}

Monthly sleep summary ({ctx.get('start_date')} – {ctx.get('end_date')}):
- Average quality score: {score} / 100
- Top sleep scene: {scene_name}
- Top scenes this month (raw names, keep this order): {scene_list}
- Average deep sleep: {ctx.get('avg_deep_pct','—')}%  REM: {ctx.get('avg_rem_pct','—')}%

Return JSON with exactly these keys:
{{
  "score_summary": {{
    "label": "<one word: Excellent | Good | Fair | Poor>"
  }},
  "sleep_trends": {{
    "body":        "<headline ≤8 words>",
    "description": "<1–2 sentences about the month's sleep trend>"
  }},
  "onset_efficiency": {{
    "scenario_list": ["<the given top scene names, each localized into the target language, same order, same count>"],
    "description": "<1 sentence about the best-performing scene(s)>"
  }}
}}"""


def _prompt_explore(ctx: dict, modules: list) -> str:
    all_modules = {
        "header_summary", "onset_efficiency", "sleep_structure",
        "night_fluctuation", "scene_preference", "sleep_advice",
    }
    wanted = set(modules) & all_modules if modules else all_modules

    schema: dict = {}
    if "header_summary" in wanted:
        schema["header_summary"] = {
            "intro_text":        "<1 sentence: what kind of night was it>",
            "intro_detail_text": "<1 sentence: what Mindora helped with>",
        }
    if "onset_efficiency" in wanted:
        schema["onset_efficiency"] = {
            "label":       "<status phrase: e.g. Healthy Range | Slightly Delayed | Excellent>",
            "description": "<1 sentence about onset speed and pre-sleep physiology>",
        }
    if "sleep_structure" in wanted:
        schema["sleep_structure"] = {
            "label":       "<status phrase: e.g. Excellent | Average | Below Average>",
            "description": "<1 sentence about stage composition and recovery>",
        }
    if "night_fluctuation" in wanted:
        schema["night_fluctuation"] = {
            "label":       "<status phrase: e.g. Normal | Moderate | High Fluctuation>",
            "awake_type":  "<short localized label for the awakening pattern, based on the given awake count/minutes; e.g. Brief awakening | Frequent awakenings | Prolonged awakening>",
            "description": "<1 sentence about disturbances and system response>",
        }
    if "scene_preference" in wanted:
        schema["scene_preference"] = {
            "scene_name":  "<the preferred scene display name, localized into the target language>",
            "scene_type":  "<scene type description, e.g. Ocean / Forest / Rain / Wind, localized>",
            "description": "<1 sentence why this scene matched the sleep rhythm>",
        }
    if "sleep_advice" in wanted:
        schema["sleep_advice"] = {
            "description": "<1 actionable sentence of personalised advice>",
        }

    # 当夜心率范围：update_profile 时按睡眠窗口持久化的 hr_min/hr_max
    hr_lo = ctx.get('latest_hr_min')
    hr_hi = ctx.get('latest_hr_max')
    hr_lo = round(hr_lo) if isinstance(hr_lo, (int, float)) else '—'
    hr_hi = round(hr_hi) if isinstance(hr_hi, (int, float)) else '—'

    return f"""{_lang_instruction(ctx.get('language','en'))}

Last-night sleep analysis ({ctx.get('date')}):
- Overall score: {ctx.get('latest_score','—')} / 100
- Onset efficiency (SOE): {ctx.get('latest_soe','—')} / 100,  fell asleep in ~{ctx.get('latest_onset_min','?')} min at {ctx.get('latest_first_sleep_time','—')}
- Pre-sleep HR: {ctx.get('latest_hr_before_sleep','—')} bpm   RR: {ctx.get('latest_rr_before_sleep','—')} brpm
- Deep: {ctx.get('latest_deep_pct','—')}%   REM: {ctx.get('latest_rem_pct','—')}%   Core: {ctx.get('latest_core_pct','—')}%
- Night wakings: {ctx.get('latest_awake_count','—')} × {ctx.get('latest_awake_min','—')} min   type: {ctx.get('latest_awake_type','—')}
- HR range: {hr_lo}–{hr_hi} bpm   HRV: {ctx.get('latest_hrv','—')}
- Preferred scene (7 days): {ctx.get('scene_name','—')}

Return JSON with exactly these keys:
{json.dumps(schema, indent=2, ensure_ascii=False)}"""


def _prompt_conclusion_polish(ctx: dict) -> str:
    """规则结论润色 prompt（对照规范 v3 §4：LLM 仅可在既定事实上润色）。

    输入是规则引擎算好的结构化结论（含事实、状态、已渲染文案），要求模型
    只重写文本字段：不得增删数字/事实/场景/结论，不得使用禁用词，保持语言。
    """
    import json as _json
    conclusions_json = _json.dumps(ctx.get("conclusions") or [], ensure_ascii=False, indent=1)
    forbidden = "、".join(_get_insight_rules()["forbidden_terms"])
    return f"""{_lang_instruction(ctx.get('language', 'en'))}

You are polishing pre-computed sleep insight conclusions for the Mindora app.
The conclusions below were computed by deterministic rules from the user's own data.
Your ONLY job is to rewrite the "title" and "text" of each conclusion to read more
naturally and warmly — the facts, numbers, states and judgments are already final.

Conclusions (JSON):
{conclusions_json}

Hard constraints (violating any → output is rejected):
- Do NOT add, remove or change any number, time, scene name, or factual claim.
- Do NOT add new conclusions, diagnoses, causes, or effectiveness claims.
- Do NOT use any of these forbidden terms: {forbidden}
- Keep "title" ≤ 8 words and "text" ≤ 4 sentences.
- Return ONLY a JSON object mapping each conclusion "key" to {{"title": ..., "text": ...}}.
- Keys must be exactly the same set as the input conclusions."""


def polish_output_ok(result: dict, conclusions: list, language: str) -> bool:
    """LLM 润色输出校验（校验失败 → 保留规则模板文案）：
    1) key 集合匹配；2) 语言校验；3) 禁用词；4) 输出数字必须能在输入中找到（防幻觉）；
    5) 单字段长度上限。"""
    if not isinstance(result, dict):
        return False
    keys = {getattr(c, "key", None) for c in conclusions}
    if set(result.keys()) != {k for k in keys if k}:
        return False
    if not _language_matches(result, language):
        return False
    import json as _json
    text_all = _json.dumps(result, ensure_ascii=False).lower()
    for term in _get_insight_rules()["forbidden_terms"]:
        if term.lower() in text_all:
            return False
    input_nums: set = set()
    for c in conclusions:
        input_nums |= set(re.findall(r"\d+", _json.dumps(getattr(c, "to_llm_dict", lambda: {})(), ensure_ascii=False)))
        input_nums |= set(re.findall(r"\d+", str(getattr(c, "title", "")) + str(getattr(c, "text", ""))))
    for v in result.values():
        if not isinstance(v, dict):
            return False
        for field in ("title", "text"):
            s = v.get(field)
            if s is None:
                continue
            if len(s) > _get_insight_rules()["polish_max_chars"] * 3:
                return False
            for num in re.findall(r"\d+", s):
                if num not in input_nums:
                    return False
    return True


def _prompt_sleep_insight(ctx: dict) -> str:
    """Prompt for the 6-module insight page report (mindora_advice.md modules 0-5).

    One LLM call produces all six modules at once so the stored
    ``SleepInsightReport`` is internally consistent.  Each module returns
    title / content / evidence / action; visibility rules (e.g. module 3
    only shows when brief awakenings exist) are enforced server-side.
    """
    def _fmt(value, suffix=""):
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:g}{suffix}"
        return f"{value}{suffix}"

    return f"""{_lang_instruction(ctx.get('language', 'en'))}

You are writing the Mindora insight page. Every sentence must answer:
"Based on which data, under what conditions, did Mindora reach this possible understanding."

Recent 7-day sleep statistics (aggregated locally):
- Records used: {ctx.get('record_count', '—')}
- Average sleep quality: {_fmt(ctx.get('avg_sleep_quality'))} / 100
- Average sleep-onset latency: {_fmt(ctx.get('avg_onset_min'), ' min')}
- Typical first sleep time: {ctx.get('typical_first_sleep_time', '—')}
- Typical bed time: {ctx.get('typical_bed_time', '—')}   wake time: {ctx.get('typical_wake_time', '—')}
- Average time in bed: {_fmt(ctx.get('avg_time_in_bed_min'), ' min')}
- Deep sleep: {_fmt(ctx.get('avg_deep_min'), ' min')} ({_fmt(ctx.get('avg_deep_pct'), '%')})
- REM sleep: {_fmt(ctx.get('avg_rem_min'), ' min')} ({_fmt(ctx.get('avg_rem_pct'), '%')})
- Core sleep: {_fmt(ctx.get('avg_core_min'), ' min')} ({_fmt(ctx.get('avg_core_pct'), '%')})
- Night awakenings: {_fmt(ctx.get('avg_awake_count'))} × {_fmt(ctx.get('avg_awake_min'), ' min')}
- Pre-sleep HR: {_fmt(ctx.get('avg_hr_before_sleep'), ' bpm')}   RR: {_fmt(ctx.get('avg_rr_before_sleep'), ' brpm')}
- Average HR: {_fmt(ctx.get('avg_heart_rate'), ' bpm')}   HRV: {_fmt(ctx.get('avg_hrv'))}
- Most recently used scene: {ctx.get('recent_scene_title', '—')}
- Weekly top scene: {ctx.get('weekly_top_scene_title', '—')} × {ctx.get('weekly_top_scene_count', 0)} times

Writing rules (must follow):
- Only explain "what happened + possible why + what Mindora did".
- Compare the user only against their OWN baselines (last night vs 7-day average); never compare with other people.
- Use hedged wording: "may / usually / tends to / often"; never causal claims, never diagnoses.
- Forbidden words/claims: stress, anxiety, insomnia, treatment, guaranteed improvement.
- Keep every field to 1–2 sentences, warm and encouraging in tone.

Return ONLY a JSON object (no markdown, no explanation) with exactly these six modules:
{{
  "greeting": {{
    "title": "<module 0: short greeting headline, ≤8 words>",
    "content": "<1 sentence guiding the user into the insights>",
    "evidence": ["<which data this page is based on>"],
    "action": ""
  }},
  "onset": {{
    "title": "<module 1: sleep onset insight headline>",
    "content": "<1–2 sentences: last night onset vs own 7-day baseline, in minutes>",
    "evidence": ["<e.g. 7-day avg onset vs last night>"],
    "action": "<1 actionable sentence, e.g. keep using the top scene>"
  }},
  "architecture": {{
    "title": "<module 2: sleep structure headline>",
    "content": "<1–2 sentences: whether deep/REM/core proportions are stable vs own baseline>",
    "evidence": ["<stage durations / percentages used>"],
    "action": "<1 sentence to help maintain rhythm; empty if structure stable>"
  }},
  "intervention": {{
    "title": "<module 3: night fluctuation & Mindora response headline>",
    "content": "<1–2 sentences normalising brief awakenings and describing Mindora's companionship>",
    "evidence": ["<awakening count / duration used>"],
    "action": ""
  }},
  "scene_preference": {{
    "title": "<module 4: scene preference headline>",
    "content": "<1–2 sentences: most-used scene and the onset pattern when it is used>",
    "evidence": ["<scene usage counts used>"],
    "action": "<1 sentence recommending a similar soundscape or continued use>"
  }},
  "micro_education": {{
    "title": "<module 5: one micro sleep-knowledge headline>",
    "content": "<1–2 sentences of light sleep knowledge relevant to the data above>",
    "evidence": [],
    "action": ""
  }}
}}"""


# ──────────────────────────────────────────────────────────────
# SleepAnalysisLLM
# ──────────────────────────────────────────────────────────────

class SleepAnalysisLLM:
    """
    Async wrapper for sleep analysis text generation.

    请求方向由 ModelRouter 决定（llm/router.py）：一组 key(env) + url(config) 算一个方向，
    按 Config.LLM_ROUTING 规则把 request_type 路由到 volc_ark / kimi 等方向。
    Falls back gracefully (returns None) when no route is available (no API key) or
    on any LLM/network error.
    """

    def __init__(self, router: Optional["ModelRouter"] = None):
        self._router = router or ModelRouter.from_env()
        self._model = self._router.chat_model_for("default")
        # request_type → 最近成功方向名：下次优先用它，避免每次都先撞失败方向
        self._last_good: dict[str, str] = {}
        if self._model is None:
            logging.warning(
                "no available LLM route (set ARK_API_KEY or KIMI_API_KEY) — LLM analysis disabled, using default text"
            )

    def _model_for(self, request_type: str) -> Optional[VolcEngineArkChat]:
        """按 request_type 路由到对应方向的 chat model；兼容 __new__ 构造（无 _router）的旧测试。"""
        router = getattr(self, "_router", None)
        if router is None:
            return self._model
        return router.chat_model_for(request_type) or self._model

    def _candidate_models(self, request_type: str) -> list[tuple[str, "VolcEngineArkChat"]]:
        """按优先级返回 [(方向名, model)]：最近成功方向优先，其余按路由表注册顺序。

        调用级故障转移的候选序列：首选方向调用失败/超时/非 JSON 时按序切换。
        """
        router = getattr(self, "_router", None)
        if router is None:
            return [("default", self._model)] if self._model is not None else []
        routes = router.available_routes(request_type)
        last = getattr(self, "_last_good", {}).get(request_type)
        if last:
            routes = sorted(routes, key=lambda r: 0 if r.name == last else 1)
        out = []
        for route in routes:
            model = router.chat_model_for_route(route)
            if model is not None:
                out.append((route.name, model))
        return out

    @property
    def enabled(self) -> bool:
        if hasattr(self, "_enabled_override"):
            return self._enabled_override
        return self._model is not None

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled_override = value

    # ── internal ──────────────────────────────────────────────

    async def _call(self, user_prompt: str, model, route_name: str, language: str = "en") -> Optional[str]:
        """Run a blocking LLM call in a thread pool（指定方向；失败由调用方切换方向）。"""
        if not self.enabled or model is None:
            return None

        def _invoke() -> str:
            resp = model.invoke([
                SystemMessage(content=_system_prompt(language)),
                HumanMessage(content=user_prompt),
            ])
            return resp.content

        try:
            loop = asyncio.get_running_loop()
            # Slightly longer than the HTTP timeout inside VolcEngineArkChat
            # so thread-pool scheduling overhead does not trigger this first.
            return await asyncio.wait_for(
                loop.run_in_executor(None, _invoke),
                timeout=130,
            )
        except asyncio.TimeoutError:
            logging.warning("LLM call timed out (route=%s)", route_name)
        except Exception as e:
            logging.error("LLM call error (route=%s): %s", route_name, e)
        return None

    def _parse(self, text: Optional[str]) -> Optional[dict]:
        if not text:
            return None
        # strip markdown fences if present
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if m:
            text = m.group(1)
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            logging.warning("LLM returned non-JSON: %.120s", text)
            return None

    # ── public API ────────────────────────────────────────────

    async def generate(
        self,
        request_type: str,
        ctx: dict,
        language: str,
        modules: list,
    ) -> Optional[dict]:
        """
        Dispatch to the appropriate prompt builder and return a dict of
        text-only fields, or None if LLM is disabled / fails.
        """
        ctx = {**ctx, "language": language}

        prompt_fn = {
            "analysis_overview":       lambda: _prompt_overview(ctx),
            "analysis_sleep_day":      lambda: _prompt_sleep_day(ctx),
            "analysis_sleep_week":     lambda: _prompt_sleep_week(ctx),
            "analysis_sleep_month":    lambda: _prompt_sleep_month(ctx),
            "analysis_explore":        lambda: _prompt_explore(ctx, modules),
            "sleep_insight_report":    lambda: _prompt_sleep_insight(ctx),
            "sleep_insight_polish":    lambda: _prompt_conclusion_polish(ctx),
            "insight_polish":          lambda: _prompt_conclusion_polish(ctx),
        }.get(request_type)

        if prompt_fn is None:
            return None

        prompt = prompt_fn() + _lang_closing(language)
        # 调用级故障转移：首选方向失败（异常/超时/非 JSON/语言校验不过）→ 切换下一个方向
        for route_name, model in self._candidate_models(request_type):
            for attempt in (1, 2):
                raw = await self._call(prompt, model, route_name, language)
                result = self._parse(raw)
                if result is None:
                    break  # 调用失败/非 JSON：换下一个方向
                if _language_matches(result, language):
                    self._last_good[request_type] = route_name
                    return result
                logging.warning("LLM output not in %s (route=%s, request_type=%s), retry %d/2", language, route_name, request_type, attempt)
            logging.warning("llm route %s failed for request_type=%s, trying next route", route_name, request_type)
        logging.error("all llm routes failed (request_type=%s), using fallback", request_type)
        return None

    def generate_sync(
        self,
        request_type: str,
        ctx: dict,
        language: str,
        modules: list,
    ) -> Optional[dict]:
        """
        Synchronous version of generate().

        Dispatches to the same prompt builders and parses the response.
        Used from synchronous contexts such as UserProfileServ.update_profile().
        """
        if not self.enabled:
            return None

        ctx = {**ctx, "language": language}

        prompt_fn = {
            "analysis_overview":       lambda: _prompt_overview(ctx),
            "analysis_sleep_day":      lambda: _prompt_sleep_day(ctx),
            "analysis_sleep_week":     lambda: _prompt_sleep_week(ctx),
            "analysis_sleep_month":    lambda: _prompt_sleep_month(ctx),
            "analysis_explore":        lambda: _prompt_explore(ctx, modules),
            "sleep_insight_report":    lambda: _prompt_sleep_insight(ctx),
            "sleep_insight_polish":    lambda: _prompt_conclusion_polish(ctx),
            "insight_polish":          lambda: _prompt_conclusion_polish(ctx),
        }.get(request_type)

        if prompt_fn is None:
            return None

        prompt = prompt_fn() + _lang_closing(language)
        # 调用级故障转移：首选方向失败（异常/超时/非 JSON/语言校验不过）→ 切换下一个方向
        for route_name, model in self._candidate_models(request_type):
            for attempt in (1, 2):
                try:
                    resp = model.invoke([
                        SystemMessage(content=_system_prompt(language)),
                        HumanMessage(content=prompt),
                    ])
                    result = self._parse(resp.content)
                except Exception as e:
                    logging.error("LLM sync call error (route=%s): %s", route_name, e)
                    break  # 换下一个方向
                if result is None:
                    logging.warning("LLM returned non-JSON (route=%s, request_type=%s)", route_name, request_type)
                    break  # 换下一个方向
                if _language_matches(result, language):
                    self._last_good[request_type] = route_name
                    return result
                logging.warning("LLM output not in %s (route=%s, request_type=%s), retry %d/2", language, route_name, request_type, attempt)
            logging.warning("llm route %s failed for request_type=%s, trying next route", route_name, request_type)
        logging.error("all llm routes failed (request_type=%s), using fallback", request_type)
        return None
