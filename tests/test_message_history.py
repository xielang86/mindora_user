"""Tests for popup_survey.md 2.1 站内消息历史恢复（query_popups scope=history）。

覆盖：
  - 配置热加载钩子 → 全局消息目录（_meta:msg:<popup_id>）upsert / offline 标记
  - query_message_history：按 ID 精确恢复（含过期/下线）、全量兜底（落地 ∩ 有效）
  - 响应字段与 query_popups popups[] 同构 + delivered_at，按 delivered_at 降序
  - PopupRequest scope / popup_ids 校验
"""
import json
import os
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from user_profile import PopupRequest
import ops_config as ops_config_module
import user_server
from engagement_service import MESSAGE_CATALOG_PREFIX, PUBLISH_LOG_PREFIX
from popup_config_fixture import POPUP_ID, fixture_popups, write_fixture_config
from user_server import UserProfileServ, UserServer

SECOND_ID = "pop_second_msg"


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


def _second_popup(**overrides) -> dict:
  """一条 valid 的 push_message 弹窗（非 survey），用于构造第二条目录消息。"""
  popup = {
    "popup_id": SECOND_ID,
    "type": "notice",
    "placement": "home",
    "priority": 50,
    "action_type": "dismiss",
    "action_payload": {},
    "image_url": "https://example.com/banner.png",
    "push_message": True,
    "i18n": {
      "zh-Hans": {"badge": "通知", "title": "系统通知", "subtitle": "第二条消息", "action_text": ""},
      "en": {"badge": "Notice", "title": "Notice", "subtitle": "second message", "action_text": ""},
    },
  }
  popup.update(overrides)
  return popup


def _config_popups() -> list:
  return fixture_popups()


def _write_popups(ops_config: Path, popups: list):
  cfg = json.loads(ops_config.read_text(encoding="utf-8"))
  cfg["popups"] = popups
  ops_config.write_text(json.dumps(cfg), encoding="utf-8")
  _bump_mtime(ops_config)


# -------------------- 消息目录（配置热加载钩子） --------------------

def test_catalog_synced_on_config_load():
  serv, _, kv = _serv()
  serv.query_popups("u1", "zh-Hans")  # 首次加载配置 → 钩子同步目录

  rec = kv.get(MESSAGE_CATALOG_PREFIX + POPUP_ID)
  assert rec is not None
  assert rec["popup_id"] == POPUP_ID and rec["type"] == "survey"
  assert rec["offline"] is False
  assert isinstance(rec["first_seen_at"], int) and rec["first_seen_at"] > 0
  assert rec["i18n"]["zh-Hans"]["title"] == "帮助我们做得更好"
  assert rec["action_payload"] == {"survey_id": "sv_sleep_2026q3"}


def test_catalog_keeps_first_seen_at_on_reload(ops_config):
  serv, _, kv = _serv()
  serv.query_popups("u1", "zh-Hans")
  first_seen = kv[MESSAGE_CATALOG_PREFIX + POPUP_ID]["first_seen_at"]

  # 内容无变化的 reload：不重写，first_seen_at 保留
  _bump_mtime(ops_config)
  serv.query_popups("u1", "zh-Hans")
  assert kv[MESSAGE_CATALOG_PREFIX + POPUP_ID]["first_seen_at"] == first_seen


def test_catalog_marks_offline_when_popup_leaves_config(ops_config):
  serv, _, kv = _serv()
  serv.query_popups("u1", "zh-Hans")

  # 运营把弹窗从配置里撤掉 → 目录标 offline（不物理删）
  _write_popups(ops_config, [])
  assert serv.query_popups("u1", "zh-Hans")["popups"] == []  # 触发 reload
  assert kv[MESSAGE_CATALOG_PREFIX + POPUP_ID]["offline"] is True

  # 按 ID 精确恢复仍能取回已下线消息的内容
  msgs = serv.query_message_history("u1", "zh-Hans", [POPUP_ID])
  assert [m["popup_id"] for m in msgs] == [POPUP_ID]
  assert msgs[0]["title"] == "帮助我们做得更好"


def test_catalog_skips_non_message_popups(ops_config):
  serv, _, kv = _serv()
  # push_message=false 且非 survey 的弹窗不进消息目录
  _write_popups(ops_config, [_second_popup(push_message=False)])
  serv.query_popups("u1", "zh-Hans")
  assert kv.get(MESSAGE_CATALOG_PREFIX + SECOND_ID) is None


# -------------------- query_message_history：按 ID 精确恢复 --------------------

def test_history_by_ids_restores_expired_and_ignores_unknown(ops_config):
  serv, _, _ = _serv()
  expired = _second_popup(popup_id="pop_expired", end_at=100)  # 早已过期
  _write_popups(ops_config, _config_popups() + [expired])
  serv.query_popups("u1", "zh-Hans")  # 同步目录（过期弹窗进不了收件箱，但进目录）

  msgs = serv.query_message_history("u1", "zh-Hans", ["pop_expired", POPUP_ID, "pop_unknown"])
  # 过期/未知 ID：过期可取回，未知静默忽略
  assert sorted(m["popup_id"] for m in msgs) == [POPUP_ID, "pop_expired"]
  expired_msg = next(m for m in msgs if m["popup_id"] == "pop_expired")
  assert expired_msg["end_at"] == 100
  # 未落地过的消息 delivered_at 退回目录 first_seen_at
  assert expired_msg["delivered_at"] is not None


# -------------------- query_message_history：全量兜底 --------------------

def test_history_full_fallback_landed_and_valid_only(ops_config):
  serv, store, kv = _serv()
  serv.query_popups("u1", "zh-Hans")  # u1 落地 POPUP_ID

  # 加第二条消息弹窗并让另一个用户触发 reload（u1 不再拉弹窗 → 不落地第二条）
  _write_popups(ops_config, _config_popups() + [_second_popup()])
  serv.query_popups("u2", "zh-Hans")  # 触发 reload + u2 落地两条
  assert kv[MESSAGE_CATALOG_PREFIX + SECOND_ID]["offline"] is False

  # u1 全量：只有落地过的 POPUP_ID；u2 全量：两条
  assert [m["popup_id"] for m in serv.query_message_history("u1", "zh-Hans", None)] == [POPUP_ID]
  assert sorted(m["popup_id"] for m in serv.query_message_history("u2", "zh-Hans", None)) == [POPUP_ID, SECOND_ID]

  # POPUP_ID 过期后：u1 全量为空，但按 ID 仍可取回
  expired_first = [dict(_config_popups()[0], end_at=100)]
  _write_popups(ops_config, expired_first + [_second_popup()])
  serv.query_popups("u3", "zh-Hans")  # 触发 reload
  assert serv.query_message_history("u1", "zh-Hans", None) == []
  assert [m["popup_id"] for m in serv.query_message_history("u1", "zh-Hans", [POPUP_ID])] == [POPUP_ID]


# -------------------- 消息结构与排序 --------------------

def test_history_message_shape_matches_query_popups():
  serv, store, _ = _serv()
  delivered = serv.query_popups("u1", "zh-Hans")["popups"][0]
  msg = serv.query_message_history("u1", "zh-Hans", [POPUP_ID])[0]

  # 与 query_popups popups[] 同构（含 delivered_at）
  assert set(msg.keys()) == set(delivered.keys())
  # delivered_at = 该 uid 首次落地时间（inbox created_at）
  landed_at = store["u1"].inbox_messages[0].created_at
  assert msg["delivered_at"] == landed_at == delivered["delivered_at"]
  assert msg["push_message"] is True
  assert msg["action_payload"] == {"survey_id": "sv_sleep_2026q3"}


def test_history_sorted_by_delivered_at_desc(ops_config):
  serv, store, _ = _serv()
  _write_popups(ops_config, _config_popups() + [_second_popup()])
  serv.query_popups("u1", "zh-Hans")  # 两条同秒落地

  # 人为拉开落地时间：SECOND 更新 → 应排前面
  for m in store["u1"].inbox_messages:
    if m.popup_id == POPUP_ID:
      m.created_at = 1000
    elif m.popup_id == SECOND_ID:
      m.created_at = 2000

  msgs = serv.query_message_history("u1", "zh-Hans", None)
  assert [m["popup_id"] for m in msgs] == [SECOND_ID, POPUP_ID]
  assert [m["delivered_at"] for m in msgs] == [2000, 1000]


# -------------------- 请求模型校验 --------------------

def test_popup_request_scope_validation():
  # 缺省 scope=active；history 合法（popup_ids 可空）
  req = PopupRequest.model_validate({
    "request_type": "query_popups", "timestamp": 1, "data": {"uid": "u1"},
  })
  assert req.data.scope == "active" and req.data.popup_ids is None

  req = PopupRequest.model_validate({
    "request_type": "query_popups", "timestamp": 1,
    "data": {"uid": "u1", "scope": "history", "popup_ids": [POPUP_ID]},
  })
  assert req.data.scope == "history" and req.data.popup_ids == [POPUP_ID]

  with pytest.raises(ValidationError):
    PopupRequest.model_validate({
      "request_type": "query_popups", "timestamp": 1,
      "data": {"uid": "u1", "scope": "bogus"},
    })


# -------------------- 消息发布审计日志（_meta:publish:*） --------------------

class TestPublishAuditLog:
  def test_record_and_list_desc(self):
    serv, _, kv = _serv()
    p1 = _second_popup(popup_id="pop_a")
    p2 = _second_popup(popup_id="pop_b")
    serv.record_publish(p1, "ops-uid", "ops@x.com")
    serv.record_publish(p2, "ops-uid", "ops@x.com")

    records = serv.list_publish_records()
    assert len(records) == 2
    # 后发布的排前面（倒序）；同秒时按 popup_id 字典序兜底
    assert {r["popup_id"] for r in records} == {"pop_a", "pop_b"}
    rec = next(r for r in records if r["popup_id"] == "pop_a")
    assert rec["operator_uid"] == "ops-uid"
    assert rec["operator_email"] == "ops@x.com"
    assert rec["popup"]["popup_id"] == "pop_a"
    assert isinstance(rec["published_at"], int) and rec["published_at"] > 0

  def test_list_limit(self):
    serv, _, _ = _serv()
    for i in range(5):
      serv.record_publish(_second_popup(popup_id=f"pop_{i}"), "ops-uid")
    assert len(serv.list_publish_records(limit=3)) == 3

  async def test_ops_push_writes_audit_log(self, monkeypatch):
    """/ops/push 发布成功 → 审计日志落 LevelDB 全局 KV。"""
    serv, _, kv = _serv()
    server = UserServer.__new__(UserServer)
    server.user_serv = serv

    async def _admin(_tok):
      return "ops-uid"

    server._check_ops_admin = _admin
    server._check_token = lambda _tok: {"uid": "ops-uid", "email": "ops@x.com"}
    monkeypatch.setattr(user_server, "append_popup", lambda popup: (True, "ok"))

    class _Req:
      async def json(self):
        return {"jwt_token": "tok", "popup": _second_popup(popup_id="pop_pub")}

    resp = await UserServer.handle_ops_push_http(server, _Req())
    assert resp.status == 200
    logs = [v for k, v in kv.items() if k.startswith(PUBLISH_LOG_PREFIX)]
    assert len(logs) == 1
    assert logs[0]["popup_id"] == "pop_pub"
    assert logs[0]["operator_email"] == "ops@x.com"

  async def test_ops_push_validation_failure_no_audit(self, monkeypatch):
    """发布失败（校验不过）不写审计。"""
    serv, _, kv = _serv()
    server = UserServer.__new__(UserServer)
    server.user_serv = serv

    async def _admin(_tok):
      return "ops-uid"

    server._check_ops_admin = _admin
    server._check_token = lambda _tok: {"uid": "ops-uid", "email": "ops@x.com"}
    monkeypatch.setattr(user_server, "append_popup", lambda popup: (False, "bad popup"))

    class _Req:
      async def json(self):
        return {"jwt_token": "tok", "popup": {"popup_id": "x"}}

    resp = await UserServer.handle_ops_push_http(server, _Req())
    assert resp.status == 400
    assert not [k for k in kv if k.startswith(PUBLISH_LOG_PREFIX)]

  async def test_ops_publish_logs_endpoint(self):
    serv, _, kv = _serv()
    serv.record_publish(_second_popup(popup_id="pop_pub"), "ops-uid", "ops@x.com")
    server = UserServer.__new__(UserServer)
    server.user_serv = serv

    async def _admin(_tok):
      return "ops-uid"

    server._check_ops_admin = _admin

    class _Req:
      async def json(self):
        return {"jwt_token": "tok"}

    resp = await UserServer.handle_ops_publish_logs_http(server, _Req())
    assert resp.status == 200
    data = json.loads(resp.text)["data"]
    assert data["total"] == 1
    assert data["records"][0]["popup_id"] == "pop_pub"

  async def test_ops_publish_logs_forbidden_for_non_admin(self):
    serv, _, _ = _serv()
    server = UserServer.__new__(UserServer)
    server.user_serv = serv

    async def _not_admin(_tok):
      return None

    server._check_ops_admin = _not_admin

    class _Req:
      async def json(self):
        return {"jwt_token": "tok"}

    resp = await UserServer.handle_ops_publish_logs_http(server, _Req())
    assert resp.status == 403


class TestOpsPopupMeta:
  """发布表单的辅助数据：survey_ids（问卷选择器）+ popup_ids（ID 查重）。"""

  def _server(self, is_admin: bool):
    serv, _, _ = _serv()
    server = UserServer.__new__(UserServer)
    server.user_serv = serv

    async def _check(_tok):
      return "ops-uid" if is_admin else None

    server._check_ops_admin = _check

    class _Req:
      async def json(self):
        return {"jwt_token": "tok"}

    return server, _Req()

  async def test_returns_survey_and_popup_ids(self):
    server, req = self._server(True)
    resp = await UserServer.handle_ops_popup_meta_http(server, req)
    assert resp.status == 200
    data = json.loads(resp.text)["data"]
    assert data["survey_ids"] == ["sv_sleep_2026q3"]
    assert data["popup_ids"] == [POPUP_ID]
    # 完整问卷内容（发布页「预览」按 survey_id 本地渲染题目流程）
    assert data["surveys"]["sv_sleep_2026q3"]["i18n"]["zh-Hans"]["questions"]

  async def test_forbidden_for_non_admin(self):
    server, req = self._server(False)
    resp = await UserServer.handle_ops_popup_meta_http(server, req)
    assert resp.status == 403
