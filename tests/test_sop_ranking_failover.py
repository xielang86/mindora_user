from types import SimpleNamespace

import pytest

from llm import reco
from llm.router import LLMRoute, ModelRouter
from sop_ranking import rank_sops, rules
from user_profile import UserProfile, SleepHealthProfile, SleepResult, SleepElement


OCEAN = "sleep.scene.cocos_island_moonlight"
FOREST = "sleep.scene.andaman_rainforest_sanctuary"
PURE_MUSIC = "sleep.pure_music.ocean_wave"


@pytest.fixture(autouse=True)
def isolated_routes(monkeypatch):
    reco._ROUTE_FAILURE_UNTIL.clear()
    monkeypatch.setattr(reco, "_append_llm_trace", lambda *a, **kw: None)
    yield
    reco._ROUTE_FAILURE_UNTIL.clear()


def router_with_models(monkeypatch, first, second):
    routes = {name: LLMRoute(name, "openai", "fake", "https://example.invalid", "fake-model")
              for name in ("kimi", "volc_ark")}
    router = ModelRouter(routes, {"default": "kimi"})
    models = {"kimi": first, "volc_ark": second}
    monkeypatch.setattr(router, "chat_model_for_route", lambda route, **kw: models[route.name])
    monkeypatch.setattr(ModelRouter, "from_env", lambda: router)
    return router


class FakeModel:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def invoke(self, messages):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(content=self.result)


@pytest.mark.parametrize("failure", [RuntimeError("403 permission_error"), TimeoutError(), "not json", '{"scenarios": []}'])
def test_sop_switches_to_ark_and_cools_down_failed_provider(monkeypatch, failure):
    kimi = FakeModel(failure)
    ark = FakeModel('{"scenarios": [{"stages": [{"cmd_name": "' + OCEAN + '"}]}]}')
    router_with_models(monkeypatch, kimi, ark)
    monkeypatch.setattr(reco, "_load_sop_candidate_scenarios", lambda: [reco._build_sop_reco_scenario(OCEAN)])
    profile = UserProfile()
    for _ in range(2):
        result = reco.RecommendationEngine.generate_sop_reco(profile)
        assert result[0].stages[0].cmd_name == OCEAN
        assert profile.sop_recommendation_details["llm_route"] == "volc_ark"
    assert kimi.calls == 1 and ark.calls == 2


def test_normal_scenarios_also_fail_over(monkeypatch):
    import json
    first = FakeModel(RuntimeError("provider unavailable"))
    second = FakeModel(json.dumps({"scenarios": reco._SCENARIO_CANDIDATES[:2]}))
    router_with_models(monkeypatch, first, second)
    assert len(reco.RecommendationEngine.generate(UserProfile())) == 2
    assert first.calls == second.calls == 1


def test_initialization_failure_does_not_block_ark(monkeypatch):
    router = router_with_models(monkeypatch, None, None)
    def build(route, **kw):
        if route.name == "kimi":
            raise ValueError("SDK init error")
        return FakeModel("{}")
    monkeypatch.setattr(router, "chat_model_for_route", build)
    assert [r.name for r, m in reco._recommendation_models()] == ["volc_ark"]


def test_rules_work_when_all_models_fail_and_include_pure_music(monkeypatch):
    router_with_models(monkeypatch, FakeModel(RuntimeError()), FakeModel(RuntimeError()))
    monkeypatch.setattr(reco, "_load_sop_candidate_scenarios", lambda: [reco._build_sop_reco_scenario(c)
                         for c in (FOREST, PURE_MUSIC, OCEAN)])
    profile = UserProfile(sleep_health=SleepHealthProfile(improvement_goal="入睡时间"),
                          long_term_profile=[("sensitivity_noise_sensitive", 1)])
    result = reco.RecommendationEngine.generate_sop_reco(profile)
    commands = [s.stages[0].cmd_name for s in result]
    assert commands[0] == OCEAN
    assert set(commands) == {OCEAN, FOREST, PURE_MUSIC}
    assert profile.sop_recommendation_details["selection_method"] == "rules"
    assert profile.sop_recommendation_details["ranking"][0]["components"]["profile_goal"] > 0


def test_llm_can_select_pure_music_for_a_sleep_plan(monkeypatch):
    model = FakeModel('{"scenarios": [{"stages": [{"cmd_name": "' + PURE_MUSIC + '"}]}]}')
    router_with_models(monkeypatch, model, model)
    monkeypatch.setattr(reco, "_load_sop_candidate_scenarios",
                        lambda: [reco._build_sop_reco_scenario(PURE_MUSIC)])
    profile = UserProfile()
    scenario, = reco.RecommendationEngine.generate_sop_reco(profile)
    assert scenario.scenario_name == scenario.stages[0].cmd_name == PURE_MUSIC
    assert profile.sop_recommendation_details["selection_method"] == "rules_with_llm_tiebreak"


def test_exposure_is_not_a_preference_and_sparse_outcomes_are_ignored():
    profile = UserProfile(sop_tag_profile={"tag_exposure": {"nature_sound:平缓海浪": {"usage_count": 999}},
        "tag_outcome_associations": {"nature_sound:平缓海浪": {"onset_minutes": {"sample_count": 1, "mean": 1}}}})
    assert all(r["score"] == 0 for r in rank_sops(profile, [OCEAN, FOREST]))


def test_sample_backed_outcomes_affect_ranking_without_causal_claims():
    profile = UserProfile(sleep_data=[SleepResult(timestamp=t, onset=40, sleep_status=[
        SleepElement(start_time=t, duration=300, sleep_type="core")]) for t in (10000, 100000, 200000)],
        sop_tag_profile={"tag_outcome_associations": {"nature_sound:平缓海浪": {
            "onset_minutes": {"sample_count": 3, "mean": 20}}}})
    rows = rank_sops(profile, [FOREST, OCEAN])
    assert rows[0]["cmd_name"] == OCEAN
    assert rows[0]["components"]["outcome_association"] > 0
    assert rows[0]["evidence"][0]["sample_count"] == 3


def test_llm_cannot_override_better_rule_score(monkeypatch):
    model = FakeModel('{"scenarios": [{"stages": [{"cmd_name": "' + FOREST + '"}]}, {"stages": [{"cmd_name": "' + OCEAN + '"}]}]}')
    router_with_models(monkeypatch, model, model)
    monkeypatch.setattr(reco, "_load_sop_candidate_scenarios", lambda: [reco._build_sop_reco_scenario(c) for c in (FOREST, OCEAN)])
    profile = UserProfile(sleep_health=SleepHealthProfile(improvement_goal="入睡时间"),
                          long_term_profile=[("sensitivity_noise_sensitive", 1)])
    assert reco.RecommendationEngine.generate_sop_reco(profile)[0].stages[0].cmd_name == OCEAN


def test_repeated_short_plays_lower_rank_without_double_counting_retries():
    now = 1_800_000_000
    profile = UserProfile(behaviors={"plays": [
        [now - 100, {"event": "sop_start", "cmd": PURE_MUSIC, "duration": 12}],
        [now - 100, {"event": "sop_start", "cmd": "pure_music.ocean_wave", "duration": 12}],
        [now - 200, {"event": "sop_start", "cmd": PURE_MUSIC, "duration": 25}],
        [now - 300, {"event": "sop_start", "cmd": PURE_MUSIC, "duration": 320}],
        [now - 400, {"event": "sop_start", "cmd": PURE_MUSIC, "duration": 1}],
    ]})
    rows = rank_sops(profile, [PURE_MUSIC, OCEAN], now=now)
    assert rows[0]["cmd_name"] == OCEAN
    assert rows[1]["recent_plays"] == 3
    feedback, = rows[1]["evidence"]
    assert feedback == {"type": "repeated_short_play", "short_plays": 2, "recorded_plays": 3}
    assert rows[1]["components"]["short_play_penalty"] > 0
    later = rank_sops(profile, [PURE_MUSIC], now=now + 15 * 86400)
    assert later[0]["components"]["short_play_penalty"] == 0


def test_exploration_keeps_an_eligible_third_slot_when_llm_fails(monkeypatch):
    config = rules()["exploration"]
    monkeypatch.setitem(config, "probability", 1)
    commands = [OCEAN, FOREST, "sleep.scene.kyoto_forest", PURE_MUSIC]
    monkeypatch.setattr(reco, "_load_sop_candidate_scenarios",
                        lambda: [reco._build_sop_reco_scenario(cmd) for cmd in commands])
    requested = []
    def unavailable(prompt, system, flow, validate, expected):
        requested.append(expected)
        return None, None
    monkeypatch.setattr(reco, "_invoke_recommendation", unavailable)

    profile = UserProfile()
    result = reco.RecommendationEngine.generate_sop_reco(profile, uid="test-account")
    assert [s.stages[0].cmd_name for s in result] == [OCEAN, FOREST, PURE_MUSIC]
    assert profile.sop_recommendation_details["exploration"] == {"slot": 3, "cmd_name": PURE_MUSIC}
    assert PURE_MUSIC not in profile.sop_recommendation_details["llm_candidate_commands"]
    assert requested == [2]


def test_unknown_metadata_is_not_assumed_safe_for_sensitive_user():
    profile = UserProfile(long_term_profile=[("sensitivity_noise_sensitive", 1)])
    rows = rank_sops(profile, ["sleep.scene.unknown", OCEAN])
    assert rows[0]["cmd_name"] == OCEAN
    assert rows[1]["evidence"][0]["type"] == "unknown_sound_metadata"


def test_failed_route_is_retried_after_cooldown(monkeypatch):
    router = router_with_models(monkeypatch, FakeModel("{}"), FakeModel("{}"))
    clock = [1000]
    monkeypatch.setattr(reco.time, "monotonic", lambda: clock[0])
    reco._mark_route_failed(router._routes["kimi"])
    assert [r.name for r, m in reco._recommendation_models()] == ["volc_ark"]
    clock[0] += 301
    assert [r.name for r, m in reco._recommendation_models()] == ["kimi", "volc_ark"]
