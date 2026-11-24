"""Premium 体验期（高级会员体验期接口.md）测试。

覆盖：
  1. common/user_rights：trial override 计算 + premium_trial_end_at 下发
  2. db/mysql_db.report_subscription：NULL 守卫幂等、留档、状态校验（mock 连接）
  3. auth.py 协议：report_subscription 必填校验、JWTTokenData 新字段
  4. auth_server.report_subscription_handler：product_id 白名单
"""
from datetime import datetime, timedelta
from unittest import mock

import pytest

from common.user_rights import (
  PREMIUM_TRIAL_DAYS,
  build_user_rights_payload,
  get_effective_user_level,
)


NOW = datetime(2026, 8, 15, 12, 0, 0)


# ── 1. user_rights 计算层 ────────────────────────────────────────────────────

class TestTrialEffectiveLevel:
  def test_trial_active_overrides_free_to_premium(self):
    trial_end = NOW + timedelta(days=3)
    assert get_effective_user_level("free", None, now=NOW, trial_end_at=trial_end) == "premium"

  def test_trial_active_overrides_pro_to_premium(self):
    trial_end = NOW + timedelta(days=3)
    assert get_effective_user_level("pro", NOW + timedelta(days=30), now=NOW, trial_end_at=trial_end) == "premium"

  def test_trial_expired_falls_back(self):
    trial_end = NOW - timedelta(seconds=1)
    assert get_effective_user_level("free", None, now=NOW, trial_end_at=trial_end) == "free"

  def test_no_trial_unchanged(self):
    assert get_effective_user_level("pro", NOW + timedelta(days=1), now=NOW) == "pro"
    assert get_effective_user_level("free", None, now=NOW) == "free"

  def test_trial_boundary_exact_end_is_expired(self):
    assert get_effective_user_level("free", None, now=NOW, trial_end_at=NOW) == "free"


class TestTrialPayload:
  def test_payload_includes_trial_end_when_active(self):
    trial_end = NOW + timedelta(days=7)
    payload = build_user_rights_payload("free", None, now=NOW, trial_end_at=trial_end)
    assert payload["effective_user_level"] == "premium"
    assert payload["premium_trial_end_at"] == trial_end.isoformat()
    assert payload["membership_active"] is True
    assert "sleep-expert-v3" in payload["rights"]["llm_models"]

  def test_payload_trial_end_none_when_expired(self):
    payload = build_user_rights_payload("free", None, now=NOW, trial_end_at=NOW - timedelta(days=1))
    assert payload["premium_trial_end_at"] is None
    assert payload["effective_user_level"] == "free"

  def test_payload_trial_end_none_when_never_granted(self):
    payload = build_user_rights_payload("pro", NOW + timedelta(days=10), now=NOW)
    assert payload["premium_trial_end_at"] is None
    assert payload["effective_user_level"] == "pro"

  def test_trial_overrides_expired_stored_membership(self):
    """stored premium 已过期但体验期仍有效 → premium（体验期优先级最高）。"""
    payload = build_user_rights_payload(
      "premium", NOW - timedelta(days=1), now=NOW, trial_end_at=NOW + timedelta(days=2),
    )
    assert payload["effective_user_level"] == "premium"


class TestMembershipInfo:
  def test_returns_all_membership_source_fields_and_effective_rights(self, monkeypatch):
    import db.mysql_db as m

    membership_now = datetime.now()
    signup_end = membership_now + timedelta(days=2)
    purchase_end = membership_now + timedelta(days=7)
    row = {
      "uid": "u1",
      "status": 1,
      "user_level": "pro",
      "level_end_at": membership_now + timedelta(days=30),
      "signup_trial_end_at": signup_end,
      "basic_purchase_trial_end_at": purchase_end,
    }
    queries = []
    monkeypatch.setattr(
      m.mysql_db,
      "query_one",
      lambda sql, params=(): queries.append((sql, params)) or row,
    )
    monkeypatch.setattr(m, "_safe_active_subscription_tier", lambda uid: None)

    result = m.get_user_membership_info("u1")

    assert result["uid"] == "u1"
    assert result["user_level"] == "pro"
    assert result["signup_trial_end_at"] == signup_end.isoformat()
    assert result["basic_purchase_trial_end_at"] == purchase_end.isoformat()
    assert result["premium_trial_end_at"] == purchase_end.isoformat()
    assert result["effective_user_level"] == "premium"
    assert "password_hash" not in queries[0][0]
    assert "device_list" not in queries[0][0]


# ── 2. db/mysql_db.report_subscription（mock 连接游标）────────────────────────

class _FakeCursor:
  """按 SQL 关键字分发的最小游标替身。user_row 由测试注入。"""

  def __init__(self, user_row):
    self.user_row = dict(user_row) if user_row else None
    self.executed = []
    self.rowcount = 0

  def __enter__(self):
    return self

  def __exit__(self, *a):
    return False

  def execute(self, sql, params=()):
    self.executed.append((sql.strip(), params))
    if sql.strip().startswith("UPDATE user_auth"):
      # 模拟 NULL 守卫：仅当 basic_purchase_trial_end_at 为 NULL 时写入生效
      if self.user_row is not None and self.user_row.get("basic_purchase_trial_end_at") is None:
        self.user_row["basic_purchase_trial_end_at"] = params[0]
        self.rowcount = 1
      else:
        self.rowcount = 0
    else:
      self.rowcount = 1

  def fetchone(self):
    return self.user_row


class _FakeConn:
  def __init__(self, user_row):
    self.cursor_obj = _FakeCursor(user_row)
    self.committed = False
    self.rolled_back = False

  def begin(self):
    pass

  def cursor(self, *a, **kw):
    return self.cursor_obj

  def commit(self):
    self.committed = True

  def rollback(self):
    self.rolled_back = True

  def close(self):
    pass


def _patch_conn(monkeypatch, user_row):
  import db.mysql_db as m
  conn = _FakeConn(user_row)
  monkeypatch.setattr(m.mysql_db, "get_connection", lambda: conn)
  return conn


def _active_user(**overrides):
  row = {
    "uid": "u1", "status": 1, "user_level": "free", "level_end_at": None,
    "signup_trial_end_at": None, "basic_purchase_trial_end_at": None,
  }
  row.update(overrides)
  return row


class TestReportSubscription:
  def test_first_report_grants_trial(self, monkeypatch):
    import db.mysql_db as m
    conn = _patch_conn(monkeypatch, _active_user())

    result = m.report_subscription("u1", "ios", "com.mindora316.monthly", "txn-1", NOW)

    assert result["code"] == 0
    assert result["data"]["trial_granted"] is True
    assert result["data"]["effective_user_level"] == "premium"
    assert result["data"]["premium_trial_end_at"] is not None
    assert conn.committed
    # 留档 INSERT 已执行
    assert any("INSERT INTO subscription_reports" in sql for sql, _ in conn.cursor_obj.executed)

  def test_second_report_is_idempotent(self, monkeypatch):
    import db.mysql_db as m
    existing_end = NOW + timedelta(days=5)
    conn = _patch_conn(monkeypatch, _active_user(basic_purchase_trial_end_at=existing_end))

    result = m.report_subscription("u1", "ios", "com.mindora316.monthly", "txn-1", NOW)

    assert result["code"] == 0
    assert result["data"]["trial_granted"] is False
    # 既有盖章不被覆盖
    assert conn.cursor_obj.user_row["basic_purchase_trial_end_at"] == existing_end
    # 重复上报仍留档（对账）
    assert any("INSERT INTO subscription_reports" in sql for sql, _ in conn.cursor_obj.executed)

  def test_trial_end_is_max_of_two_columns(self, monkeypatch):
    import db.mysql_db as m
    signup_end = NOW + timedelta(days=6)  # 第①段还没跑完
    _patch_conn(monkeypatch, _active_user(signup_trial_end_at=signup_end))

    result = m.report_subscription("u1", "ios", "com.mindora316.yearly", "txn-2", NOW)

    # 第②段从自己的触发时刻起算 7 天，不与第①段衔接 → max = now+7d > signup_end
    expected = result["data"]["premium_trial_end_at"]
    assert expected > signup_end.isoformat()

  def test_user_not_found(self, monkeypatch):
    import db.mysql_db as m
    _patch_conn(monkeypatch, None)
    result = m.report_subscription("ghost", "ios", "com.mindora316.monthly", "txn-1", NOW)
    assert result["code"] == 404

  def test_inactive_user_rejected(self, monkeypatch):
    import db.mysql_db as m
    conn = _patch_conn(monkeypatch, _active_user(status=0))
    result = m.report_subscription("u1", "ios", "com.mindora316.monthly", "txn-1", NOW)
    assert result["code"] == 403
    assert conn.rolled_back

  def test_missing_params_rejected(self):
    import db.mysql_db as m
    assert m.report_subscription("", "ios", "p", "t", NOW)["code"] == 400
    assert m.report_subscription("u1", "ios", "", "t", NOW)["code"] == 400
    assert m.report_subscription("u1", "ios", "p", "", NOW)["code"] == 400

  def test_grant_trial_false_archives_without_stamping(self, monkeypatch):
    """§5.1③：Premium 档上报也要留档对账，但不盖第②段的章。"""
    import db.mysql_db as m
    conn = _patch_conn(monkeypatch, _active_user())

    result = m.report_subscription(
      "u1", "ios", "com.mindora316.premium.monthly", "txn-9", NOW,
      grant_trial=False,
    )

    assert result["code"] == 0
    assert result["data"]["trial_granted"] is False
    # 没有执行盖章 UPDATE
    assert not any("basic_purchase_trial_end_at=%s" in sql for sql, _ in conn.cursor_obj.executed)
    assert conn.cursor_obj.user_row["basic_purchase_trial_end_at"] is None
    # 但留档 INSERT 照常
    assert any("INSERT INTO subscription_reports" in sql for sql, _ in conn.cursor_obj.executed)
    assert conn.committed


# ── 3. auth.py 协议层 ────────────────────────────────────────────────────────

class TestReportSubscriptionProtocol:
  def _req(self, **data):
    from auth import AuthRequest
    base = {
      "jwt_token": "tok",
      "platform": "ios",
      "product_id": "com.mindora316.monthly",
      "original_transaction_id": "txn-1",
      "purchased_at": 1710000000,
    }
    base.update(data)
    return {
      "request_type": "report_subscription",
      "version": "1.0",
      "timestamp": int(datetime.now().timestamp()),
      "data": base,
    }

  def test_valid_request_parses(self):
    from auth import AuthRequest, AuthRequestType
    req = AuthRequest(**self._req())
    assert req.request_type == AuthRequestType.REPORT_SUBSCRIPTION
    assert req.data.product_id == "com.mindora316.monthly"

  @pytest.mark.parametrize("missing", ["jwt_token", "platform", "product_id", "original_transaction_id", "purchased_at"])
  def test_missing_required_field_rejected(self, missing):
    from auth import AuthRequest
    from pydantic import ValidationError
    body = self._req()
    body["data"].pop(missing)
    with pytest.raises(ValidationError):
      AuthRequest(**body)

  def test_blank_product_id_rejected(self):
    from auth import AuthRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
      AuthRequest(**self._req(product_id="  "))

  def test_jwt_token_data_carries_trial_field(self):
    from auth import JWTTokenData
    data = JWTTokenData(
      uid="u1", email="u@example.com", token="tok", expire_days=7,
      user_level="free", effective_user_level="premium",
      level_end_at=None, premium_trial_end_at=NOW,
    )
    dumped = data.model_dump(mode="json")
    assert dumped["premium_trial_end_at"] is not None
    assert dumped["effective_user_level"] == "premium"


# ── 4. auth_server handler：product_id 白名单 ───────────────────────────────

class TestReportSubscriptionHandler:
  def _data(self, product_id="com.mindora316.monthly"):
    from auth import AuthData
    return AuthData(
      jwt_token="tok", platform="ios", product_id=product_id,
      original_transaction_id="txn-1", purchased_at=1710000000,
    )

  def test_basic_product_grants(self, monkeypatch):
    import auth_server as srv
    monkeypatch.setattr(srv, "decode_access_token", lambda t: {"uid": "u1"})
    called = {}

    def fake_report(**kw):
      called.update(kw)
      return {"code": 0, "msg": "premium trial granted",
              "data": {"effective_user_level": "premium", "trial_granted": True}}

    monkeypatch.setattr(srv, "report_subscription", fake_report)
    resp = srv.report_subscription_handler(self._data())
    assert resp.code == 0
    assert called["uid"] == "u1"
    assert called["product_id"] == "com.mindora316.monthly"
    assert resp.data["trial_granted"] is True

  @pytest.mark.parametrize("pid", ["com.mindora316.premium.monthly", "com.mindora316.premium.yearly", "com.other.app"])
  def test_non_basic_product_archived_without_trial(self, monkeypatch, pid):
    """§5.1③：Premium/未知档也上报、也归档（对账用），但不盖章——grant_trial=False 传给 db。"""
    import auth_server as srv
    monkeypatch.setattr(srv, "decode_access_token", lambda t: {"uid": "u1"})
    called = {}

    def fake_report(**kw):
      called.update(kw)
      return {"code": 0, "msg": "product not eligible for premium trial (report archived)",
              "data": {"effective_user_level": "premium", "trial_granted": False}}

    monkeypatch.setattr(srv, "report_subscription", fake_report)
    resp = srv.report_subscription_handler(self._data(product_id=pid))

    assert resp.code == 0
    assert called["grant_trial"] is False       # 归档但不盖章
    assert called["product_id"] == pid
    assert resp.data["effective_user_level"] == "premium"

  def test_basic_product_passes_grant_trial_true(self, monkeypatch):
    import auth_server as srv
    monkeypatch.setattr(srv, "decode_access_token", lambda t: {"uid": "u1"})
    called = {}

    def fake_report(**kw):
      called.update(kw)
      return {"code": 0, "msg": "ok", "data": {"trial_granted": True}}

    monkeypatch.setattr(srv, "report_subscription", fake_report)
    srv.report_subscription_handler(self._data())
    assert called["grant_trial"] is True

  @pytest.mark.parametrize("code", [400, 403, 404])
  def test_business_error_returns_200_with_code(self, monkeypatch, code):
    """§5.1②：参数/业务错误绝不回 4xx（客户端出队不再重试 = 上报永久丢失），
    回 HTTP 200 语义的 AuthResponse + code != 0。"""
    import auth_server as srv
    monkeypatch.setattr(srv, "decode_access_token", lambda t: {"uid": "u1"})
    monkeypatch.setattr(srv, "report_subscription",
                        lambda **kw: {"code": code, "msg": "bad", "data": None})
    resp = srv.report_subscription_handler(self._data())  # 不抛 HTTPException
    assert resp.code == code
    assert resp.data is None

  def test_db_failure_raises_http(self, monkeypatch):
    """5xx 内部错误仍回 HTTP 500：客户端保留队列重试（§5.1②）。"""
    import auth_server as srv
    from fastapi import HTTPException
    monkeypatch.setattr(srv, "decode_access_token", lambda t: {"uid": "u1"})
    monkeypatch.setattr(srv, "report_subscription",
                        lambda **kw: {"code": 500, "msg": "db down", "data": None})
    with pytest.raises(HTTPException):
      srv.report_subscription_handler(self._data())


class TestReportSubscriptionHttpLayer:
  """§5.1② 端到端：FastAPI 参数校验失败（缺字段）默认 422 → 客户端永久丢单；
  report_subscription 必须回 HTTP 200 + code=400。其他 request_type 维持 422。"""

  def _post(self, body):
    import auth_server as srv
    from fastapi.testclient import TestClient
    client = TestClient(srv.app, raise_server_exceptions=False)
    return client.post("/auth", json=body)

  def test_report_subscription_missing_field_returns_200_code400(self):
    body = {
      "request_type": "report_subscription",
      "version": "1.0",
      "timestamp": 1710000000,
      "data": {  # 缺 product_id / purchased_at
        "jwt_token": "tok", "platform": "ios",
        "original_transaction_id": "txn-1",
      },
    }
    resp = self._post(body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 400
    assert payload["request_type"] == "report_subscription"

  def test_other_request_type_keeps_422(self):
    body = {
      "request_type": "send_verify_code",  # 缺 email/device_id
      "version": "1.0",
      "timestamp": 1710000000,
      "data": {},
    }
    resp = self._post(body)
    assert resp.status_code == 422
