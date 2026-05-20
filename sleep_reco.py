import json
import logging
import os
import random
import re
from functools import lru_cache
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from tool.doubao_langchain import VolcEngineArkChat
from user_profile import UserProfile, SleepScenario


_KNOWLEDGE_BASE_PATH = os.path.join(
    os.path.dirname(__file__),
    "db",
    "knowledge_base.md",
)

_TOPOLOGY_PATH = os.path.join(
    os.path.dirname(__file__),
    "db",
    "topology.md",
)

_SOP_CANDIDATES_PATH = os.path.join(
    os.path.dirname(__file__),
    "data",
    "reco_candidates.json",
)

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


def _safe_profile_json(profile: UserProfile) -> str:
    payload = profile.model_dump(mode="json", exclude_none=True)
    if isinstance(payload.get("profile"), dict) and payload["profile"].get("avatar_base64"):
        payload["profile"]["avatar_base64"] = "[omitted base64 image data]"
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
{_safe_profile_json(profile)}

Sleep intervention knowledge base:
{knowledge}

Sleep strategy topology:
{topology}

Scenario candidates:
{json.dumps(_SCENARIO_CANDIDATES, ensure_ascii=False, indent=2)}

Task:
1. Read the user profile and infer the most likely sleep issue pattern, preferences, and suitable intervention style.
2. Select the best 2 scenario candidates from the provided candidate list.
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
5. Return exactly 2 scenarios.
"""


def _build_sop_reco_prompt(profile: UserProfile, candidates: List[str]) -> str:
    knowledge = _load_text(_KNOWLEDGE_BASE_PATH)
    topology = _load_text(_TOPOLOGY_PATH)
    return f"""
User profile JSON:
{_safe_profile_json(profile)}

Sleep intervention knowledge base:
{knowledge}

Sleep strategy topology:
{topology}

Standard SOP process candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Task:
1. Read the user profile and infer the most likely sleep issue pattern, preferences, and suitable intervention style.
2. Select the best 3 SOP process candidates from the provided candidate list.
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
7. Return exactly 3 SOP process ids.
"""


def _get_model() -> Optional[VolcEngineArkChat]:
    api_key = os.getenv("ARK_API_KEY")
    endpoint_id = os.getenv("ARK_ENDPOINT_ID", "ep-20260325170723-znh7n")
    model = os.getenv("ARK_MODEL", "doubao-seed-2-0-lite-260215")
    if not api_key:
        return None
    try:
        return VolcEngineArkChat(
            ark_api_key=api_key,
            endpoint_id=endpoint_id,
            model=model,
            temperature=0.3,
        )
    except Exception as e:
        logging.error("sleep recommendation llm init failed: %s", e)
        return None


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
        key.replace("sleep.scene.", "")
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
        scenario_name=None,
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


def _pick_random_sop_reco(scenarios: List[SleepScenario]) -> List[SleepScenario]:
    if not scenarios:
        return []
    return [random.choice(scenarios)]


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
        model = _get_model()
        if model is None:
            logging.warning("ARK_API_KEY not set, using fallback sleep scenario candidates")
            return _fallback_scenarios()

        prompt = _build_prompt(profile)
        try:
            response = model.invoke([
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            parsed = _extract_json(response.content)
            scenarios = _validate_scenarios(parsed)
            if len(scenarios) == 2:
                return scenarios
            logging.warning("sleep recommendation llm returned %s valid scenarios, using fallback", len(scenarios))
        except Exception as e:
            logging.error("sleep recommendation llm call failed: %s", e)

        return _fallback_scenarios()

    @staticmethod
    def generate_sop_reco(profile: UserProfile, candidates: Optional[List[str]] = None) -> List[SleepScenario]:
        file_candidates = _load_sop_candidate_scenarios()
        candidate_scenarios = file_candidates
        if not candidate_scenarios:
            normalized_candidates = list(dict.fromkeys(item for item in (candidates or []) if isinstance(item, str) and item))
            candidate_scenarios = [_build_sop_reco_scenario(item) for item in normalized_candidates]
        if not candidate_scenarios:
            candidate_scenarios = [_build_sop_reco_scenario(item) for item in _default_sop_candidates()]

        normalized_candidates = [
            cmd_name
            for scenario in candidate_scenarios
            if (cmd_name := _extract_sop_cmd_name(scenario)) is not None
        ]

        fallback = _fallback_sop_reco(candidate_scenarios)
        if not fallback:
            return []

        model = _get_model()
        if model is None:
            logging.warning("ARK_API_KEY not set, using fallback SOP candidates")
            return _pick_random_sop_reco(fallback)

        prompt = _build_sop_reco_prompt(profile, normalized_candidates)
        try:
            response = model.invoke([
                SystemMessage(content=_SOP_SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ])
            parsed = _extract_json(response.content)
            reco = _validate_sop_reco(parsed, candidate_scenarios)
            if len(reco) == min(3, len(normalized_candidates)):
                return _pick_random_sop_reco(reco)
            logging.warning("sleep sop recommendation llm returned %s valid candidates, using fallback", len(reco))
        except Exception as e:
            logging.error("sleep sop recommendation llm call failed: %s", e)

        return _pick_random_sop_reco(fallback)
