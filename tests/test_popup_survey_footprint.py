"""Tests for /popup, /survey, /companion_footprint serv logic.

Covers tanchuang_suvey.md (popup + survey) and peibanzuji.md (footprint).
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from user_profile import (
  FootprintDay, FootprintRequest, GiftDelivery, PopupRequest, SurveyAnswer,
  SurveyData, SurveySubmission, UserProfile,
)
import ops_config as ops_config_module
import user_server
from popup_config_fixture import POPUP_ID, SURVEY_ID, write_fixture_config
from user_server import UserProfileServ


@pytest.fixture(autouse=True)
def ops_config(tmp_path, monkeypatch):
  """每个测试用独立的自包含夹具配置（不依赖仓库 live 配置，运营改动不影响测试）。"""
  dst = tmp_path / "popup_survey_config.json"
  write_fixture_config(dst)
  monkeypatch.setenv("POPUP_SURVEY_CONFIG_PATH", str(dst))
  monkeypatch.setattr(
    ops_config_module, "_ops_config_cache",
    {"path": None, "mtime": None, "popups": [], "surveys": {}},
  )
  return dst


def _bump_mtime(path: Path):
  st = os.stat(path)
  os.utime(path, (st.st_atime, st.st_mtime + 5))


def _serv():
  """In-memory UserProfileServ without LevelDB / LLM, 带全局 KV（dict 后端）。"""
  serv = UserProfileServ.__new__(UserProfileServ)
  serv.lock = threading.RLock()
  store, kv = {}, {}
  serv.get_profile = lambda uid: store.get(uid)
  serv.save_profile = lambda uid, profile: store.__setitem__(uid, profile)
  serv.get_global = lambda key: kv.get(key)
  serv.put_global = lambda key, value: kv.__setitem__(key, value)
  serv.iter_global_prefix = lambda prefix: [
    (k, v) for k, v in kv.items() if k.startswith(prefix)
  ]
  return serv, store, kv


# SURVEY_ID / POPUP_ID 由 popup_config_fixture 导入（自包含夹具配置）

def _answers() -> list[SurveyAnswer]:
  return [
    SurveyAnswer(question_id="q1", type="single_choice", option_ids=["o2"]),
    SurveyAnswer(question_id="q2", type="single_choice", option_ids=["o1"]),
    SurveyAnswer(question_id="q3", type="text", text=""),
  ]


def _survey_data(**kwargs) -> SurveyData:
  payload = {
    "uid": "u1",
    "survey_id": SURVEY_ID,
    "answers": _answers(),
    "gift_delivery": GiftDelivery(
      type="physical", name="张三", phone="13800000000", region="上海市 普陀区", detail="x座1501",
    ),
  }
  payload.update(kwargs)
  return SurveyData.model_validate(payload)


# -------------------- popup --------------------

def test_query_popups_returns_catalog_and_lands_inbox_once():
  serv, store, _kv = _serv()
  popups = serv.query_popups("u1", "zh-Hans")["popups"]
  assert [p["popup_id"] for p in popups] == [POPUP_ID]
  assert popups[0]["title"] == "帮助我们做得更好"
  assert popups[0]["action_payload"] == {"survey_id": SURVEY_ID}

  inbox = store["u1"].inbox_messages
  assert len(inbox) == 1 and inbox[0].popup_id == POPUP_ID

  # 第二次拉取：站内消息按 popup_id 去重，不重复落地
  serv.query_popups("u1", "zh-Hans")
  assert len(store["u1"].inbox_messages) == 1


def test_query_popups_language_fallback():
  serv, _, _kv = _serv()
  popups = serv.query_popups("u1", "en")["popups"]
  assert popups[0]["title"] == "Help Us Improve"


def test_report_popup_event_updates_state_and_dismiss_filters_popup():
  serv, store, _kv = _serv()
  now = int(time.time())
  assert serv.report_popup_event("u1", POPUP_ID, "impression", now)
  state = store["u1"].popup_states[POPUP_ID]
  assert state.show_count == 1 and state.last_impression_at == now

  # 冷却期内（cooldown_seconds=86400）不再下发
  assert serv.query_popups("u1", "zh-Hans")["popups"] == []

  # 冷却过后可再下发；手动关闭后（dismiss_stops=true）不再下发
  assert serv.report_popup_event("u1", POPUP_ID, "dismiss", now)
  assert store["u1"].popup_states[POPUP_ID].dismissed is True
  assert serv.query_popups("u1", "zh-Hans")["popups"] == []


def test_report_popup_event_unknown_popup():
  serv, _, _kv = _serv()
  assert serv.report_popup_event("u1", "nope", "click", int(time.time())) is False


def test_survey_popup_hidden_after_survey_submitted():
  serv, store, _kv = _serv()
  store["u1"] = UserProfile(
    survey_submissions={
      SURVEY_ID: SurveySubmission(submission_id="sub_x", survey_id=SURVEY_ID, submitted_at=1),
    },
  )
  assert serv.query_popups("u1", "zh-Hans")["popups"] == []


def test_query_popups_next_query_after_from_config(ops_config):
  serv, _, _kv = _serv()
  # 仓库配置里 next_query_after=300，原样下发
  assert serv.query_popups("u1", "zh-Hans")["next_query_after"] == 300

  cfg = json.loads(ops_config.read_text(encoding="utf-8"))

  # 误配成 1 → 夹到 60s 硬下限
  cfg["next_query_after"] = 1
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)
  assert serv.query_popups("u1", "zh-Hans")["next_query_after"] == 60

  # 配得过大 → 夹到 24h 上限
  cfg["next_query_after"] = 999999
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)
  assert serv.query_popups("u1", "zh-Hans")["next_query_after"] == 86400

  # 不配置 → 不下发（客户端用默认 300s）
  cfg.pop("next_query_after")
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)
  assert serv.query_popups("u1", "zh-Hans")["next_query_after"] is None


def test_popup_route_whitelist_validation(ops_config):
  serv, _, _kv = _serv()
  cfg = json.loads(ops_config.read_text(encoding="utf-8"))
  cfg["popups"] = [
    {
      "popup_id": "pop_route_ok", "type": "mall", "placement": "home",
      "action_type": "route", "action_payload": {"route": " Store "},
      "i18n": {"zh-Hans": {"title": "t", "action_text": "go"}},
    },
    {
      "popup_id": "pop_route_bad", "type": "mall", "placement": "home",
      "action_type": "route", "action_payload": {"route": "admin_panel"},
      "i18n": {"zh-Hans": {"title": "t", "action_text": "go"}},
    },
  ]
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)

  popups = serv.query_popups("u1", "zh-Hans")["popups"]
  # 非法 route 的弹窗被丢弃；合法 route 归一化（去空格、转小写）
  assert [p["popup_id"] for p in popups] == ["pop_route_ok"]
  assert popups[0]["action_payload"]["route"] == "store"


def test_popup_request_validation():
  with pytest.raises(ValidationError):
    PopupRequest.model_validate({
      "request_type": "report_popup", "timestamp": 1,
      "data": {"uid": "u1", "event": "click"},  # 缺 popup_id
    })
  with pytest.raises(ValidationError):
    PopupRequest.model_validate({
      "request_type": "report_popup", "timestamp": 1,
      "data": {"uid": "u1", "popup_id": "p", "event": "view"},  # 非法 event
    })


# -------------------- survey --------------------

def test_get_survey_by_language():
  serv, _, _kv = _serv()
  zh = serv.get_survey(SURVEY_ID, "zh-Hans")
  en = serv.get_survey(SURVEY_ID, "en")
  assert zh["title"] == "睡眠体验问卷"
  assert en["questions"][0]["options"][0]["text"] == "Very satisfied"
  assert zh["reward"]["gift_type"] == "physical"
  assert zh["reward"]["contact_email"]  # physical 必须下发客服邮箱
  assert serv.get_survey("unknown", "en") is None


def test_submit_survey_success_and_idempotent():
  serv, store, _kv = _serv()
  data, code = serv.submit_survey("u1", _survey_data())
  assert code == 0
  assert data["reward_granted"] is True
  assert data["submission_id"].startswith("sub_")
  assert SURVEY_ID in store["u1"].survey_submissions

  # 重复提交：同一 submission_id，reward_granted=False
  data2, code2 = serv.submit_survey("u1", _survey_data())
  assert code2 == 0
  assert data2["submission_id"] == data["submission_id"]
  assert data2["reward_granted"] is False


def test_submit_survey_missing_required_answer_rejected():
  serv, _, _kv = _serv()
  bad = _survey_data(answers=[SurveyAnswer(question_id="q1", type="single_choice", option_ids=["o2"])])
  data, code = serv.submit_survey("u1", bad)
  assert data is None and code == 400


def test_submit_survey_requires_gift_delivery_for_physical_reward():
  serv, _, _kv = _serv()
  data, code = serv.submit_survey("u1", _survey_data(gift_delivery=None))
  assert data is None and code == 400


def test_submit_survey_unknown_survey():
  serv, _, _kv = _serv()
  data, code = serv.submit_survey("u1", _survey_data(survey_id="nope"))
  assert data is None and code == 404


# -------------------- 运营配置热加载 --------------------

def test_ops_config_hot_reload(ops_config):
  serv, _, _kv = _serv()
  assert serv.get_survey(SURVEY_ID, "en") is not None

  # 运营后台写入新配置（删掉问卷）→ user_server 按 mtime 检查更新
  cfg = json.loads(ops_config.read_text(encoding="utf-8"))
  cfg["surveys"].pop(SURVEY_ID)
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)
  assert serv.get_survey(SURVEY_ID, "en") is None


def test_ops_config_invalid_json_keeps_last_good(ops_config):
  serv, _, _kv = _serv()
  assert serv.get_survey(SURVEY_ID, "en") is not None

  ops_config.write_text("{not valid json", encoding="utf-8")
  _bump_mtime(ops_config)
  # 解析失败沿用上一份可用配置
  assert serv.get_survey(SURVEY_ID, "en") is not None


# -------------------- footprint --------------------

def test_merge_footprint_days_idempotent_merge_rules():
  serv, store, _kv = _serv()
  serv.merge_footprint_days("u1", [
    FootprintDay(date="2026-07-28", app_active=True, sleep_companion=False,
                 app_open_count=3, companion_minutes=10, first_active_at=200),
  ])
  serv.merge_footprint_days("u1", [
    FootprintDay(date="2026-07-28", app_active=False, sleep_companion=True,
                 app_open_count=5, companion_minutes=8, first_active_at=100),
  ])
  day = store["u1"].footprint_days["2026-07-28"]
  # 布尔取 OR、计数取大、首活跃取小
  assert day.app_active and day.sleep_companion
  assert day.app_open_count == 5 and day.companion_minutes == 10
  assert day.first_active_at == 100


def _upload(serv, uid, dates, **flags):
  serv.merge_footprint_days(uid, [
    FootprintDay(date=d, **flags) for d in dates
  ])


def test_query_footprint_month_stats_and_days():
  serv, _, _kv = _serv()
  _upload(serv, "u1", ["2026-07-01", "2026-07-07", "2026-07-23"], app_active=True)
  _upload(serv, "u1", ["2026-07-07", "2026-07-23"], sleep_companion=True)
  _upload(serv, "u1", ["2026-06-30"], app_active=True)  # 上月不计入本月

  data = serv.query_footprint("u1", "month", 2026, 7, "Asia/Shanghai")
  assert data["stats"]["app_active_month"] == 3
  assert data["stats"]["sleep_companion_month"] == 2
  assert data["stats"]["continuous_use_year"] == 4
  assert [d["day"] for d in data["days"]] == [1, 7, 23]
  assert data["anchor_date"] == "2026-07-23"
  assert data["weekday"] == 4  # 2026-07-23 是周四


def test_query_footprint_year_scope():
  serv, _, _kv = _serv()
  _upload(serv, "u1", ["2026-01-01", "2026-07-07"], app_active=True)
  data = serv.query_footprint("u1", "year", 2026, None, "UTC")
  assert len(data["months"]) == 12
  assert data["stats"]["continuous_use_year"] == 2
  jan = data["months"][0]
  assert jan["month"] == 1 and jan["days"] == [
    {"day": 1, "app_active": True, "sleep_companion": False, "plan_completed": False},
  ]


def test_footprint_milestones_completed_and_in_progress():
  serv, _, _kv = _serv()
  _upload(serv, "u1", [f"2026-07-{d:02d}" for d in range(1, 6)], plan_completed=True)  # 5 天连续
  _upload(serv, "u1", ["2026-07-20", "2026-07-21"], plan_completed=True)  # 进行中
  data = serv.query_footprint("u1", "month", 2026, 7, "UTC")
  milestones = data["milestones"]
  completed = [m for m in milestones if m["completed"]]
  in_progress = [m for m in milestones if not m["completed"]]
  assert len(completed) == 1
  assert completed[0]["date_range"] == "2026.7.1-2026.7.5"
  assert "已完成" in completed[0]["desc"]
  assert len(in_progress) == 1 and "2/5" in in_progress[0]["desc"]


def test_footprint_request_validation():
  with pytest.raises(ValidationError):
    FootprintRequest.model_validate({
      "request_type": "query_footprint", "timestamp": 1,
      "data": {"uid": "u1", "scope": "month", "year": 2026},  # 缺 month
    })
  with pytest.raises(ValidationError):
    FootprintRequest.model_validate({
      "request_type": "upload_footprint", "timestamp": 1,
      "data": {"uid": "u1", "days": []},  # days 为空
    })
