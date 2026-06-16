"""
llm_service.py — LLM-powered sleep analysis text generation.

Wraps VolcEngineArkChat (doubao_langchain) and generates the TEXT fields
for each /analysis response type.  Numeric fields (scores, durations, counts)
are always computed from real sensor data by user_server._build_* methods;
LLM only fills in human-readable titles, descriptions, labels, and advice.

Usage (from user_server.py):
    from llm_service import SleepAnalysisLLM, extract_sleep_context
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

from tool.doubao_langchain import VolcEngineArkChat


_PROFILE_JSON_MAX_CHARS = 12000
_KNOWLEDGE_BASE_PATH = os.path.join(
    os.path.dirname(__file__),
    "db",
    "knowledge_base.md",
)


# ──────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────

def extract_sleep_context(profile, data) -> dict:
    """
    Pull key sleep metrics from UserProfile into a flat dict
    that can be embedded in an LLM prompt.
    """
    ctx: dict[str, Any] = {
        "date":       getattr(data, "date", None) or "",
        "start_date": getattr(data, "start_date", None) or "",
        "end_date":   getattr(data, "end_date", None) or "",
        "language":   getattr(data, "language", "en"),
    }

    if not profile or not profile.sleep_data:
        return ctx

    recent = profile.sleep_data[-7:]
    scores = [s.sleep_quality for s in recent if s.sleep_quality is not None]
    ctx["avg_score"]    = round(sum(scores) / len(scores)) if scores else None
    ctx["latest_score"] = int(recent[-1].sleep_quality) if recent[-1].sleep_quality else None

    latest = recent[-1]
    ctx["first_sleep_time"]  = latest.first_sleep_time
    ctx["hr_before_sleep"]   = latest.hr_before_sleep
    ctx["rr_before_sleep"]   = latest.rr_before_sleep
    ctx["avg_heart_rate"]    = latest.avg_heart_rate
    ctx["hrv"]               = latest.hrv
    ctx["onset_score"]       = int(latest.soe) if latest.soe else None

    summ = latest.sequence_summaries if latest.sleep_status else {}
    tb   = summ.get("time_in_bed") or 1
    ctx["deep_min"]    = round(summ.get("deep_sleep_duration", 0))
    ctx["rem_min"]     = round(summ.get("rem_sleep_duration",  0))
    ctx["core_min"]    = round(summ.get("core_sleep_duration", 0))
    ctx["awake_count"] = summ.get("night_awake_count", 0)
    ctx["awake_min"]   = round(summ.get("night_awake_duration", 0))
    ctx["awake_type"]  = summ.get("night_awake_type") or "brief awakening"
    ctx["deep_pct"]    = round(summ.get("deep_sleep_duration", 0) / tb * 100, 1)
    ctx["rem_pct"]     = round(summ.get("rem_sleep_duration",  0) / tb * 100, 1)
    ctx["core_pct"]    = round(summ.get("core_sleep_duration", 0) / tb * 100, 1)

    # best scene from mindora_record
    if profile.mindora_record:
        best = max(profile.mindora_record.items(), key=lambda x: len(x[1]), default=None)
        if best and best[1]:
            ctx["scene_id"]    = best[0].replace("sleep.scene.", "")
            ctx["scene_name"]  = ctx["scene_id"].replace("_", " ").title()
            ctx["used_times"]  = len(best[1])

    ctx["user_profile_json"] = _serialize_profile_for_prompt(profile)
    ctx["sleep_knowledge"] = _load_sleep_knowledge()

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
    profile_dict = profile.model_dump(mode="json", exclude_none=True)

    if isinstance(profile_dict.get("profile"), dict):
        # Skip bulky image payloads while keeping the field name visible.
        if profile_dict["profile"].get("avatar_base64"):
            profile_dict["profile"]["avatar_base64"] = "[omitted base64 image data]"

    profile_dict["behaviors"] = _summarize_behavior_series(profile_dict.get("behaviors", {}))

    text = json.dumps(profile_dict, ensure_ascii=False, indent=2)
    return _truncate_text(text, _PROFILE_JSON_MAX_CHARS)


@lru_cache(maxsize=1)
def _load_sleep_knowledge() -> str:
    try:
        with open(_KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
            return _truncate_text(text, 5000)
    except Exception as e:
        logging.warning("failed to load sleep knowledge base: %s", e)
        return ""


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


def _prompt_sleep_advice(ctx: dict) -> str:
    """Prompt for the /sleep_advice endpoint.

    Asks the LLM for a structured JSON with analysis paragraph,
    actionable advice bullets, and optional per-pillar highlights.
    """
    focus = ctx.get("focus") or []
    focus_hint = ""
    if focus:
        focus_hint = f"\nFocus especially on: {', '.join(focus)}."

    hr = ctx.get('avg_heart_rate', '—')
    hr_lo = round(hr - 15) if isinstance(hr, (int, float)) else '—'
    hr_hi = round(hr + 15) if isinstance(hr, (int, float)) else '—'
    profile_json = ctx.get("user_profile_json", "{}")
    knowledge = ctx.get("sleep_knowledge", "")
    knowledge_block = f"\nMindora sleep recommendation knowledge base:\n{knowledge}\n" if knowledge else ""

    return f"""{_lang_instruction(ctx.get('language', 'en'))}

Sleep data for {ctx.get('date', 'last night')}:
- Overall score: {ctx.get('latest_score', '—')} / 100
- Onset efficiency (SOE): {ctx.get('onset_score', '—')} / 100
- First sleep time: {ctx.get('first_sleep_time', '—')}
- Pre-sleep HR: {ctx.get('hr_before_sleep', '—')} bpm   RR: {ctx.get('rr_before_sleep', '—')} brpm
- Deep: {ctx.get('deep_pct', '—')}%   REM: {ctx.get('rem_pct', '—')}%   Core: {ctx.get('core_pct', '—')}%
- Night wakings: {ctx.get('awake_count', '—')} × {ctx.get('awake_min', '—')} min   type: {ctx.get('awake_type', '—')}
- HR range: {hr_lo}–{hr_hi} bpm   HRV: {ctx.get('hrv', '—')}
- Preferred scene (7 days): {ctx.get('scene_name', '—')} (used {ctx.get('used_times', 0)} times)
{focus_hint}
{knowledge_block}
Full user profile JSON snapshot:
```json
{profile_json}
```

Your task:
1. Use both the structured sleep metrics and the user profile JSON to infer the user's likely sleep issues, context, and preferences.
2. Ground your recommendations in the Mindora sleep recommendation knowledge base when it is relevant.
3. Write a brief sleep analysis (2–4 sentences), warm, concrete, and personalized.
4. Provide 2–4 personalised, actionable advice bullets based on the data.
5. For each relevant pillar, give a one-line highlight.
6. Do not mention missing fields, raw JSON, or that you used a knowledge base.

Return ONLY a JSON object (no markdown, no explanation):
{{
  "analysis": "<2–4 sentence analysis paragraph>",
  "advice": [
    "<actionable bullet 1>",
    "<actionable bullet 2>"
  ],
  "highlights": {{
    "onset": "<one-liner about onset quality>",
    "deep": "<one-liner about deep sleep>",
    "rem": "<one-liner about REM sleep>",
    "rhythm": "<one-liner about sleep regularity / awakenings>"
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


# ──────────────────────────────────────────────────────────────
# SleepAnalysisLLM
# ──────────────────────────────────────────────────────────────

class SleepAnalysisLLM:
    """
    Async wrapper around VolcEngineArkChat for sleep analysis text generation.
    Falls back gracefully (returns None) when ARK_API_KEY is not set or
    on any LLM/network error.
    """

    def __init__(self):
        self._model: Optional[VolcEngineArkChat] = None
        self._init_model()

    def _init_model(self):
        api_key     = os.getenv("ARK_API_KEY")
        endpoint_id = os.getenv("ARK_ENDPOINT_ID", "ep-20260325170723-znh7n")
        model       = os.getenv("ARK_MODEL", "doubao-seed-2-0-lite-260215")
        if not api_key:
            logging.warning("ARK_API_KEY not set — LLM analysis disabled, using default text")
            return
        try:
            self._model = VolcEngineArkChat(
                ark_api_key=api_key,
                endpoint_id=endpoint_id,
                model=model,
                temperature=0.5,
            )
            logging.info("SleepAnalysisLLM ready (endpoint=%s, model=%s)", endpoint_id, model)
        except Exception as e:
            logging.error("SleepAnalysisLLM init failed: %s", e)

    @property
    def enabled(self) -> bool:
        return self._model is not None

    # ── internal ──────────────────────────────────────────────

    async def _call(self, user_prompt: str) -> Optional[str]:
        """Run a blocking LLM call in a thread pool."""
        if not self.enabled:
            return None
        model = self._model

        def _invoke() -> str:
            resp = model.invoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_prompt),
            ])
            return resp.content

        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, _invoke),
                timeout=120,
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
            "sleep_analysis_advice":   lambda: _prompt_sleep_advice(ctx),
        }.get(request_type)

        if prompt_fn is None:
            return None

        raw = await self._call(prompt_fn())
        return self._parse(raw)
