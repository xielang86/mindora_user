import datetime
from types import SimpleNamespace
import pytest
from user_profile import UserProfile, SleepResult, SleepElement
from sleep_metrics import build_sleep_metrics
from analysis_score import daily_score, SCORE_VERSION
from analysis_builders import build_overview, build_sleep_day, report_text_updates
from sleep_session_builder import derive_sessions, build_sleep_result

UTC = datetime.timezone.utc
BASE = int(datetime.datetime(2026,9,14,23,tzinfo=UTC).timestamp())


def record(stages, onset=None, beds=()):
    return SleepResult(timestamp=BASE+max(a+d for a,d,_ in stages)*60 if stages else BASE,
        onset=onset, sleep_quality=80, in_bed_intervals=list(beds),
        sleep_status=[SleepElement(start_time=BASE+a*60,duration=d,sleep_type=t) for a,d,t in stages])


def metrics(r, **kwargs):
    return build_sleep_metrics(r,UserProfile(sleep_data=[r],**kwargs),UTC)


def test_measured_duplicate_beds_and_stages_are_unioned():
    r=record([(10,100,'core'),(10,100,'core'),(110,10,'awake'),(120,100,'deep')],
             beds=[(BASE,BASE+230*60),(BASE,BASE+230*60)])
    assert metrics(r)==dict(sleep_onset_minutes=10,time_in_bed_minutes=230,source='measured',date='2026-09-15',
                            sleep_onset_source='in_bed',stage_classification='staged')


def test_inferred_bedtime_and_score_with_or_without_mindora_play():
    r = record([(0,240,'core')])
    no_play = UserProfile(sleep_data=[r])
    default = build_sleep_metrics(r,no_play,UTC)
    assert (default['sleep_onset_minutes'],default['time_in_bed_minutes'],default['sleep_onset_source']) == (10,250,'estimated')
    scored = daily_score(r,no_play)
    assert scored and scored['score_version'] == SCORE_VERSION
    assert scored['dimension_count'] == 4
    assert 'efficiency' in scored['estimated_dimensions']

    nearby = UserProfile(sleep_data=[r],mindora_record={
        'sleep.scene.rain': [[BASE-30*60,60],[BASE-4*3600,60]]})
    inferred = build_sleep_metrics(r,nearby,UTC)
    assert (inferred['sleep_onset_minutes'],inferred['time_in_bed_minutes'],inferred['sleep_onset_source']) == (30,270,'mindora_play')
    assert daily_score(r,nearby)['score'] <= scored['score']

    distant = UserProfile(sleep_data=[r],mindora_record={'sleep.scene.rain': [[BASE-4*3600,60]]})
    assert build_sleep_metrics(r,distant,UTC) == default


def test_raw_inbed_and_persisted_inbed_produce_same_metrics():
    behavior={'sleep_stage_light':[[BASE+10*60,240*60]],'sleep_in_bed':[[BASE,260*60]]}
    r=build_sleep_result(derive_sessions(behavior)[0],behavior,UTC)
    assert r.in_bed_intervals==[(BASE,BASE+260*60)]
    saved=metrics(r)
    r.in_bed_intervals=[]
    assert metrics(r,behaviors=behavior)==saved
    assert saved['time_in_bed_minutes']==260
    assert saved['sleep_onset_minutes']==10


@pytest.mark.parametrize('stage,expected', [('core',10),('rem',10),('deep',10)])
def test_first_sleep_stage_uses_shared_onset_estimate(stage,expected):
    r=record([(0,240,stage)])
    m=metrics(r)
    assert m['source']=='estimated'
    assert m['sleep_onset_minutes']==expected
    assert m['time_in_bed_minutes']==240+(expected if expected is not None else 15)


def test_awake_is_not_added_twice_and_gaps_are_included():
    r=record([(0,20,'awake'),(20,100,'core'),(125,10,'awake'),(135,100,'rem')])
    assert metrics(r)['sleep_onset_minutes']==20
    assert metrics(r)['time_in_bed_minutes']==235


def test_sustained_sleep_confirmation_and_micro_awake():
    r=record([(0,10,'awake'),(10,3,'core'),(13,1,'awake'),(14,3,'core')])
    assert metrics(r)['sleep_onset_minutes']==10
    r=record([(0,10,'awake'),(10,3,'core'),(13,2,'awake'),(15,6,'core')])
    assert metrics(r)['sleep_onset_minutes']==15


def test_first_sleep_stage_with_equal_inbed_uses_estimate():
    r=record([(0,3,'core'),(10,3,'core')],beds=[(BASE,BASE+20*60)])
    assert metrics(r)['sleep_onset_minutes']==10
    assert metrics(r)['time_in_bed_minutes']==20


def test_partial_or_unrelated_inbed_is_not_measured():
    r=record([(0,240,'core')],beds=[(BASE+10*60,BASE+50*60)])
    assert metrics(r)['source']=='estimated'
    m=metrics(r,behaviors={'sleep_in_bed':[[BASE-86400,250*60]]})
    assert m['source']=='estimated'


def test_shared_first_stage_estimate_takes_precedence_over_bed_prior():
    r=record([(0,240,'core')])
    history=[]
    for i,latency in enumerate([20,25,120],1):
        old=r.model_copy(deep=True)
        old.timestamp-=i*86400
        for x in old.sleep_status:
            x.start_time-=i*86400
        old.in_bed_intervals=[(old.sleep_status[0].start_time-latency*60,old.timestamp)]
        history.append(old)
    p=UserProfile(sleep_data=history+[r])
    m=build_sleep_metrics(r,p,UTC)
    assert m['sleep_onset_minutes']==10
    assert m['time_in_bed_minutes']==250
    for old in history:
        old.in_bed_intervals=[]
        old.onset=25
    assert build_sleep_metrics(r,p,UTC)['sleep_onset_minutes']==10


@pytest.mark.parametrize('onset',[241,-1,float('nan')])
def test_bad_latency_uses_explicitly_estimated_fallback(onset):
    r=record([(0,240,'core')],onset=onset)
    assert metrics(r)['sleep_onset_minutes'] == 15
    assert metrics(r)['time_in_bed_minutes'] == 255
    assert metrics(r)['sleep_onset_source'] == 'estimated'


def test_measured_latency_uses_builder_cap_and_preserves_real_bed_duration():
    r=record([(200,240,'core')],beds=[(BASE,BASE+440*60)])
    m=metrics(r)
    assert m['sleep_onset_minutes']==200
    assert m['time_in_bed_minutes']==440
    assert m['source']=='measured'


def test_rounding_zero_missing_and_large_gap():
    r=record([(0,10,'core')],onset=0.9)
    assert metrics(r)['sleep_onset_minutes']==0
    assert metrics(r)['time_in_bed_minutes']==11
    r=record([],onset=None)
    assert metrics(r)['time_in_bed_minutes'] is None
    assert metrics(r)['sleep_onset_minutes'] is None
    assert build_sleep_metrics(None,None,UTC) is None
    r=record([(0,60,'core'),(300,60,'deep')])
    assert metrics(r)['time_in_bed_minutes'] is None


def test_home_and_day_identical_filterable_read_only_and_actual_date():
    r=record([(0,240,'deep')])
    r.soe=96.4
    p=UserProfile(sleep_data=[r])
    before=p.model_dump()
    d=SimpleNamespace(date='2026-09-18',language='en',timezone='UTC',modules=['sleep_metrics'])
    home,day=build_overview(d,p),build_sleep_day(d,p)
    assert home==day
    assert set(home)=={'sleep_metrics'}
    assert set(home['sleep_metrics'])=={'sleep_onset_minutes','time_in_bed_minutes','date','source',
                                        'sleep_onset_source','stage_classification'}
    assert home['sleep_metrics']['date']=='2026-09-15'
    assert p.model_dump()==before
    d.modules=['score_summary']
    assert 'sleep_metrics' not in build_sleep_day(d,p)
    assert build_sleep_day(d,p)['score_summary']['date']=='2026-09-15'


def test_reports_cannot_overwrite_metrics_or_date():
    report={'sleep_metrics':{'time_in_bed_minutes':999,'source':'measured'},
            'score_summary':{'score':999},'stage_insights':{'core':{'description':'text','date':'wrong'}},
            'sleep_scenarios':{'description':'story','date':'wrong'}}
    assert report_text_updates('analysis_sleep_day',report)=={
        'sleep_scenarios':{'description':'story'},'stage_insights':{'core':{'description':'text'}}}
