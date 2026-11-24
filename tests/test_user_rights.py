from datetime import datetime, timedelta

from common.user_rights import build_user_rights_payload, resolve_level_upgrade


def test_build_user_rights_payload_falls_back_to_free_when_membership_expired():
  now = datetime(2026, 4, 24, 12, 0, 0)
  payload = build_user_rights_payload("premium", now - timedelta(days=1), now=now)

  assert payload["stored_user_level"] == "premium"
  assert payload["effective_user_level"] == "free"
  assert payload["membership_active"] is False
  assert "sleep-basic-v1" in payload["rights"]["llm_models"]
  assert "sleep-expert-v3" not in payload["rights"]["llm_models"]


def test_resolve_level_upgrade_extends_same_level_from_current_end_at():
  now = datetime(2026, 4, 24, 12, 0, 0)
  current_end = now + timedelta(days=10)

  result = resolve_level_upgrade("pro", current_end, "pro", 30, now=now)

  assert result["new_user_level"] == "pro"
  assert result["action"] == "extended"
  assert result["new_level_end_at"] == current_end + timedelta(days=30)


def test_resolve_level_upgrade_starts_higher_level_immediately():
  now = datetime(2026, 4, 24, 12, 0, 0)
  current_end = now + timedelta(days=10)

  result = resolve_level_upgrade("pro", current_end, "premium", 30, now=now)

  assert result["new_user_level"] == "premium"
  assert result["action"] == "activated"
  assert result["new_level_end_at"] == now + timedelta(days=30)


def test_resolve_level_upgrade_rejects_lower_level_during_higher_membership():
  now = datetime(2026, 4, 24, 12, 0, 0)
  current_end = now + timedelta(days=10)

  try:
    resolve_level_upgrade("premium", current_end, "pro", 30, now=now)
  except ValueError as exc:
    assert "lower-tier" in str(exc)
  else:
    raise AssertionError("expected ValueError for lower-tier redemption")
