"""Persist the versioned nightly scores exposed by query_profile and /analysis."""
from __future__ import annotations

from analysis_score import SCORE_VERSION, INDEX_VERSION, daily_score
from insight_rules import compute_night_indices

UNAVAILABLE_SCORE_VERSION = "mindora-score-unavailable-v1"


def score_cache_ready(profile) -> bool:
  return all(row.score_version in {SCORE_VERSION, UNAVAILABLE_SCORE_VERSION}
             and row.index_version == INDEX_VERSION
             and (row.score_version != SCORE_VERSION or
                  (row.score_details is not None and row.score_details.get("score") == row.sleep_quality))
             for row in profile.sleep_data or [])


def refresh_score_cache(profile) -> bool:
  """Recompute every retained night; bounded to UserProfileServ.MAX_SLEEP_DATA_LEN."""
  changed = False
  for row in profile.sleep_data or []:
    indices = compute_night_indices(profile, row) if row.sleep_status else {}
    scored = daily_score(row, profile, insight_indices=indices) if row.sleep_status else None
    if scored and scored["score_version"] != SCORE_VERSION:
      scored = None
    updates = {
      "sleep_quality": scored["score"] if scored else None,
      "score_version": SCORE_VERSION if scored else UNAVAILABLE_SCORE_VERSION,
      "score_details": scored,
      "soe": indices.get("onset_index"),
      "sleep_arch_index": indices.get("structure_index"),
      "night_var_index": indices.get("stability_index"),
      "index_version": INDEX_VERSION,
    }
    for field, value in updates.items():
      if getattr(row, field) != value:
        setattr(row, field, value)
        changed = True
  return changed


def cached_daily_score(row, profile, *, tz=None, insight_indices=None):
  """Use a complete current cache, falling back to live calculation for old rows."""
  if row.score_version == SCORE_VERSION and row.score_details:
    return row.score_details
  if row.score_version == UNAVAILABLE_SCORE_VERSION:
    return None
  return daily_score(row, profile, tz=tz, insight_indices=insight_indices)


def cached_night_indices(row, profile):
  if row.index_version == INDEX_VERSION:
    return {"onset_index": row.soe, "structure_index": row.sleep_arch_index,
            "stability_index": row.night_var_index}
  return compute_night_indices(profile, row)


def cached_period_score(records, profile, *, tz=None):
  pairs = [(row.timestamp, cached_daily_score(row, profile, tz=tz)) for row in records]
  latest = max(((ts, score["score_version"]) for ts, score in pairs if score), default=None)
  if latest is None:
    return None
  version = latest[1]
  scores = [score["score"] for _, score in pairs if score and score["score_version"] == version]
  return {"score": round(sum(scores) / len(scores)), "score_version": version,
          "valid_nights": len(scores)}
