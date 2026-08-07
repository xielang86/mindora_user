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


from user_profile import compute_recent_sleep_stats


_PROFILE_JSON_MAX_CHARS = 12000


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

    # Profile sleep_analysis fields drive the prompt content.
    sleep_analysis = profile.sleep_analysis or {}
    ctx["sleep_trend_week"]  = sleep_analysis.get("sleep_trend_week", "")
    ctx["sleep_trend_month"] = sleep_analysis.get("sleep_trend_month", "")
    ctx["scene_title"]       = (sleep_analysis.get("scene") or {}).get("title", "")
    ctx["scene_text"]        = (sleep_analysis.get("scene") or {}).get("text", "")


    return ctx


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 32] + "\n... [truncated for prompt size]"


def _summarize_behavior_series(behaviors: dict[str, Any]) -> dict[str, Any]:
    summarized: dict[str, Any] = {}
    for key, values in (behaviors or {}).items():
        if not isinstance(values, list):
            summarized[key] = values
            continue
        summarized[key] = {
            "count": len(values),
            "recent_samples": values[-5:],
        }
    return summarized


def _serialize_profile_for_prompt(profile) -> str:
    """Kept for non-sleep-advice prompts; still omits bulky image payloads."""
    profile_dict = profile.model_dump(mode="json", exclude_none=True)

    if isinstance(profile_dict.get("profile"), dict):
        # Skip bulky image payloads while keeping the field name visible.
        if profile_dict["profile"].get("avatar_base64"):
            profile_dict["profile"]["avatar_base64"] = "[omitted base64 image data]"

    profile_dict["behaviors"] = _summarize_behavior_series(profile_dict.get("behaviors", {}))

    text = json.dumps(profile_dict, ensure_ascii=False, indent=2)
    return _truncate_text(text, _PROFILE_JSON_MAX_CHARS)


def _summarize_profile_for_prompt(profile) -> str:
    """Return a compact, LLM-friendly snapshot of the user profile.

    Includes only stable user info and the stored sleep_analysis fields.
    Raw behaviors and full mindora_record history are intentionally omitted.
    """
    if not profile:
        return "{}"

    data: dict[str, Any] = {}
    if profile.basic_info:
        data["basic_info"] = profile.basic_info
    if profile.long_term_profile:
        data["long_term_profile"] = profile.long_term_profile
    if profile.profile:
        prof = profile.profile.model_dump(mode="json", exclude_none=True)
        prof.pop("avatar_base64", None)
        data["profile"] = prof

    sleep_analysis = profile.sleep_analysis or {}
    data["sleep_analysis"] = {
        "sleep_trend_week": sleep_analysis.get("sleep_trend_week", ""),
        "sleep_trend_month": sleep_analysis.get("sleep_trend_month", ""),
        "scene": sleep_analysis.get("scene", {}),
    }

    return json.dumps(data, ensure_ascii=False, indent=2)


def deep_merge(base: dict, updates: dict) -> None:
    """
    Recursively merge `updates` into `base`, overwriting only non-empty string
    values.  Numeric / boolean / list values in `base` are never overwritten.
    """
    for k, v in updates.items():
        if k not in base:
            base[k] = v
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        elif isinstance(v, str) and v.strip():
            base[k] = v
        # skip None, empty strings, and non-string overrides of existing data


# ──────────────────────────────────────────────────────────────
# Prompt templates  (text fields only — no numeric placeholders)
# ──────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a professional sleep health analyst for the Mindora app. "
    "Analyze the provided sleep data and return ONLY a JSON object — "
    "no markdown fences, no explanation, no trailing text. "
    "All string values must be written in the language specified. "
    "Keep every description to 1–2 sentences, warm and encouraging in tone."
)

def _lang_instruction(language: str) -> str:
    names = {
        "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
        "en": "English", "ja": "日本語", "ko": "한국어",
        "de": "Deutsch", "fr": "Français", "it": "Italiano",
        "es": "Español", "id": "Bahasa Indonesia",
    }
    lang_name = names.get(language, "English")
    return f"Language for all text values: {lang_name} ({language})."


def _prompt_overview(ctx: dict) -> str:
    return f"""{_lang_instruction(ctx.get('language','en'))}

Sleep data summary:
- Date: {ctx.get('date')}
- 7-day average sleep quality: {ctx.get('avg_score')} / 100
- Last night's quality: {ctx.get('latest_score')} / 100
- Most-used scene (7 days): {ctx.get('scene_name','—')} × {ctx.get('used_times','—')} times

Return JSON with exactly these keys:
{{
  "sleep_insight": {{
    "title": "<8 words or fewer>",
    "description": "<1–2 sentences>"
  }}
}}"""


def _prompt_sleep_day(ctx: dict) -> str:
    return f"""{_lang_instruction(ctx.get('language','en'))}

Sleep data for {ctx.get('date')}:
- Quality score: {ctx.get('latest_score')} / 100
- First sleep time: {ctx.get('first_sleep_time','—')}
- Pre-sleep HR: {ctx.get('hr_before_sleep','—')} bpm  RR: {ctx.get('rr_before_sleep','—')} brpm
- Deep: {ctx.get('deep_min')} min ({ctx.get('deep_pct')}%)  REM: {ctx.get('rem_min')} min ({ctx.get('rem_pct')}%)
- Core: {ctx.get('core_min')} min  Night wakings: {ctx.get('awake_count')} × {ctx.get('awake_min')} min
- Scene used: {ctx.get('scene_name','—')}

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
    score = ctx.get('avg_score')
    label = "Excellent" if score and score >= 80 else "Good" if score and score >= 60 else "Fair"
    return f"""{_lang_instruction(ctx.get('language','en'))}

Weekly sleep summary ({ctx.get('start_date')} – {ctx.get('end_date')}):
- Average quality score: {score} / 100  (baseline label: {label})
- Most-used scene: {ctx.get('scene_name','—')} × {ctx.get('used_times','—')} times
- Typical first-sleep time: {ctx.get('first_sleep_time','—')}
- Deep sleep proportion: {ctx.get('deep_pct')}%  REM: {ctx.get('rem_pct')}%

Return JSON with exactly these keys:
{{
  "score_summary": {{
    "label": "<one word: Excellent | Good | Fair | Poor>"
  }},
  "sleep_trends": {{
    "body":        "<headline ≤8 words>",
    "description": "<1–2 sentences summarising the week's pattern>"
  }}
}}"""


def _prompt_sleep_month(ctx: dict) -> str:
    score = ctx.get('avg_score')
    scene_name = ctx.get('scene_name', '—')
    return f"""{_lang_instruction(ctx.get('language','en'))}

Monthly sleep summary ({ctx.get('start_date')} – {ctx.get('end_date')}):
- Average quality score: {score} / 100
- Top sleep scene: {scene_name}
- Average deep sleep: {ctx.get('deep_pct')}%  REM: {ctx.get('rem_pct')}%

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
            "description": "<1 sentence about disturbances and system response>",
        }
    if "scene_preference" in wanted:
        schema["scene_preference"] = {
            "description": "<1 sentence why this scene matched the sleep rhythm>",
        }
    if "sleep_advice" in wanted:
        schema["sleep_advice"] = {
            "description": "<1 actionable sentence of personalised advice>",
        }

    hr = ctx.get('avg_heart_rate', '—')
    hr_lo = round(hr - 15) if isinstance(hr, (int, float)) else '—'
    hr_hi = round(hr + 15) if isinstance(hr, (int, float)) else '—'

    return f"""{_lang_instruction(ctx.get('language','en'))}

Last-night sleep analysis ({ctx.get('date')}):
- Overall score: {ctx.get('latest_score')} / 100
- Onset efficiency (SOE): {ctx.get('onset_score')} / 100,  fell asleep in ~? min at {ctx.get('first_sleep_time','—')}
- Pre-sleep HR: {ctx.get('hr_before_sleep','—')} bpm   RR: {ctx.get('rr_before_sleep','—')} brpm
- Deep: {ctx.get('deep_pct')}%   REM: {ctx.get('rem_pct')}%   Core: {ctx.get('core_pct')}%
- Night wakings: {ctx.get('awake_count')} × {ctx.get('awake_min')} min   type: {ctx.get('awake_type','—')}
- HR range: {hr_lo}–{hr_hi} bpm   HRV: {ctx.get('hrv','—')}
- Preferred scene (7 days): {ctx.get('scene_name','—')}

Return JSON with exactly these keys:
{json.dumps(schema, indent=2, ensure_ascii=False)}"""


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

    @property
    def enabled(self) -> bool:
        if hasattr(self, "_enabled_override"):
            return self._enabled_override
        return self._model is not None

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled_override = value

    # ── internal ──────────────────────────────────────────────

    async def _call(self, user_prompt: str, request_type: str = "default") -> Optional[str]:
        """Run a blocking LLM call in a thread pool."""
        if not self.enabled:
            return None
        model = self._model_for(request_type)
        if model is None:
            return None

        def _invoke() -> str:
            resp = model.invoke([
                SystemMessage(content=_SYSTEM),
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
            logging.warning("LLM call timed out")
        except Exception as e:
            logging.error("LLM call error: %s", e)
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
        }.get(request_type)

        if prompt_fn is None:
            return None

        raw = await self._call(prompt_fn(), request_type)
        return self._parse(raw)

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
        }.get(request_type)

        if prompt_fn is None:
            return None

        model = self._model_for(request_type)
        if model is None:
            return None

        try:
            resp = model.invoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=prompt_fn()),
            ])
            return self._parse(resp.content)
        except Exception as e:
            logging.error("LLM sync call error: %s", e)
            return None
