"""Tests for 运营后台支撑逻辑：

  - engagement_service：submit_survey 写 _meta:survey: 全量记录 + list_survey_records
  - ops_config：validate_new_popup / append_popup（消息发布）
  - auth：query_ops_role / grant_ops_role 请求模型校验
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import ops_config as ops_config_module
from ops_config import append_popup, validate_new_popup
from auth import AuthRequest
from engagement_service import SURVEY_RECORD_PREFIX
from user_profile import GiftDelivery, SurveyAnswer, SurveyData
from user_server import UserProfileServ

CONFIG_SRC = Path(__file__).resolve().parent.parent / "data" / "popup_survey_config.json"
SURVEY_ID = "sv_sleep_2026q3"


@pytest.fixture(autouse=True)
def ops_config(tmp_path, monkeypatch):
  dst = tmp_path / "popup_survey_config.json"
  shutil.copy(CONFIG_SRC, dst)
  monkeypatch.setenv("POPUP_SURVEY_CONFIG_PATH", str(dst))
  monkeypatch.setattr(
    ops_config_module, "_ops_config_cache",
    {"path": None, "mtime": None, "popups": [], "surveys": {}},
  )
  return dst


def _serv():
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


def _survey_data(uid: str, submitted_at: int) -> SurveyData:
  return SurveyData.model_validate({
    "uid": uid,
    "survey_id": SURVEY_ID,
    "submitted_at": submitted_at,
    "answers": [
      {"question_id": "q1", "type": "single_choice", "option_ids": ["o2"]},
      {"question_id": "q2", "type": "single_choice", "option_ids": ["o1"]},
      {"question_id": "q3", "type": "text", "text": ""},
    ],
    "gift_delivery": {"type": "virtual", "email": "u@example.com"},
  })


# -------------------- _meta:survey: 全量记录 --------------------

def test_submit_survey_writes_meta_record():
  serv, _store, kv = _serv()
  data, code = serv.submit_survey("u1", _survey_data("u1", 1710000000), email="u1@example.com")
  assert code == 0

  key = SURVEY_RECORD_PREFIX + data["submission_id"]
  rec = kv.get(key)
  assert rec is not None
  assert rec["uid"] == "u1" and rec["survey_id"] == SURVEY_ID
  assert rec["email"] == "u1@example.com"
  assert rec["submitted_at"] == 1710000000
  assert [a["question_id"] for a in rec["answers"]] == ["q1", "q2", "q3"]
  assert rec["gift_delivery"]["type"] == "virtual"


def test_list_survey_records_sorted_and_filtered():
  serv, _store, _kv = _serv()
  serv.submit_survey("u1", _survey_data("u1", 1710000000))
  serv.submit_survey("u2", _survey_data("u2", 1710000100))

  records = serv.list_survey_records()
  assert len(records) == 2
  # 按提交时间倒序
  assert records[0]["uid"] == "u2" and records[1]["uid"] == "u1"
  # survey_id 过滤
  assert serv.list_survey_records("unknown_survey") == []
  assert len(serv.list_survey_records(SURVEY_ID)) == 2


# -------------------- append_popup 消息发布 --------------------

def _new_popup(popup_id="pop_push_01") -> dict:
  return {
    "popup_id": popup_id,
    "type": "mall",
    "placement": "home",
    "priority": 50,
    "action_type": "route",
    "action_payload": {"route": "store"},
    "push_message": True,
    "i18n": {
      "zh-Hans": {"title": "标题", "subtitle": "副标题", "action_text": "前往商城"},
    },
  }


def test_append_popup_success_and_hot_reload(ops_config):
  ok, msg = append_popup(_new_popup())
  assert ok, msg

  raw = json.loads(ops_config.read_text(encoding="utf-8"))
  assert raw["popups"][-1]["popup_id"] == "pop_push_01"

  # 热加载立即生效
  popups, _surveys, _ = ops_config_module._load_ops_config()
  assert any(p["popup_id"] == "pop_push_01" for p in popups)


def test_append_popup_rejects_duplicate_id(ops_config):
  ok, _ = append_popup(_new_popup())
  assert ok
  ok, msg = append_popup(_new_popup())
  assert not ok and "已存在" in msg


def test_validate_new_popup_cases():
  existing = set()
  assert validate_new_popup(_new_popup(), existing) is None

  bad_route = _new_popup("p2")
  bad_route["action_payload"] = {"route": "admin_panel"}
  assert "白名单" in validate_new_popup(bad_route, existing)

  no_i18n = _new_popup("p3")
  no_i18n["i18n"] = {}
  assert "i18n" in validate_new_popup(no_i18n, existing)

  missing_text = _new_popup("p4")
  missing_text["i18n"] = {"zh-Hans": {"subtitle": "x"}}
  assert "必填文案" in validate_new_popup(missing_text, existing)

  survey_no_id = _new_popup("p5")
  survey_no_id["action_type"] = "survey"
  survey_no_id["action_payload"] = {}
  assert "survey_id" in validate_new_popup(survey_no_id, existing)

  bad_window = _new_popup("p6")
  bad_window["start_at"], bad_window["end_at"] = 200, 100
  assert "start_at" in validate_new_popup(bad_window, existing)


# -------------------- auth 请求模型 --------------------

def test_auth_query_ops_role_requires_jwt():
  with pytest.raises(ValidationError):
    AuthRequest.model_validate({
      "request_type": "query_ops_role", "timestamp": int(time.time()), "data": {},
    })
  req = AuthRequest.model_validate({
    "request_type": "query_ops_role", "timestamp": int(time.time()),
    "data": {"jwt_token": "tok"},
  })
  assert req.data.jwt_token == "tok"


def test_auth_grant_ops_role_required_fields():
  with pytest.raises(ValidationError):
    AuthRequest.model_validate({
      "request_type": "grant_ops_role", "timestamp": int(time.time()),
      "data": {"jwt_token": "tok"},  # 缺 target_email / ops_role
    })
  req = AuthRequest.model_validate({
    "request_type": "grant_ops_role", "timestamp": int(time.time()),
    "data": {"jwt_token": "tok", "target_email": "ops@mindora316.com", "ops_role": "admin"},
  })
  assert req.data.ops_role == "admin"
