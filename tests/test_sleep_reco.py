import time

from user_profile import UserProfile, SleepResult
from llm import reco as sr


def _profile() -> UserProfile:
    return UserProfile(
        long_term_profile=[
            ("sleep_type_ONSET", 1.0),
            ("stress_level_none", 1.0),
            ("sensitivity_light_sensitive", 1.0),
            ("bedroom_env_noisy", 1.0),
            ("stress_index", 0.5),
        ],
        sleep_data=[
            SleepResult(
                timestamp=int(time.time()),
                sleep_quality=81.0,
                soe=74.0,
                first_sleep_time="23:20",
                hr_before_sleep=68,
                rr_before_sleep=14,
                avg_heart_rate=61,
                hrv=40,
            )
        ],
    )


def test_prompt_contains_candidates_and_schema():
    prompt = sr._build_prompt(_profile())
    assert "Scenario candidates" in prompt
    assert "cocos_island_moonlight_v1" in prompt
    assert '"cmd_name"' in prompt
    assert '"scenarios"' in prompt


def test_validate_scenarios_accepts_wrapped_payload():
    payload = {"scenarios": sr._SCENARIO_CANDIDATES[:2]}
    scenarios = sr._validate_scenarios(payload)
    assert len(scenarios) == 2
    assert scenarios[0].scenario_id == sr._SCENARIO_CANDIDATES[0]["scenario_id"]
    assert scenarios[0].stages[0].cmd_name == sr._SCENARIO_CANDIDATES[0]["stages"][0]["cmd_name"]


def test_generate_falls_back_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(sr, "_recommendation_models", lambda: [])
    scenarios = sr.RecommendationEngine.generate(_profile())
    assert len(scenarios) == 2
    assert scenarios[0].scenario_id == sr._SCENARIO_CANDIDATES[0]["scenario_id"]


def test_menu_recommendation_puts_repeated_short_scene_outside_model_candidates(monkeypatch):
    now = int(time.time())
    rejected = sr._SCENARIO_CANDIDATES[0]
    profile = UserProfile(behaviors={"plays": [
        [now - offset, {"event": "sop_start",
                      "cmd": "sleep.scene.cocos_island_moonlight", "duration": 15}]
        for offset in (120, 60)
    ]})

    def model(prompt, system, flow, validate, expected):
        assert rejected["scenario_id"] not in prompt
        assert validate({"scenarios": [rejected]}) == []
        return None, None

    monkeypatch.setattr(sr, "_invoke_recommendation", model)
    scenarios = sr.RecommendationEngine.generate(profile)
    assert [s.scenario_id for s in scenarios] == [c["scenario_id"] for c in sr._SCENARIO_CANDIDATES[1:3]]


def test_generate_uses_fallback_on_invalid_llm_output(monkeypatch):
    class FakeResponse:
        content = '{"oops": 1}'

    class FakeModel:
        def invoke(self, messages):
            return FakeResponse()

    monkeypatch.setattr(sr, "_recommendation_models", lambda: [(sr.ModelRouter.from_env()._routes["volc_ark"], FakeModel())])
    scenarios = sr.RecommendationEngine.generate(_profile())
    assert len(scenarios) == 2
    assert scenarios[1].scenario_id == sr._SCENARIO_CANDIDATES[1]["scenario_id"]


def test_validate_sop_reco_accepts_wrapped_payload():
    candidates = [
        sr.SleepScenario.model_validate(
            {
                "scenario_id": None,
                "scenario_name": "cocos_island_moonlight",
                "stages": [{"cmd_name": "cocos_island_moonlight"}],
            }
        ),
        sr.SleepScenario.model_validate(
            {
                "scenario_id": None,
                "scenario_name": "amalfi_breeze",
                "stages": [{"cmd_name": "amalfi_breeze"}],
            }
        ),
        sr.SleepScenario.model_validate(
            {
                "scenario_id": None,
                "scenario_name": "kyoto_forest",
                "stages": [{"cmd_name": "kyoto_forest"}],
            }
        ),
    ]
    payload = {
        "scenarios": [
            {
                "scenario_id": None,
                "scenario_name": None,
                "stages": [{"cmd_name": "kyoto_forest"}],
            },
            {
                "scenario_id": None,
                "scenario_name": None,
                "stages": [{"cmd_name": "amalfi_breeze"}],
            },
            {
                "scenario_id": None,
                "scenario_name": None,
                "stages": [{"cmd_name": "unknown_one"}],
            },
            {
                "scenario_id": None,
                "scenario_name": None,
                "stages": [{"cmd_name": "cocos_island_moonlight"}],
            },
        ]
    }
    reco = sr._validate_sop_reco(payload, candidates)
    assert [scenario.stages[0].cmd_name for scenario in reco] == [
        "kyoto_forest",
        "amalfi_breeze",
        "cocos_island_moonlight",
    ]
    assert reco[0].scenario_id is None
    assert reco[0].scenario_name == "kyoto_forest"
    assert reco[0].stages[0].audio_file is None


def test_generate_sop_reco_falls_back_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(sr, "_recommendation_models", lambda: [])
    monkeypatch.setattr(
        sr,
        "_load_sop_candidate_scenarios",
        lambda: [
            sr._build_sop_reco_scenario("sleep.scene.cocos_island_moonlight"),
            sr._build_sop_reco_scenario("sleep.scene.amalfi_breeze"),
            sr._build_sop_reco_scenario("sleep.scene.kyoto_forest"),
        ],
    )
    # 现行兜底语义：确定性取候选顺序前 3 个（无随机挑选）
    reco = sr.RecommendationEngine.generate_sop_reco(
        _profile(),
        ["cocos_island_moonlight", "amalfi_breeze", "kyoto_forest"],
    )
    assert [scenario.stages[0].cmd_name for scenario in reco] == [
        "sleep.scene.cocos_island_moonlight",
        "sleep.scene.amalfi_breeze",
        "sleep.scene.kyoto_forest",
    ]
    assert reco[0].scenario_name == "sleep.scene.cocos_island_moonlight"
    assert reco[0].stages[0].guide_file is None


def test_generate_sop_reco_uses_fallback_on_invalid_llm_output(monkeypatch):
    class FakeResponse:
        content = '{"oops": 1}'

    class FakeModel:
        def invoke(self, messages):
            return FakeResponse()

    monkeypatch.setattr(sr, "_recommendation_models", lambda: [(sr.ModelRouter.from_env()._routes["volc_ark"], FakeModel())])
    monkeypatch.setattr(
        sr,
        "_load_sop_candidate_scenarios",
        lambda: [
            sr._build_sop_reco_scenario("sleep.scene.cocos_island_moonlight"),
            sr._build_sop_reco_scenario("sleep.scene.amalfi_breeze"),
            sr._build_sop_reco_scenario("sleep.scene.kyoto_forest"),
        ],
    )
    # LLM 输出无效 → 同上的确定性兜底
    reco = sr.RecommendationEngine.generate_sop_reco(
        _profile(),
        ["cocos_island_moonlight", "amalfi_breeze", "kyoto_forest"],
    )
    assert [scenario.stages[0].cmd_name for scenario in reco] == [
        "sleep.scene.cocos_island_moonlight",
        "sleep.scene.amalfi_breeze",
        "sleep.scene.kyoto_forest",
    ]
    assert reco[0].scenario_name == "sleep.scene.cocos_island_moonlight"


def test_user_profile_accepts_legacy_standard_sop_reco_strings():
    profile = UserProfile.model_validate(
        {
            "standard_sop_reco": ["cocos_island_moonlight", "amalfi_breeze"],
        }
    )
    assert [scenario.stages[0].cmd_name for scenario in profile.standard_sop_reco] == [
        "cocos_island_moonlight",
        "amalfi_breeze",
    ]
    assert [scenario.scenario_name for scenario in profile.standard_sop_reco] == [
        "cocos_island_moonlight",
        "amalfi_breeze",
    ]
