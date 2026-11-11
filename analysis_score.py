"""Versioned Mindora display score from observed nightly metrics (v3 draft)."""
from __future__ import annotations

import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from user_profile import SleepResult, UserProfile, active_sleep_plan
from sleep_session_builder import cap_sleep_quality, resolve_sleep_quality
from sleep_metrics import estimate_sleep_window, _timeline

SCORE_VERSION = "mindora-score-v3-draft-4"
INDEX_VERSION = "mindora-insight-v3-draft-4"
LEGACY_SCORE_VERSION = "mindora-score-legacy-v1"
WEIGHTS = {"duration": .35, "efficiency": .25, "continuity": .20, "schedule": .20}


def _clamp(value: float) -> float:
  return max(0.0, min(100.0, value))


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


def _insight_indices_for_night(record: SleepResult, profile: UserProfile, tz=None) -> dict:
  """Compute the two insight indices for this night, excluding later nights."""
  from insight_rules import compute_baselines, compute_insight_indices
  if tz is None:
    try:
      tz = ZoneInfo(profile.last_request_timezone or "UTC")
    except (KeyError, ValueError):
      tz = datetime.timezone.utc
  history = [night for night in profile.sleep_data if night.timestamp < record.timestamp]
  history.append(record)
  night_profile = profile.model_copy(update={"sleep_data": history})
  base = compute_baselines(night_profile, tz)
  if base.latest is None or base.latest.timestamp != record.timestamp:
    return {}
  return compute_insight_indices(night_profile, base)


def daily_score(record: SleepResult, profile: UserProfile, *, tz=None, insight_indices=None) -> Optional[dict]:
  """AN_SCORE using each night's structure and stability insight indices."""
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
  scores: dict[str, float] = {}
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

  indices = insight_indices if insight_indices is not None else _insight_indices_for_night(record, profile, tz)
  # Keep the response's established dimension names for client compatibility.
  # Both values come from the same night's /analysis_explore cards.
  if indices.get("stability_index") is not None:
    scores["continuity"] = indices["stability_index"]
  if indices.get("structure_index") is not None:
    scores["schedule"] = indices["structure_index"]

  if len(scores) < 3:
    return legacy()
  weight = sum(WEIGHTS[k] for k in scores)
  value = round(cap_sleep_quality(sum(WEIGHTS[k] * v for k, v in scores.items()) / weight, tst))
  return {"score": value, "score_version": SCORE_VERSION,
          "dimension_scores": {k: round(v) for k, v in sorted(scores.items())},
          "available_dimensions": sorted(scores), "dimension_count": len(scores),
          "estimated_dimensions": sorted(estimated_dimensions),
          "duration_target_source": target_source,
          "in_bed_source": window["source"] if window else "unavailable"}


def period_score(records: list[SleepResult], profile: UserProfile, *, tz=None) -> Optional[dict]:
  scores = [daily_score(record, profile, tz=tz) for record in records]
  # Keep one score version in an aggregate; never mix historical raw and v3.
  latest = max(((r.timestamp, s["score_version"]) for r, s in zip(records, scores) if s), default=None)
  version = latest[1] if latest else None
  valid = [score["score"] for score in scores if score and score["score_version"] == version]
  if not valid:
    return None
  return {"score": round(sum(valid) / len(valid)), "score_version": version,
          "valid_nights": len(valid)}
