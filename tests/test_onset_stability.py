"""SOE must distinguish equally fast but fragmented and sustained early sleep."""
import pytest
from sleep_session_builder import _score_onset_with_stability, resolve_sleep_onset_efficiency
from user_profile import SleepResult, SleepElement, UserProfile, ProfileData
from user_server import UserServer

BASE = 1790000000

def score(stages):
    return _score_onset_with_stability(10, [(BASE+a*60, BASE+(a+d)*60,t) for a,d,t in stages])


def test_early_wake_penalty_and_duration_frequency():
    stable = [(0,30,'core'),(30,30,'deep')]
    early = [(0,3,'core'),(3,2,'awake'),(5,25,'core'),(30,30,'deep')]
    later = [(0,10,'core'),(10,2,'awake'),(12,18,'core'),(30,30,'deep')]
    frequent = [(0,3,'core'),(3,2,'awake'),(5,5,'core'),(10,2,'awake'),(12,18,'core'),(30,30,'deep')]
    assert score(stable) == 96.4
    assert score(early) == 84.8  # (4 duration + 3 episode + 5 early) percent penalty
    assert score(later) == 89.7
    assert score(frequent) < score(early) < score(later) < score(stable)


def test_first_sustained_deep_does_not_shorten_initial_30_minutes():
    assert score([(0,15,'deep'),(15,3,'awake'),(18,42,'core')]) < 96.4
    assert score([(0,15,'deep'),(15,20,'core'),(35,5,'awake')]) == 96.4


def test_extend_to_deep_and_cap_at_60():
    assert score([(0,35,'core'),(35,5,'awake'),(40,10,'core'),(50,20,'deep')]) < 96.4
    assert score([(0,61,'core'),(61,10,'awake'),(71,20,'deep')]) == 96.4
    assert score([(0,100,'core')]) == 96.4  # no deep is not itself a penalty


def test_duplicate_split_wakes_and_deep_are_invariant():
    original = [(0,3,'core'),(3,2,'awake'),(5,25,'core'),(30,20,'deep')]
    split = [(0,3,'core'),(3,1,'awake'),(4,1,'awake'),(5,25,'core'),(30,5,'deep'),(35,15,'deep')]
    assert score(original) == score(split) == score(original+original)


def test_micro_awake_gap_and_cap():
    assert score([(0,3,'core'),(3,.5,'awake'),(3.5,56.5,'core')]) == 95.4
    assert score([(0,3,'core'),(5,55,'core')]) == 96.4  # unknown is not awake
    assert score([(0,1,'core'),(1,59,'awake')]) == 48.2
    assert _score_onset_with_stability(None, []) is None


def test_old_score_recomputed_in_profile_and_resolver_without_mutation():
    row = SleepResult(timestamp=BASE+3600, onset=10, soe=96.4,
        sleep_status=[SleepElement(start_time=BASE+a*60,duration=d,sleep_type=t)
                      for a,d,t in [(0,3,'core'),(3,2,'awake'),(5,55,'core')]])
    p = UserProfile(sleep_data=[row]); before=p.model_dump()
    assert resolve_sleep_onset_efficiency(row) == 84.8
    body=UserServer._profile_response_body(p,ProfileData())
    assert body['sleep_data'][0]['soe'] == 84.8
    assert p.model_dump() == before
    assert resolve_sleep_onset_efficiency(SleepResult(timestamp=BASE,soe=76)) == 76
