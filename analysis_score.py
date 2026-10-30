"""Versioned Mindora display score from observed nightly metrics (v3 draft)."""
from __future__ import annotations

import math
from typing import Optional

from user_profile import SleepResult, UserProfile, active_sleep_plan
from sleep_session_builder import cap_sleep_quality, resolve_sleep_quality
from sleep_metrics import estimate_sleep_window, _timeline

SCORE_VERSION = "mindora-score-v3-draft-3"
INDEX_VERSION = "mindora-insight-v3-draft-3"
LEGACY_SCORE_VERSION = "mindora-score-legacy-v1"
WEIGHTS = {"duration": .35, "efficiency": .25, "continuity": .20, "schedule": .20}


def _clamp(value: float) -> float:
  return max(0.0, min(100.0, value))


def _clock(value) -> Optional[int]:
  if isinstance(value, int) and 0 <= value < 1440:
    return value
  if isinstance(value, str):
    try:
      hour, minute = map(int, value.split(":"))
      if 0 <= hour < 24 and 0 <= minute < 60:
        return hour * 60 + minute
    except ValueError:
      pass
  return None


def _clock_distance(a: int, b: int) -> int:
  return abs((a - b + 720) % 1440 - 720)


def _bed_minutes(intervals: list[tuple[int, int]]) -> Optional[float]:
  merged: list[list[int]] = []
  for start, end in sorted(intervals):
    if start <= 0 or end <= start:
      continue
    if merged and start <= merged[-1][1]:
      merged[-1][1] = max(end, merged[-1][1])
    else:
      merged.append([start, end])
  return sum(end - start for start, end in merged) / 60 if merged else None


def effective_sleep_plan(profile: UserProfile, night_start_ts: int):
  """Use only goals known to be active when the night began."""
  plan = active_sleep_plan(profile)
  if plan is not None and plan.activated_at is not None and plan.activated_at <= night_start_ts:
    return plan
  legacy = profile.sleep_plan
  if legacy is not None and legacy.updated_at and legacy.updated_at > night_start_ts:
    return None
  return legacy


def daily_score(record: SleepResult, profile: UserProfile) -> Optional[dict]:
  """AN_SCORE with documented estimates when measured targets or TIB are absent."""
  def legacy():
    raw = resolve_sleep_quality(record)
    return {"score": int(raw), "score_version": LEGACY_SCORE_VERSION,
            "available_dimensions": [], "dimension_count": 0} if raw is not None else None
  if not record.sleep_status:
    return legacy()
  rows = _timeline(record)
  sleep_rows = [(start, end) for start, end, kind in rows if kind != "awake"]
  if not sleep_rows:
    return legacy()
  tst = sum(end - start for start, end in sleep_rows) / 60
  first_sleep, last_sleep = sleep_rows[0][0], sleep_rows[-1][1]
  wake_rows = [(max(start, first_sleep), min(end, last_sleep)) for start, end, kind in rows
               if kind == "awake" and end > first_sleep and start < last_sleep]
  waso = sum(end - start for start, end in wake_rows) / 60
  wake_episodes = sum(1 for i, (start, _) in enumerate(wake_rows)
                      if i == 0 or start > wake_rows[i-1][1])
  scores: dict[str, float] = {"continuity": _clamp(100 - 2 * waso - 10 * max(wake_episodes - 1, 0))}
  estimated_dimensions = []

  plan = effective_sleep_plan(profile, min(s.start_time for s in record.sleep_status))
  target = getattr(plan, "target_minutes", None) or getattr(plan, "target_duration_min", None)
  target_source = "personal" if target and target > 0 else "default_8h"
  if target_source != "personal":
    target = 480
    estimated_dimensions.append("duration")
  scores["duration"] = _clamp(100 - abs(tst - target) / 1.5)

  window = estimate_sleep_window(record, profile)
  tib = window["time_in_bed_minutes"] if window else _bed_minutes(record.in_bed_intervals)
  if tib and tib >= tst:
    efficiency = _clamp((tst / tib - .70) / .20 * 100)
    if window and window["source"] == "estimated":
      # A short inferred latency is only a lower bound on TIB. Do not grant a
      # perfect efficiency score from that assumption alone.
      ceiling = {"estimated": 85, "mindora_play": 85, "stage_awake": 100,
                 "reported": 90}.get(window["sleep_onset_source"], 85)
      efficiency = min(efficiency, ceiling)
      estimated_dimensions.append("efficiency")
    scores["efficiency"] = efficiency

  target_sleep = _clock(getattr(plan, "sleep_time", None) or getattr(plan, "target_bed_time", None))
  target_wake = _clock(getattr(plan, "wake_time", None) or getattr(plan, "target_wake_time", None))
  actual_sleep = _clock(record.first_sleep_time)
  actual_wake = _clock(record.wake_time)
  if None not in (target_sleep, target_wake, actual_sleep, actual_wake):
    delta = (_clock_distance(actual_sleep, target_sleep) +
             _clock_distance(actual_wake, target_wake)) / 2
    scores["schedule"] = _clamp(100 - delta / 1.2)

  if len(scores) < 3:
    return legacy()
  weight = sum(WEIGHTS[k] for k in scores)
  value = round(cap_sleep_quality(sum(WEIGHTS[k] * v for k, v in scores.items()) / weight, tst))
  return {"score": value, "score_version": SCORE_VERSION,
          "available_dimensions": sorted(scores), "dimension_count": len(scores),
          "estimated_dimensions": sorted(estimated_dimensions),
          "duration_target_source": target_source,
          "in_bed_source": window["source"] if window else "unavailable"}


def period_score(records: list[SleepResult], profile: UserProfile) -> Optional[dict]:
  scores = [daily_score(record, profile) for record in records]
  # Keep one score version in an aggregate; never mix historical raw and v3.
  latest = max(((r.timestamp, s["score_version"]) for r, s in zip(records, scores) if s), default=None)
  version = latest[1] if latest else None
  valid = [score["score"] for score in scores if score and score["score_version"] == version]
  if not valid:
    return None
  return {"score": round(sum(valid) / len(valid)), "score_version": version,
          "valid_nights": len(valid)}
