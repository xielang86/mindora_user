"""健康同步分片、混合版本和按字段迁移回归。"""
import copy

import pytest

from tests.test_health_schema_version import _serv, _ts
from user_profile import UserProfile, ProfileData
from user_server import UserServer

DAY = '2026-08-13'
TS = _ts(DAY, 1)


def update(serv, profile, behaviors, version=2):
    return serv._apply_basic_update('u', UserProfile(behaviors=behaviors), profile,
                                    health_schema_version=version, timezone='UTC')


@pytest.mark.parametrize('key', sorted(_serv().HEALTH_V2_BEHAVIOR_KEYS))
def test_v2_vitals_or_stage_increment_preserves_existing_core(key):
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600], [TS+7200, 3600]]},
                          health_sync_days={DAY: 2})
    before = copy.deepcopy(profile.behaviors['sleep_stage_light'])
    update(serv, profile, {key: [[TS+14400, 300]]})
    assert profile.behaviors['sleep_stage_light'][:2] == before
    assert profile.health_sync_days[DAY] == 2


def test_v2_same_timestamp_corrects_only_that_sample():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600], [TS+7200, 3600]]},
                          health_sync_days={DAY: 2})
    update(serv, profile, {'sleep_stage_light': [[TS, 4000]]})
    assert profile.behaviors['sleep_stage_light'] == [(TS, 4000), (TS+7200, 3600)]


def test_migration_is_per_field_and_survives_serialization_and_retries():
    serv = _serv()
    profile = UserProfile(behaviors={
        'sleep_stage_light': [[TS, 3600]], 'heart_rate': [[TS, 70]],
        'respiratory_rate': [[TS, 18]],
    })
    update(serv, profile, {'sleep_heart_rate_min': [[TS, 50]]})
    assert profile.behaviors['heart_rate']
    assert profile.behaviors['sleep_stage_light']
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'sleep_heart_rate_max': [[TS, 75]]})
    assert not profile.behaviors['heart_rate']
    assert profile.behaviors['respiratory_rate']
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'sleep_stage_light': [[TS+60, 3600]]})
    assert profile.behaviors['sleep_stage_light'] == [(TS+60, 3600)]
    # 其他字段仍 v1，但已经迁移的 core 不应再次被清理。
    profile = UserProfile.model_validate_json(profile.model_dump_json())
    update(serv, profile, {'sleep_stage_light': [[TS+7200, 3600]]})
    assert len(profile.behaviors['sleep_stage_light']) == 2
    update(serv, profile, {'sleep_respiratory_rate': [[TS, 18]]})
    assert not profile.behaviors['respiratory_rate']
    assert profile.health_sync_days[DAY] == 2
    before = copy.deepcopy(profile.behaviors)
    update(serv, profile, {'sleep_stage_light': [[TS+7200, 3600]], 'sleep_respiratory_rate': [[TS, 18]]})
    assert profile.behaviors == before


@pytest.mark.parametrize('old,new', [
    ('heart_rate_variability_sdnn','sleep_heart_rate_variability_sdnn'),
    ('respiratory_rate','sleep_respiratory_rate'),
    ('body_temperature','sleep_body_temperature'),
    ('body_temperature','sleeping_wrist_temperature'),
])
def test_renamed_field_only_purged_when_replacement_arrives(old, new):
    serv = _serv()
    previous_day = _ts('2026-08-12')
    profile = UserProfile(behaviors={old: [[previous_day, 10], [TS, 20]]})
    update(serv, profile, {'sleep_stage_deep': [[TS, 3600]]})
    assert len(profile.behaviors[old]) == 2
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {new: [[TS, 20]]})
    assert profile.behaviors[old] == [(previous_day, 10)]
    assert profile.health_sync_days[DAY] == 2
    assert profile.health_sync_days['2026-08-12'] == 1


def test_late_v1_light_cannot_overwrite_migrated_core_but_new_day_is_accepted():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]}, health_sync_days={DAY: 2})
    other = _ts('2026-08-14')
    update(serv, profile, {'sleep_stage_light': [[TS, 4000], [TS+7200, 3600], [other, 3600]]}, version=1)
    assert profile.behaviors['sleep_stage_light'] == [(TS, 3600), (other, 3600)]
    assert profile.health_sync_days == {DAY: 2, '2026-08-14': 1}


def test_late_v1_other_field_does_not_reset_core_migration():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]}, health_sync_days={DAY: 2})
    update(serv, profile, {'heart_rate': [[TS, 70]]}, version=1)
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'sleep_stage_light': [[TS+7200, 3600]]})
    assert len(profile.behaviors['sleep_stage_light']) == 2
    assert profile.behaviors['heart_rate']
    assert profile.health_sync_days[DAY] == 1


def test_empty_update_does_not_clear_data_or_change_version():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]}, health_sync_days={DAY: 1})
    update(serv, profile, {'sleep_stage_light': []})
    assert profile.behaviors['sleep_stage_light'] == [(TS, 3600)]
    assert profile.health_sync_days[DAY] == 1


def test_unknown_version_and_mislabeled_legacy_fields_do_not_claim_v2():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]})
    update(serv, profile, {'sleep_heart_rate_min': [[TS, 50]]}, version=99)
    assert profile.behaviors['sleep_stage_light']
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'heart_rate': [[TS, 70]]}, version=2)
    assert profile.health_sync_days[DAY] == 1


def test_version_zero_queries_as_legacy_and_internal_state_is_not_returned():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]}, health_sync_days={DAY: 0},
                          health_sync_field_versions={DAY: {'sleep_stage_light': 1}})
    serv.get_profile = lambda uid: profile
    assert serv.query_health_sync_days('u', DAY, DAY, 'UTC') == [{'date': DAY, 'health_schema_version': 1}]
    data = UserServer._profile_response_body(profile, ProfileData(uid='test'))
    assert 'health_sync_field_versions' not in data


def test_client_cannot_spoof_internal_migration_state():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600]]},
                          health_sync_field_versions={DAY: {'sleep_stage_light': 2}})
    result = serv._apply_basic_update('u', profile, None, health_schema_version=1, timezone='UTC')
    assert result.health_sync_field_versions[DAY]['sleep_stage_light'] == 1


def test_timezone_change_keeps_migrated_core_and_query_uses_sample_version():
    serv = _serv()
    start = _ts(DAY, 22)  # 上海次日 06:00，原登记日期为 UTC 的 DAY。
    profile = UserProfile(behaviors={'sleep_stage_light': [[start, 3600]]},
                          health_sync_days={DAY: 2}, last_request_timezone='UTC')
    serv.get_profile = lambda uid: profile
    assert serv.query_health_sync_days('u', '2026-08-14', '2026-08-14', 'Asia/Shanghai') == [
        {'date':'2026-08-14', 'health_schema_version':2}]
    serv._apply_basic_update('u', UserProfile(behaviors={'sleep_stage_light': [[start+7200,3600]]}),
                             profile, health_schema_version=2, timezone='Asia/Shanghai')
    assert len(profile.behaviors['sleep_stage_light']) == 2
    assert profile.health_sync_days == {'2026-08-14':2}
    assert profile.health_sync_timezone == 'Asia/Shanghai'
    assert serv.query_health_sync_days('u', DAY, DAY, 'UTC') == [{'date':DAY, 'health_schema_version':2}]


def test_hr_min_max_must_pair_at_same_session_timestamp_before_migration():
    serv = _serv()
    profile = UserProfile(behaviors={'heart_rate': [[TS, 70]]})
    update(serv, profile, {'sleep_heart_rate_min': [[TS, 50]], 'sleep_heart_rate_max': [[TS+3600,80]]})
    assert profile.behaviors['heart_rate']
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'sleep_heart_rate_max': [[TS, 80]]})
    assert not profile.behaviors['heart_rate']
    assert profile.health_sync_days[DAY] == 2


def test_unspecified_migration_replaces_only_matching_legacy_light_samples():
    serv = _serv()
    profile = UserProfile(behaviors={'sleep_stage_light': [[TS, 3600],[TS+7200,3600]]})
    update(serv, profile, {'sleep_stage_unspecified': [[TS, 3600]]})
    assert profile.behaviors['sleep_stage_light'] == [(TS+7200,3600)]
    assert profile.health_sync_days[DAY] == 1
    update(serv, profile, {'sleep_stage_unspecified': [[TS+7200,3600]]})
    assert not profile.behaviors['sleep_stage_light']
    assert profile.health_sync_days[DAY] == 2
    assert len(profile.behaviors['sleep_stage_unspecified']) == 2
