"""真实 2026-09-22 夜晚：增量上传不能丢 core，所有页面统一读取合成器 onset。"""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis_builders import build_explore, build_overview, build_sleep_day
from tests.test_health_schema_version import _serv
from user_profile import AnalysisData, UserProfile, SleepResult
from sleep_session_builder import resolve_sleep_onset, resolve_sleep_onset_efficiency
from insight_rules import compute_baselines, compute_insight_indices
from tool.dump_analysis import _effective_onset, _effective_soe

TZ = ZoneInfo('Asia/Shanghai')
DAY = '2026-09-22'
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/sleep_sync_20260922.json').read_text())


def profile_for(name):
    row = SleepResult.model_validate(FIXTURE[name])
    return UserProfile(sleep_data=[row], last_request_timezone=str(TZ), last_request_language='zh-Hans')


def behaviors_for(row):
    mapping = {'core':'sleep_stage_light','deep':'sleep_stage_deep','rem':'sleep_stage_rem','awake':'sleep_stage_awake'}
    out = {}
    for stage in row.sleep_status:
        out.setdefault(mapping[stage.sleep_type], []).append([stage.start_time, round(stage.duration * 60)])
    if row.hr_min is not None:
        ts = row.sleep_status[0].start_time
        out['sleep_heart_rate_min'] = [[ts, row.hr_min]]
        out['sleep_heart_rate_max'] = [[ts, row.hr_max]]
    return out


def test_actual_case_v2_hr_only_update_keeps_complete_night():
    p = profile_for('complete')
    original = p.sleep_data[-1]
    p.behaviors = behaviors_for(original)
    p.health_sync_days = {DAY: 2}
    serv = _serv()
    patch = UserProfile(behaviors={k:v for k,v in p.behaviors.items() if k.startswith('sleep_heart_rate_')})
    serv._apply_basic_update('u', patch, p, health_schema_version=2, timezone=str(TZ))
    after = p.sleep_data[-1]
    assert [(s.start_time, s.duration, s.sleep_type) for s in after.sleep_status] == [
        (s.start_time, s.duration, s.sleep_type) for s in original.sleep_status]
    assert after.sequence_summaries['core_sleep_duration'] == pytest.approx(324.85)
    assert after.sequence_summaries['total_sleep_duration'] == pytest.approx(517.57)
    assert (after.onset, after.soe) == (10, 78.1)
    assert (after.sleep_quality, after.hr_min, after.hr_max) == (95.2, 49, 72)


def test_actual_case_missing_core_recovers_after_stage_reupload():
    serv = _serv()
    complete = profile_for('complete').sleep_data[-1]
    p = profile_for('missing_core')
    p.behaviors = behaviors_for(p.sleep_data[-1])
    p.health_sync_days = {DAY: 2}
    serv._apply_basic_update('u', UserProfile(behaviors=behaviors_for(complete)), p,
                             health_schema_version=2, timezone=str(TZ))
    row = p.sleep_data[-1]
    assert len(p.sleep_data) == 1
    assert row.sequence_summaries['core_sleep_duration'] == pytest.approx(324.85)
    assert (row.sleep_quality, row.onset, row.soe, row.hr_min, row.hr_max) == (95.2, 10, 78.1, 49, 72)


@pytest.mark.parametrize('name,minutes,score', [('complete',10,78),('missing_core',31,57)])
def test_actual_case_all_pages_rules_and_dump_agree(name, minutes, score):
    p = profile_for(name)
    before = p.model_dump()
    d = AnalysisData(date=DAY, timezone=str(TZ), language='zh-Hans')
    home, day, explore = build_overview(d,p), build_sleep_day(d,p), build_explore(d,p)
    assert home['sleep_metrics']['sleep_onset_minutes'] == minutes
    assert day['sleep_metrics']['sleep_onset_minutes'] == minutes
    assert explore['onset_efficiency'].get('onset_minutes') == minutes
    expected_index = compute_insight_indices(p, compute_baselines(p, TZ))['onset_index']
    assert explore['onset_efficiency'].get('score') == expected_index
    assert explore['score_summary'].get('efficiency_score') == expected_index
    assert _effective_onset(FIXTURE[name]) == (44 if name == 'missing_core' else minutes)
    assert _effective_soe(FIXTURE[name]) == score
    if minutes is not None:
        assert f'{minutes} 分钟' in explore['onset_efficiency']['description']
        assert '41 分钟' not in explore['onset_efficiency']['description']
    assert p.model_dump() == before


def test_shared_legacy_fallback_is_also_used_by_rules_and_scores():
    p = profile_for('missing_core')
    row = p.sleep_data[-1]
    row.onset = row.soe = None
    assert resolve_sleep_onset(row) == 44
    assert resolve_sleep_onset_efficiency(row) == 57.5
    d = AnalysisData(date=DAY, timezone=str(TZ), language='zh-Hans')
    assert build_overview(d,p)['sleep_metrics']['sleep_onset_minutes'] == 31
    card = build_explore(d,p)['onset_efficiency']
    assert card['onset_minutes'] == 31
    assert card['score'] == compute_insight_indices(p, compute_baselines(p, TZ))['onset_index']
    assert '31 分钟' in card['description']
