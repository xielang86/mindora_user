import json
import logging
import os
import re
import time
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from llm.ark_chat import VolcEngineArkChat
from llm.router import ModelRouter
from user_profile import UserProfile, SleepScenario, short_scene_id


# 仓库根目录（本文件在 llm/ 包内，db/ 和 data/ 在仓库根）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_KNOWLEDGE_BASE_PATH = os.path.join(
    _REPO_ROOT,
    "db",
    "knowledge_base.md",
)

_TOPOLOGY_PATH = os.path.join(
    _REPO_ROOT,
    "db",
    "topology.md",
)

_SOP_CANDIDATES_PATH = os.path.join(
    _REPO_ROOT,
    "data",
    "reco_candidates.json",
)

_LLM_TRACE_LOG_PATH = Path(_REPO_ROOT) / "llm_request_response.log"

_SCENARIO_CANDIDATES: list[dict[str, Any]] = [
    {
        "scenario_id": "cocos_island_moonlight_v1",
        "scenario_name": "Cocos Island Moonlight",
        "stages": [
            {"cmd_name": "cocos_island_moonlight", "stage_name": "Relax", "audio_file": "moon_wave_relax.mp3", "guide_file": "relax_breathing_soft.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "cocos_island_moonlight", "stage_name": "Induce", "audio_file": "moon_wave_induce.mp3", "guide_file": "body_scan_slow.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "cocos_island_moonlight", "stage_name": "Deep", "audio_file": "deep_ocean_brown_noise.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "cocos_island_moonlight", "stage_name": "Waken", "audio_file": "gentle_dawn_tide.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
    {
        "scenario_id": "amalfi_breeze_v1",
        "scenario_name": "Amalfi Breeze",
        "stages": [
            {"cmd_name": "amalfi_breeze", "stage_name": "Relax", "audio_file": "coastal_breeze_relax.mp3", "guide_file": "relax_breathing_soft.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "amalfi_breeze", "stage_name": "Induce", "audio_file": "coastal_breeze_induce.mp3", "guide_file": "countdown_sleep.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "amalfi_breeze", "stage_name": "Deep", "audio_file": "sea_brown_noise_90m.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "amalfi_breeze", "stage_name": "Waken", "audio_file": "seaside_sunrise.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
    {
        "scenario_id": "kyoto_forest_v1",
        "scenario_name": "Kyoto Forest",
        "stages": [
            {"cmd_name": "kyoto_forest", "stage_name": "Relax", "audio_file": "forest_breath_relax.mp3", "guide_file": "shoulder_release.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_woodland"},
            {"cmd_name": "kyoto_forest", "stage_name": "Induce", "audio_file": "forest_rain_induce.mp3", "guide_file": "body_scan_slow.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_woodland"},
            {"cmd_name": "kyoto_forest", "stage_name": "Deep", "audio_file": "forest_brown_noise.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "kyoto_forest", "stage_name": "Waken", "audio_file": "forest_dawn_birds.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
    {
        "scenario_id": "andaman_rainforest_sanctuary_v1",
        "scenario_name": "Andaman Rainforest Sanctuary",
        "stages": [
            {"cmd_name": "andaman_rainforest_sanctuary", "stage_name": "Relax", "audio_file": "rainforest_relax.mp3", "guide_file": "relax_breathing_soft.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "andaman_rainforest_sanctuary", "stage_name": "Induce", "audio_file": "rain_canopy_induce.mp3", "guide_file": "countdown_sleep.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "andaman_rainforest_sanctuary", "stage_name": "Deep", "audio_file": "rainforest_brown_noise.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "andaman_rainforest_sanctuary", "stage_name": "Waken", "audio_file": "rainforest_dawn.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
    {
        "scenario_id": "bhutan_misty_forest_v1",
        "scenario_name": "Bhutan Misty Forest",
        "stages": [
            {"cmd_name": "bhutan_misty_forest", "stage_name": "Relax", "audio_file": "mist_forest_relax.mp3", "guide_file": "shoulder_release.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_woodland"},
            {"cmd_name": "bhutan_misty_forest", "stage_name": "Induce", "audio_file": "mist_forest_induce.mp3", "guide_file": "body_scan_slow.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_woodland"},
            {"cmd_name": "bhutan_misty_forest", "stage_name": "Deep", "audio_file": "mist_brown_noise.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "bhutan_misty_forest", "stage_name": "Waken", "audio_file": "misty_dawn_bells.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
    {
        "scenario_id": "sedona_red_rock_peace_v1",
        "scenario_name": "Sedona Red Rock Peace",
        "stages": [
            {"cmd_name": "sedona_red_rock_peace", "stage_name": "Relax", "audio_file": "desert_wind_relax.mp3", "guide_file": "relax_breathing_soft.mp3", "light_scene": "sunset_dim", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "sedona_red_rock_peace", "stage_name": "Induce", "audio_file": "desert_night_induce.mp3", "guide_file": "countdown_sleep.mp3", "light_scene": "warm_amber_low", "aroma_mode": "lavender_cedar"},
            {"cmd_name": "sedona_red_rock_peace", "stage_name": "Deep", "audio_file": "desert_brown_noise.mp3", "guide_file": "none.mp3", "light_scene": "micro_red_night", "aroma_mode": "lavender_chamomile"},
            {"cmd_name": "sedona_red_rock_peace", "stage_name": "Waken", "audio_file": "desert_sunrise_soft.mp3", "guide_file": "wake_up_gentle.mp3", "light_scene": "sunrise_soft", "aroma_mode": "bergamot_light"},
        ],
    },
]

_SYSTEM_PROMPT = (
    "You are Mindora's sleep intervention recommender. "
    "Return ONLY JSON with the exact field names requested. "
    "Choose scenarios only from the given candidate list. "
    "Do not invent new field names, do not add commentary, and do not include markdown fences."
)

_SOP_SYSTEM_PROMPT = (
    "You are Mindora's sleep intervention recommender. "
    "Return ONLY JSON with the exact field names requested. "
    "Choose SOP process ids only from the given candidate list. "
    "Do not choose any pure_music candidate. "
    "Do not invent new field names, do not add commentary, and do not include markdown fences."
)


def _append_llm_trace(entry_type: str, flow: str, prompt: str, response_text: str = "", error_text: str = "") -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 88,
        f"time: {timestamp}",
        f"flow: {flow}",
        f"entry_type: {entry_type}",
        "prompt:",
        prompt,
    ]
    if response_text:
        lines.extend(["response:", response_text])
    if error_text:
        lines.extend(["error:", error_text])
    lines.append("")
    try:
        _LLM_TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LLM_TRACE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    except Exception as e:
        logging.warning("failed to append llm trace log %s: %s", _LLM_TRACE_LOG_PATH, e)


def _safe_profile_json(profile: UserProfile) -> str:
    payload = profile.model_dump(mode="json", exclude_none=True)
    if isinstance(payload.get("profile"), dict) and payload["profile"].get("avatar_base64"):
        payload["profile"]["avatar_base64"] = "[omitted base64 image data]"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _summarize_profile_for_prompt(profile: UserProfile) -> str:
    """Return a compact profile snapshot suitable for LLM prompts.

    Omits bulky raw behaviors and full sleep sequences to keep prompts small
    and avoid HTTP read timeouts.
    """
    if not profile:
        return "{}"

    data: dict[str, Any] = {}
    if profile.basic_info:
        data["basic_info"] = profile.basic_info
    if profile.long_term_profile:
        data["long_term_profile"] = profile.long_term_profile
    if profile.sop_tag_profile:
        data["sop_tag_profile"] = {k: v for k, v in profile.sop_tag_profile.items()
                                   if k != "sleep_usage_links"}
    if profile.profile:
        prof = profile.profile.model_dump(mode="json", exclude_none=True)
        prof.pop("avatar_base64", None)
        data["profile"] = prof

    sleep_analysis = profile.sleep_analysis or {}
    data["sleep_analysis"] = {
        # 只保留仍有写入方的数值快照（场景统计由 update_profile 每次重算）；
        # 旧 sleep_trend_week/month（死字段，从未写入）不再传入，
        # 当前睡眠分析由 _sleep_analysis_summary 基于原始数据现算。
        "most_used_scene_7d": sleep_analysis.get("most_used_scene_7d"),
        "best_sleep_quality_scene_7d": sleep_analysis.get("best_sleep_quality_scene_7d"),
    }

    return json.dumps(data, ensure_ascii=False, indent=2)


@lru_cache(maxsize=1)
def _load_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logging.warning("failed to load %s: %s", path, e)
        return ""


def _build_prompt(profile: UserProfile) -> str:
    knowledge = _load_text(_KNOWLEDGE_BASE_PATH)
    topology = _load_text(_TOPOLOGY_PATH)
    return f"""
User profile JSON:
{_summarize_profile_for_prompt(profile)}

Current sleep analysis and advice (rule-computed from the user's own data; last night vs their 7-day baseline):
{_sleep_analysis_summary(profile, getattr(profile, "last_request_language", None) or "en")}

Sleep intervention knowledge base:
{knowledge}

Sleep strategy topology:
{topology}

Scenario candidates:
{json.dumps(_SCENARIO_CANDIDATES, ensure_ascii=False, indent=2)}

Task:
1. Read the user's CURRENT sleep analysis and advice above (onset / structure / night fluctuations / scene association). Pick scenarios that best match their current sleep pattern and the advice already given.
2. Select the best 2 scenario candidates from the provided candidate list, ordered by fit (first = best).
3. You may reorder candidates, but every returned scenario and every stage field value must come from the candidate list.
4. Keep the output schema exactly compatible with this Python model:
{{
  "scenarios": [
    {{
      "scenario_id": "string",
      "scenario_name": "string",
      "stages": [
        {{
          "cmd_name": "string",
          "stage_name": "Relax|Induce|Deep|Waken",
          "audio_file": "string",
          "guide_file": "string",
          "light_scene": "string",
          "aroma_mode": "string"
        }}
      ]
    }}
  ]
}}
5. Return exactly 2 scenarios in ranked order.
"""


def _sleep_analysis_summary(profile: UserProfile, lang: str) -> str:
    """把当前睡眠分析与建议汇总成 prompt 段落（规则结论 + 7d/当夜聚合 + 场景使用 +
    已存洞察报告），让推荐随睡眠状态变化。

    之前推荐恒定的根因：prompt 里的 sleep_analysis 只有永远为空的
    sleep_trend_week/month（死字段），LLM 看不到任何真实睡眠数据。
    """
    try:
        import insight_rules as ir
    except Exception as e:
        logging.warning("insight_rules unavailable for reco prompt: %s", e)
        return ""
    lines: list[str] = []
    try:
        tz = ir.resolve_tz(getattr(profile, "last_request_timezone", None))
        state = ir.compute_data_state(profile, tz)
        base = ir.compute_baselines(profile, tz)
        lines.append(
            f"data_state={state}; valid nights: 7d={len(base.nights_7d)}, 30d={len(base.nights_30d)}"
        )
        if base.latest is not None:
            parts = []
            if base.latest.sleep_quality is not None:
                parts.append(f"score={base.latest.sleep_quality:.0f}/100")
            if base.latest.onset is not None:
                parts.append(f"onset={base.latest.onset:.0f}min")
            summ = base.latest.sequence_summaries if base.latest.sleep_status else {}
            if summ:
                tst = summ.get("total_sleep_duration") or 0
                parts.append(f"TST={tst:.0f}min")
                parts.append(f"WASO={summ.get('night_awake_duration', 0):.0f}min")
                parts.append(f"awakenings={summ.get('night_awake_count', 0)}")
            lines.append("last night: " + ", ".join(parts))
        # 规则结论（入睡/结构/波动/场景 + 建议），含与近7日个人基线的比较
        _, _, conclusions = ir.build_night_conclusions(profile, lang)
        for c in conclusions:
            if c.text:
                lines.append(f"insight[{c.key}|{c.state}] {c.title}: {c.text}")
        # 场景使用与关联表现
        top = ir.top_scenes_in_window(profile.mindora_record, end_ts=int(time.time()), days=7, limit=3)
        if top:
            lines.append("scene usage last 7d: " + ", ".join(f"{s}×{n}" for s, n in top))
        sa = profile.sleep_analysis or {}
        best = sa.get("best_sleep_quality_scene_7d")
        if best:
            lines.append(
                f"best associated scene 7d: {best.get('scene_name')} "
                f"(avg score {best.get('avg_sleep_quality')}, {best.get('nights')} nights)"
            )
        # 已存洞察报告（LLM 润色版，若已有）与建议动作
        si = getattr(profile, "sleep_insight", None)
        if si is not None:
            for key in ("onset", "architecture", "intervention", "scene_preference"):
                m = getattr(si, key, None)
                if m is not None and getattr(m, "content", ""):
                    lines.append(f"stored insight[{key}]: {m.content}")
            onset_m = getattr(si, "onset", None)
            if onset_m is not None and getattr(onset_m, "action", ""):
                lines.append(f"current advice action: {onset_m.action}")
    except Exception as e:
        logging.warning("sleep analysis summary build failed: %s", e)
    return "\n".join(lines)


def _build_sop_reco_prompt(profile: UserProfile, candidates: List[str]) -> str:
    from content_tags import candidate_metadata
    knowledge = _load_text(_KNOWLEDGE_BASE_PATH)
    topology = _load_text(_TOPOLOGY_PATH)
    return f"""
User profile JSON:
{_summarize_profile_for_prompt(profile)}

Current sleep analysis and advice (rule-computed from the user's own data; last night vs their 7-day baseline):
{_sleep_analysis_summary(profile, getattr(profile, "last_request_language", None) or "en")}

Sleep intervention knowledge base:
{knowledge}

Sleep strategy topology:
{topology}

Standard SOP process candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Workbook content metadata (null means unannotated; do not guess missing tags):
{json.dumps(candidate_metadata(candidates), ensure_ascii=False, indent=2)}
Exposure means content was used, not liked or fully heard. Outcome associations are
observations, not causal improvements. Workbook goals/health clues are unvalidated
annotations, not user outcomes. Guide variants are not confirmed played.

Task:
1. Read the user's CURRENT sleep analysis and advice above (onset / structure / night fluctuations / scene association). Pick SOP processes that best match their current sleep pattern and the advice already given — e.g. slow onset → stronger relaxation/induction; frequent awakenings → steadier deep-stage sound; a well-associated scene → keep its acoustic style.
2. Select the best 3 SOP process candidates from the provided candidate list, ordered by fit (first = best).
3. You may reorder candidates, but every returned value must come from the candidate list.
4. Do not return any `sleep.pure_music.*` candidate. Restrict the result to guided `sleep.scene.*` candidates only.
5. Keep the output schema exactly compatible with this JSON structure:
{{
  "scenarios": [
    {{
      "scenario_id": null,
      "scenario_name": null,
      "stages": [
        {{
          "cmd_name": "string",
          "stage_name": null,
          "audio_file": null,
          "guide_file": null,
          "light_scene": null,
          "aroma_mode": null
        }}
      ]
    }}
  ]
}}
6. Only set `cmd_name`; all other fields should be null.
7. Return up to 3 SOP process ids in ranked order, or all candidates if fewer than 3 are available.
"""


_ROUTE_FAILURE_UNTIL = {}
_ROUTE_FAILURE_LOCK = threading.Lock()


def _route_key(route):
    return (route.name, route.api_base, route.model)


def _recommendation_models():
    """Lazy initialization lets one broken provider fall through to the next."""
    try:
        router = ModelRouter.from_env()
    except Exception as e:
        logging.error("sleep recommendation llm init failed: %s", e)
        return
    for route in router.available_routes("sleep_reco"):
        with _ROUTE_FAILURE_LOCK:
            cooling_down = _ROUTE_FAILURE_UNTIL.get(_route_key(route), 0) > time.monotonic()
        if cooling_down:
            continue
        try:
            model = router.chat_model_for_route(route, temperature=0.3)
            if model is not None:
                yield route, model
        except Exception:
            _mark_route_failed(route)
            logging.exception("recommendation model initialization failed: route=%s", route.name)


def _mark_route_failed(route):
    with _ROUTE_FAILURE_LOCK:
        _ROUTE_FAILURE_UNTIL[_route_key(route)] = time.monotonic() + 300


def _invoke_recommendation(prompt, system_prompt, flow, validate, expected):
    for route, model in _recommendation_models():
        route_flow = f"{flow}:{route.name}"
        _append_llm_trace("request", route_flow, prompt)
        try:
            response = model.invoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
            raw = response.content
            _append_llm_trace("response", route_flow, prompt, response_text=str(raw))
            result = validate(_extract_json(raw))
            if len(result) != expected:
                raise ValueError(f"Expected {expected} valid candidates, received {len(result)}")
            with _ROUTE_FAILURE_LOCK:
                _ROUTE_FAILURE_UNTIL.pop(_route_key(route), None)
            logging.info("recommendation LLM succeeded: flow=%s route=%s", flow, route.name)
            return result, route.name
        except Exception as e:
            _mark_route_failed(route)
            _append_llm_trace("error", route_flow, prompt, error_text=str(e))
            logging.warning("recommendation LLM failed: flow=%s route=%s; trying next provider (%s)", flow, route.name, type(e).__name__)
    return None, None


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    logging.warning("sleep recommendation llm returned non-json: %.200s", text)
    return None


def _validate_scenarios(payload: Any) -> List[SleepScenario]:
    if isinstance(payload, dict):
        payload = payload.get("scenarios", [])
    if not isinstance(payload, list):
        return []

    scenarios: List[SleepScenario] = []
    for item in payload[:2]:
        try:
            scenarios.append(SleepScenario.model_validate(item))
        except Exception as e:
            logging.warning("invalid sleep scenario from llm: %s item=%s", e, item)
    return scenarios


def _fallback_scenarios() -> List[SleepScenario]:
    return [SleepScenario.model_validate(item) for item in _SCENARIO_CANDIDATES[:2]]


def _default_sop_candidates() -> List[str]:
    profile_candidates = [
        short_scene_id(key)
        for key in UserProfile().mindora_record.keys()
    ]
    return list(dict.fromkeys(profile_candidates))


@lru_cache(maxsize=1)
def _load_sop_candidate_scenarios() -> List[SleepScenario]:
    try:
        with open(_SOP_CANDIDATES_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logging.warning("failed to load SOP candidates from %s: %s", _SOP_CANDIDATES_PATH, e)
        return [_build_sop_reco_scenario(item) for item in _default_sop_candidates()]

    if not isinstance(payload, list):
        logging.warning("SOP candidates file must contain a JSON array: %s", _SOP_CANDIDATES_PATH)
        return [_build_sop_reco_scenario(item) for item in _default_sop_candidates()]

    scenarios: List[SleepScenario] = []
    for item in payload:
        try:
            scenario = SleepScenario.model_validate(item)
        except Exception as e:
            logging.warning("invalid SOP candidate in %s: %s item=%s", _SOP_CANDIDATES_PATH, e, item)
            continue
        cmd_name = _extract_sop_cmd_name(scenario)
        if cmd_name is None or _is_pure_music_cmd(cmd_name):
            continue
        scenarios.append(scenario)
    return scenarios


def _build_sop_reco_scenario(cmd_name: str) -> SleepScenario:
    return SleepScenario(
        scenario_id=None,
        scenario_name=cmd_name,
        stages=[
            {
                "cmd_name": cmd_name,
                "stage_name": None,
                "audio_file": None,
                "guide_file": None,
                "light_scene": None,
                "aroma_mode": None,
            }
        ],
    )


def _clone_sleep_scenario(scenario: SleepScenario) -> SleepScenario:
    return SleepScenario.model_validate(scenario.model_dump())


def _extract_sop_cmd_name(item: Any) -> Optional[str]:
    if isinstance(item, SleepScenario):
        if item.stages and item.stages[0].cmd_name:
            return item.stages[0].cmd_name
        return None
    if not isinstance(item, dict):
        return None

    stages = item.get("stages", [])
    if not isinstance(stages, list) or not stages:
        return None
    first_stage = stages[0]
    if not isinstance(first_stage, dict):
        return None
    cmd_name = first_stage.get("cmd_name")
    if isinstance(cmd_name, str) and cmd_name:
        return cmd_name
    return None


def _is_pure_music_cmd(cmd_name: Optional[str]) -> bool:
    return isinstance(cmd_name, str) and cmd_name.startswith("sleep.pure_music.")


def _validate_sop_reco(payload: Any, candidates: List[SleepScenario]) -> List[SleepScenario]:
    if isinstance(payload, dict):
        payload = payload.get("scenarios", [])
    if not isinstance(payload, list):
        return []

    candidate_map = {}
    for scenario in candidates:
        cmd_name = _extract_sop_cmd_name(scenario)
        if cmd_name is not None and not _is_pure_music_cmd(cmd_name):
            candidate_map[cmd_name] = scenario

    reco: List[SleepScenario] = []
    seen_cmd_names: set[str] = set()
    for item in payload:
        cmd_name = _extract_sop_cmd_name(item)
        if cmd_name is None or _is_pure_music_cmd(cmd_name):
            continue
        if cmd_name not in candidate_map or cmd_name in seen_cmd_names:
            continue
        reco.append(_clone_sleep_scenario(candidate_map[cmd_name]))
        seen_cmd_names.add(cmd_name)
        if len(reco) == 3:
            break
    return reco


def _fallback_sop_reco(candidates: List[SleepScenario]) -> List[SleepScenario]:
    if candidates:
        return [_clone_sleep_scenario(item) for item in candidates[:3]]
    return [_build_sop_reco_scenario(item) for item in _default_sop_candidates()[:3]]


class RecommendationEngine:
    """根据用户画像生成 Sleep Scenarios 的引擎"""

    @staticmethod
    def should_rerun_recommendation(old_profile: Optional[UserProfile], new_profile: UserProfile) -> bool:
        """层级判断逻辑"""
        if not old_profile or not old_profile.sleep_scenarios_reco:
            return True

        def get_metric(profile, key):
            for k, v in profile.long_term_profile:
                if k == key:
                    return v
            return None

        old_stress = get_metric(old_profile, "stress_index")
        new_stress = get_metric(new_profile, "stress_index")
        if old_stress is not None and new_stress is not None:
            if abs(old_stress - new_stress) > 0.3:
                return True

        return False

    @staticmethod
    def generate(profile: UserProfile) -> List[SleepScenario]:
        prompt = _build_prompt(profile)
        scenarios, _route = _invoke_recommendation(prompt, _SYSTEM_PROMPT, "sleep_scenario_reco", _validate_scenarios, 2)
        return scenarios if scenarios is not None else _fallback_scenarios()

    @staticmethod
    def generate_sop_reco(profile: UserProfile, candidates: Optional[List[str]] = None) -> List[SleepScenario]:
        file_candidates = _load_sop_candidate_scenarios()
        candidate_scenarios = file_candidates
        if not candidate_scenarios:
            normalized_candidates = list(dict.fromkeys(item for item in (candidates or []) if isinstance(item, str) and item))
            candidate_scenarios = [_build_sop_reco_scenario(item) for item in normalized_candidates]
        if not candidate_scenarios:
            candidate_scenarios = [_build_sop_reco_scenario(item) for item in _default_sop_candidates()]

        from content_tags import canonical_cmd
        from sop_ranking import rank_sops, rules

        # Normalize and deduplicate at the execution seam, including fallback inputs.
        unique = {}
        for scenario in candidate_scenarios:
            cmd = _extract_sop_cmd_name(scenario)
            if cmd and not _is_pure_music_cmd(cmd) and canonical_cmd(cmd).startswith("sleep.scene."):
                unique.setdefault(canonical_cmd(cmd), scenario)
        candidate_scenarios = list(unique.values())
        commands = [_extract_sop_cmd_name(s) for s in candidate_scenarios]
        ranking = rank_sops(profile, commands)
        candidate_map = {_extract_sop_cmd_name(s): s for s in candidate_scenarios}
        ranked_candidates = [candidate_map[row["cmd_name"]] for row in ranking]
        details = {
            "rules_version": rules()["version"], "generated_at": int(time.time()),
            "basis": "current_profile_snapshot", "ranking": ranking,
            "historical": sorted([r for r in ranking if any(e["type"] in ("explicit_preference", "outcome_association")
                for e in r["evidence"])], key=lambda r: -r["historical_score"])[:3],
            "current": ranking[:3], "llm_route": None, "selection_method": "rules",
            "limitations": ["Outcome scores describe associations, not causal improvement.",
                            "Exposure is not preference; unavailable feedback contributes zero.",
                            "Profile goals are not a separately collected daily state."]}
        profile.sop_recommendation_details = details
        details["historical_status"] = "available" if details["historical"] else "insufficient_evidence"
        if not ranked_candidates:
            return []
        prompt = _build_sop_reco_prompt(profile, [row["cmd_name"] for row in ranking])
        prompt += "\nRule ranking evidence (higher scores take priority):\n" + json.dumps(ranking, ensure_ascii=False)
        prompt += f"\nReturn exactly {min(3, len(ranking))} distinct candidates. Use content fit to resolve equal rule scores."
        proposed, route = _invoke_recommendation(prompt, _SOP_SYSTEM_PROMPT, "sleep_sop_reco",
            lambda payload: _validate_sop_reco(payload, ranked_candidates), min(3, len(ranking)))
        if proposed is not None:
            llm_order = {_extract_sop_cmd_name(s): i for i, s in enumerate(proposed)}
            ranking = sorted(ranking, key=lambda r: (-r["score"], llm_order.get(r["cmd_name"], len(ranking))))
            details.update(llm_route=route, selection_method="rules_with_llm_tiebreak", ranking=ranking, current=ranking[:3])
        return [_clone_sleep_scenario(candidate_map[row["cmd_name"]]) for row in ranking[:3]]
