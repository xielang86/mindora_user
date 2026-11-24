from collections import Counter
from content_tags import load_catalog, candidate_metadata
from sop_tag_profile import rebuild_sop_tag_profile
from user_profile import UserProfile, SleepResult, SleepElement

CMD = "sleep.scene.cocos_island_moonlight"


def test_catalog_preserves_sources_and_variants():
    entries = load_catalog()
    assert Counter(e["kind"] for e in entries) == {"sop": 10, "nature_audio": 17, "wake_audio": 3}
    sops = [e for e in entries if e["kind"] == "sop"]
    assert all(e["cmd_name"] and len(e["guide_variants"]) == 5 for e in sops)
    assert "nature_sound:平缓海浪" in sops[0]["tags"]
    assert candidate_metadata(["sleep.scene.unknown"])[0]["content"] is None


def make_profile(records=None):
    return UserProfile(mindora_record=records or {CMD: [(10000, None), (10000, None)]},
        sleep_data=[SleepResult(timestamp=900000, onset=20, sleep_quality=80,
            sleep_status=[SleepElement(start_time=11000, duration=400, sleep_type="core")])])


def test_rebuild_deduplicates_and_separates_exposure_from_preference():
    profile = make_profile()
    rebuild_sop_tag_profile(profile)
    evidence = profile.sop_tag_profile
    assert evidence["tag_exposure"]["nature_sound:平缓海浪"]["usage_count"] == 1
    assert evidence["tag_preferences"] == {}
    assert evidence["sleep_usage_links"][0]["association_method"] == "stage_window_inferred"
    assert evidence["tag_outcome_associations"]["nature_sound:平缓海浪"]["onset_minutes"] == {"sample_count": 1, "mean": 20}
    rebuild_sop_tag_profile(profile)
    assert profile.sop_tag_profile == evidence


def test_unknown_content_also_makes_night_ambiguous():
    profile = make_profile({CMD: [(10000, None)], "sleep.scene.unknown": [(10500, None)]})
    rebuild_sop_tag_profile(profile)
    assert profile.sop_tag_profile["sleep_usage_links"][0]["ambiguous"]
    assert profile.sop_tag_profile["tag_outcome_associations"] == {}
    assert profile.sop_tag_profile["unmapped_commands"] == ["sleep.scene.unknown"]


def test_update_timestamp_alone_does_not_associate_sleep():
    profile = make_profile()
    profile.sleep_data[0].sleep_status = []
    rebuild_sop_tag_profile(profile)
    assert profile.sop_tag_profile["sleep_usage_links"] == []


def test_real_profile_update_retries_do_not_inflate_counts():
    from profile_service import UserProfileServ
    service = UserProfileServ.__new__(UserProfileServ)
    profile = UserProfile(sleep_data=[SleepResult(timestamp=20000, sleep_status=[
        SleepElement(start_time=11000, duration=120, sleep_type="core")])])
    incoming = UserProfile(behaviors={"plays": [(10000, {"event": "sop_start", "cmd": CMD,
                                                          "duration": 300})]})
    profile.behaviors["plays"] = incoming.behaviors["plays"]
    service._update_mindora_record(profile)
    service._update_mindora_record(profile)
    assert profile.mindora_record[CMD] == [(10000, 300)]


def test_recommendation_prompt_contains_real_metadata():
    from llm.reco import _build_sop_reco_prompt
    prompt = _build_sop_reco_prompt(UserProfile(), [CMD])
    assert "平缓海浪" in prompt
    assert "not causal improvements" in prompt
