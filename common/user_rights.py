from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any


DEFAULT_USER_LEVEL = "free"

LEVEL_PRIORITIES = {
  "free": 0,
  "pro": 1,
  "premium": 2,
}

LEVEL_RIGHTS = {
  "free": {
    "llm_models": ["sleep-basic-v1"],
    "algorithms": ["sleep_stage_basic", "daily_summary_basic"],
    "analysis_depth": "basic",
    "max_reports_per_day": 3,
  },
  "pro": {
    "llm_models": ["sleep-basic-v1", "sleep-advanced-v2"],
    "algorithms": [
      "sleep_stage_basic",
      "daily_summary_basic",
      "sleep_trend_pro",
      "insight_engine_pro",
    ],
    "analysis_depth": "advanced",
    "max_reports_per_day": 20,
  },
  "premium": {
    "llm_models": ["sleep-basic-v1", "sleep-advanced-v2", "sleep-expert-v3"],
    "algorithms": [
      "sleep_stage_basic",
      "daily_summary_basic",
      "sleep_trend_pro",
      "insight_engine_pro",
      "sleep_coach_premium",
      "multi_day_correlation",
    ],
    "analysis_depth": "premium",
    "max_reports_per_day": 100,
  },
}


def normalize_user_level(level: str | None) -> str:
  normalized = (level or DEFAULT_USER_LEVEL).strip().lower()
  return normalized if normalized in LEVEL_PRIORITIES else DEFAULT_USER_LEVEL


def level_priority(level: str | None) -> int:
  return LEVEL_PRIORITIES[normalize_user_level(level)]


def get_level_rights(level: str | None) -> dict[str, Any]:
  return deepcopy(LEVEL_RIGHTS[normalize_user_level(level)])


def is_level_active(level: str | None, level_end_at: datetime | None, now: datetime | None = None) -> bool:
  normalized = normalize_user_level(level)
  if normalized == DEFAULT_USER_LEVEL:
    return True
  if level_end_at is None:
    return False
  current_time = now or datetime.now()
  return level_end_at > current_time


def get_effective_user_level(level: str | None, level_end_at: datetime | None, now: datetime | None = None) -> str:
  normalized = normalize_user_level(level)
  if is_level_active(normalized, level_end_at, now):
    return normalized
  return DEFAULT_USER_LEVEL


def build_user_rights_payload(
  user_level: str | None,
  level_end_at: datetime | None,
  now: datetime | None = None,
) -> dict[str, Any]:
  current_time = now or datetime.now()
  stored_level = normalize_user_level(user_level)
  effective_level = get_effective_user_level(stored_level, level_end_at, current_time)
  return {
    "stored_user_level": stored_level,
    "effective_user_level": effective_level,
    "level_end_at": level_end_at.isoformat() if level_end_at else None,
    "membership_active": is_level_active(stored_level, level_end_at, current_time),
    "rights": get_level_rights(effective_level),
    "server_time": current_time.isoformat(),
  }


def resolve_level_upgrade(
  current_level: str | None,
  current_end_at: datetime | None,
  redeemed_level: str | None,
  duration_days: int,
  now: datetime | None = None,
) -> dict[str, Any]:
  if duration_days <= 0:
    raise ValueError("duration_days must be greater than 0")

  current_time = now or datetime.now()
  active_level = get_effective_user_level(current_level, current_end_at, current_time)
  target_level = normalize_user_level(redeemed_level)

  if level_priority(target_level) < level_priority(active_level):
    raise ValueError("cannot redeem a lower-tier code while a higher-tier membership is active")

  if target_level == active_level and current_end_at and current_end_at > current_time:
    start_time = current_end_at
    action = "extended"
  else:
    start_time = current_time
    action = "activated" if target_level != active_level else "renewed"

  new_end_at = start_time + timedelta(days=duration_days)
  return {
    "new_user_level": target_level,
    "new_level_end_at": new_end_at,
    "action": action,
  }
