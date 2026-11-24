"""画像体积兜底：头像上限、SleepResult 内嵌序列截断、新建画像字段白名单。

三条都是「防御异常/恶意客户端」的兜底，不是业务校验：正常请求永远碰不到。
"""
import threading

import pytest

from user_profile import (
  AnalysisTextReport, FootprintDay, InboxMessage, PopupState, Profile,
  SleepElement, SleepResult, SurveySubmission, SyncedSleepPlan, UserProfile,
)
from profile_service import UserProfileServ


def _serv() -> UserProfileServ:
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  return serv


class TestAvatarLimit:
  def test_oversize_avatar_keeps_previous(self, caplog):
    old = Profile(nickname="old", avatar_base64="small")
    huge = "A" * (UserProfileServ.MAX_AVATAR_BASE64_LEN + 1)
    new = Profile(nickname="new", avatar_base64=huge)
    with caplog.at_level("WARNING"):
      merged = UserProfileServ._merge_personal_profile(old, new)
    assert merged.avatar_base64 == "small"
    # 其余个人资料字段照常合并，不因头像超限整体失败
    assert merged.nickname == "new"
    assert "avatar_base64 rejected" in caplog.text

  def test_avatar_at_limit_accepted(self):
    ok = "A" * UserProfileServ.MAX_AVATAR_BASE64_LEN
    merged = UserProfileServ._merge_personal_profile(
      Profile(avatar_base64="small"), Profile(avatar_base64=ok))
    assert merged.avatar_base64 == ok

  def test_new_profile_oversize_avatar_left_empty(self):
    huge = "A" * (UserProfileServ.MAX_AVATAR_BASE64_LEN + 1)
    merged = UserProfileServ._merge_personal_profile(None, Profile(avatar_base64=huge))
    assert merged.avatar_base64 == ""


class TestSleepRecordListCaps:
  def _record(self, stages: int) -> SleepResult:
    return SleepResult(
      timestamp=1000,
      sleep_status=[
        SleepElement(start_time=i, duration=1.0, sleep_type="core") for i in range(stages)
      ],
    )

  def test_truncates_oversize_sleep_status_keeping_head(self, caplog):
    cap = UserProfileServ.MAX_SLEEP_RECORD_LIST_LEN["sleep_status"]
    profile = UserProfile(sleep_data=[self._record(cap + 10)])
    with caplog.at_level("WARNING"):
      UserProfileServ._cap_sleep_record_lists(profile)
    kept = profile.sleep_data[0].sleep_status
    assert len(kept) == cap
    # 保留开头（入睡段），不是结尾
    assert kept[0].start_time == 0
    assert "truncated" in caplog.text

  def test_normal_night_untouched(self):
    profile = UserProfile(sleep_data=[self._record(30)])
    UserProfileServ._cap_sleep_record_lists(profile)
    assert len(profile.sleep_data[0].sleep_status) == 30

  def test_self_rating_tags_capped(self):
    cap = UserProfileServ.MAX_SLEEP_RECORD_LIST_LEN["self_rating_tags"]
    record = SleepResult(timestamp=1, self_rating_tags=[f"t{i}" for i in range(cap + 5)])
    profile = UserProfile(sleep_data=[record])
    UserProfileServ._cap_sleep_record_lists(profile)
    assert len(profile.sleep_data[0].self_rating_tags) == cap

  def test_save_profile_applies_caps(self, monkeypatch):
    cap = UserProfileServ.MAX_SLEEP_RECORD_LIST_LEN["sleep_status"]
    serv = _serv()
    serv.storage_mode = "txt_json"
    serv.text_profiles = {}
    monkeypatch.setattr(serv, "_flush_text_profiles_unlocked", lambda: None)
    profile = UserProfile(sleep_data=[self._record(cap + 1)])
    serv.save_profile("u1", profile)
    assert len(profile.sleep_data[0].sleep_status) == cap


class TestNewProfileWhitelist:
  def test_server_owned_fields_dropped(self, caplog):
    hostile = UserProfile(
      uid_emb=[0.1],
      behaviors={"plays": [(1, {"cmd": "sleep.scene.kyoto_forest", "event": "sop_start"})]},
      sleep_data=[SleepResult(timestamp=10, sleep_quality=80.0)],
      profile=Profile(nickname="me"),
      # 以下全是服务端独占，客户端首包不应该能写
      revision=999,
      mindora_record={"sleep.scene.kyoto_forest": [(1, 60)] * 5},
      inbox_messages=[InboxMessage(message_id="m", popup_id="p", title="t", created_at=1)],
      sleep_plans=[SyncedSleepPlan(plan_id="P1", updated_at=1)],
      analysis_reports={"analysis_sleep_day": [AnalysisTextReport(
        request_type="analysis_sleep_day", date="2026-09-23", generated_at=1)]},
      sop_tag_profile={"injected": True},
      health_sync_days={"2026-09-23": 99},
    )
    with caplog.at_level("WARNING"):
      clean = UserProfileServ._new_profile_from_client(hostile)

    assert clean.uid_emb == [0.1]
    assert clean.behaviors["plays"]
    assert clean.sleep_data[0].timestamp == 10
    assert clean.profile.nickname == "me"

    assert clean.revision == 0
    assert clean.inbox_messages == []
    assert clean.sleep_plans == []
    assert clean.sop_tag_profile == {}
    assert clean.health_sync_days == {}
    assert clean.analysis_reports == {k: [] for k in clean.analysis_reports}
    assert all(not v for v in clean.mindora_record.values())
    assert "dropped server-owned fields" in caplog.text

  def test_clean_payload_logs_nothing(self, caplog):
    with caplog.at_level("WARNING"):
      clean = UserProfileServ._new_profile_from_client(
        UserProfile(behaviors={"plays": []}, sleep_data=[]))
    assert "dropped server-owned fields" not in caplog.text
    assert clean.revision == 0

  def test_first_update_does_not_persist_injected_plans(self, monkeypatch):
    """端到端：首次 update_profile 不该把客户端塞的 sleep_plans 落库（绕过额度校验）。"""
    serv = _serv()
    saved = {}
    monkeypatch.setattr(serv, "get_profile", lambda uid: None)
    monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update(profile=profile))
    hostile = UserProfile(
      behaviors={"plays": []},
      sleep_plans=[SyncedSleepPlan(plan_id="P1", updated_at=1)],
    )
    assert serv.update_profile_basic("new_uid", hostile)
    assert saved["profile"].sleep_plans == []


class TestUidEmbLimit:
  def test_oversize_uid_emb_rejected(self, caplog):
    cap = UserProfileServ.MAX_UID_EMB_LEN
    with caplog.at_level("WARNING"):
      assert UserProfileServ._accept_uid_emb([0.1] * (cap + 1)) is False
    assert "uid_emb rejected" in caplog.text
    assert UserProfileServ._accept_uid_emb([0.1] * cap) is True

  def test_merge_keeps_previous_uid_emb(self, monkeypatch):
    serv = _serv()
    existing = UserProfile(uid_emb=[0.5] * 32, behaviors={"plays": []})
    saved = {}
    monkeypatch.setattr(serv, "get_profile", lambda uid: existing.model_copy(deep=True))
    monkeypatch.setattr(serv, "save_profile", lambda uid, profile: saved.update(profile=profile))
    huge = UserProfile(uid_emb=[0.9] * (UserProfileServ.MAX_UID_EMB_LEN + 1))
    assert serv.update_profile_basic("u1", huge)
    assert saved["profile"].uid_emb == [0.5] * 32

  def test_new_profile_drops_oversize_uid_emb(self):
    clean = UserProfileServ._new_profile_from_client(
      UserProfile(uid_emb=[0.9] * (UserProfileServ.MAX_UID_EMB_LEN + 1)))
    assert clean.uid_emb == []


class TestEngagementStorageCap:
  def _profile(self, n: int) -> UserProfile:
    return UserProfile(
      footprint_days={
        f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": FootprintDay(
          date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", app_active=True)
        for i in range(n)
      },
      inbox_messages=[
        InboxMessage(message_id=f"m{i}", popup_id=f"p{i}", title="t", created_at=i)
        for i in range(n)
      ],
      popup_states={f"p{i}": PopupState(show_count=1, last_impression_at=i) for i in range(n)},
      survey_submissions={
        f"s{i}": SurveySubmission(submission_id=f"sub{i}", survey_id=f"s{i}", submitted_at=i)
        for i in range(n)
      },
    )

  def test_caps_to_limit_keeping_newest(self, caplog):
    cap = UserProfileServ.MAX_ENGAGEMENT_LEN
    profile = self._profile(cap + 20)
    with caplog.at_level("WARNING"):
      UserProfileServ._cap_engagement_containers(profile)
    assert len(profile.footprint_days) == cap
    assert len(profile.inbox_messages) == cap
    assert len(profile.popup_states) == cap
    assert len(profile.survey_submissions) == cap
    # 挤掉的是最旧的：最新一条必须还在
    assert profile.inbox_messages[-1].message_id == f"m{cap + 19}"
    assert f"s{cap + 19}" in profile.survey_submissions
    assert "m0" not in [m.message_id for m in profile.inbox_messages]
    assert "truncated" in caplog.text

  def test_under_limit_untouched(self):
    profile = self._profile(10)
    UserProfileServ._cap_engagement_containers(profile)
    assert len(profile.footprint_days) == 10
    assert len(profile.inbox_messages) == 10

  def test_save_profile_applies_caps(self, monkeypatch):
    cap = UserProfileServ.MAX_ENGAGEMENT_LEN
    serv = _serv()
    serv.storage_mode = "txt_json"
    serv.text_profiles = {}
    monkeypatch.setattr(serv, "_flush_text_profiles_unlocked", lambda: None)
    profile = self._profile(cap + 5)
    serv.save_profile("u1", profile)
    assert len(profile.popup_states) == cap
