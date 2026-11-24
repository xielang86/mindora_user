"""Only sustained plays associated with a staged sleep night become usage evidence."""
import threading
import time

from analysis_score import SCORE_VERSION
from profile_service import UserProfileServ
from tool.rebuild_mindora_records import rebuilt_profile
from user_profile import SleepElement, SleepResult, UserProfile

CMD = "sleep.scene.kyoto_forest"
BASE = 1_790_000_000


def _service():
  service = UserProfileServ.__new__(UserProfileServ)
  service.lock = threading.RLock()
  return service


def _night():
  return SleepResult(timestamp=BASE + 7 * 3600, sleep_status=[
    SleepElement(start_time=BASE, duration=360, sleep_type="core")])


def _play(start, duration, cmd=CMD):
  return (start, {"cmd": cmd, "event": "sop_start", "duration": duration})


def test_gate_uses_stage_time_and_five_minute_boundary():
  service = _service()
  profile = UserProfile(sleep_data=[_night()])
  incoming = UserProfile(behaviors={"plays": [
    _play(BASE - 7201, 600),     # outside the two-hour lead window
    _play(BASE - 3600, 299),     # too short
    _play(BASE - 1800, 300),     # exactly five minutes
    _play(BASE + 1800, 420),     # playback during the staged night
    _play(BASE + 6 * 3600, 600), # after the last sleep stage
    _play(BASE + 7 * 3600, 600), # near upload timestamp, but not sleep
    _play(BASE + 30, None),      # unknown duration
  ]})
  profile.behaviors["plays"] = incoming.behaviors["plays"]
  service._update_mindora_record(profile)
  assert profile.mindora_record[CMD] == [(BASE - 1800, 300), (BASE + 1800, 420)]


def test_play_is_deferred_until_sleep_data_arrives_and_retries_are_idempotent():
  service = _service()
  first = service._apply_basic_update("u", UserProfile(behaviors={
    "plays": [_play(BASE - 1800, 350)]}), None)
  assert first.behaviors["plays"]
  assert not any(first.mindora_record.values())

  second = service._apply_basic_update("u", UserProfile(sleep_data=[_night()]), first)
  assert second.mindora_record[CMD] == [(BASE - 1800, 350)]
  third = service._apply_basic_update("u", UserProfile(), second)
  assert third.mindora_record[CMD] == [(BASE - 1800, 350)]


def test_score_only_night_does_not_qualify_and_rebuild_clears_legacy_rows():
  service = _service()
  profile = UserProfile(
    sleep_data=[SleepResult(timestamp=BASE + 7 * 3600, sleep_quality=90)],
    behaviors={"plays": [_play(BASE - 1800, 600)]},
    mindora_record={CMD: [(BASE - 1800, 600)], "sleep.scene.old": [(BASE, 900)]},
  )
  updated = rebuilt_profile(service, profile)
  assert updated.mindora_record == {}
  assert profile.mindora_record[CMD] == [(BASE - 1800, 600)]

  profile.sleep_data = [_night()]
  updated = rebuilt_profile(service, profile)
  assert updated.mindora_record == {CMD: [(BASE - 1800, 600)]}
  assert updated.sleep_analysis["most_used_scene"]["count"] == 1


def test_best_scene_does_not_reuse_a_previous_nights_play():
  now = int(time.time())
  first, second = now - 10 * 3600, now - 2 * 3600
  def scored_night(start, minutes, score):
    return SleepResult(timestamp=start + (minutes + 30) * 60,
                       sleep_quality=score, score_version=SCORE_VERSION,
                       score_details={"score": score, "score_version": SCORE_VERSION},
                       sleep_status=[SleepElement(start_time=start, duration=minutes,
                                                  sleep_type="core")])
  profile = UserProfile(sleep_data=[scored_night(first, 240, 80),
                                    scored_night(second, 60, 70)],
                        mindora_record={CMD: [(first - 1800, 600)]})
  assert UserProfileServ._pick_best_sleep_quality_scene(profile) is None
  profile.mindora_record[CMD].append((second - 1800, 600))
  assert UserProfileServ._pick_best_sleep_quality_scene(profile)["avg_sleep_quality"] == 75
