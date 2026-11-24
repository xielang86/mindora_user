"""Apple 审核专用账号（AUTH_REVIEW_CODES）测试。

覆盖：
  1. env 解析：_load_review_codes / _review_fixed_code（含 email 归一化）
  2. send_verify_code_handler：审核账号不发邮件直接成功；普通账号走原流程
  3. auth_by_verify_code：固定码登录成功/错误码 401/不消费码（可反复登录）
  4. 注销流程：send_delete_verify_code_handler 不发邮件；
     delete_user_with_code_handler 固定码校验，注销后账号可被恢复（登录侧逻辑）
"""
import uuid

import pytest
from fastapi import HTTPException

REVIEW_EMAIL = "appreview@mindora316.com"
REVIEW_CODE = "5317"  # 4 位，与 App 验证码输入框和普通验证码（generate_verify_code(4)）一致


@pytest.fixture()
def srv(monkeypatch):
  """导入 auth_server 并注入审核账号配置。"""
  import auth_server as s
  monkeypatch.setattr(s.Config, "Mode", 0)  # 走生产分支（审核打的是正式服）
  monkeypatch.setitem(s.AUTH_REVIEW_CODES, REVIEW_EMAIL, REVIEW_CODE)
  yield s
  s.AUTH_REVIEW_CODES.pop(REVIEW_EMAIL, None)


def _auth_data(srv, email=REVIEW_EMAIL, code=REVIEW_CODE):
  from auth import AuthData
  return AuthData(email=email, device_id=uuid.uuid4(), verify_code=code)


# ── 1. env 解析 ──────────────────────────────────────────────────────────────

class TestReviewCodeConfig:
  def test_load_parses_multi_accounts(self, monkeypatch):
    import auth_server as s
    monkeypatch.setenv("AUTH_REVIEW_CODES",
                       f"  {REVIEW_EMAIL}:{REVIEW_CODE} , Foo@Bar.com:1234 ,bad-entry, :999 ")
    s.AUTH_REVIEW_CODES.clear()
    try:
      s._load_review_codes()
      assert s.AUTH_REVIEW_CODES == {REVIEW_EMAIL: REVIEW_CODE, "foo@bar.com": "1234"}
    finally:
      s.AUTH_REVIEW_CODES.clear()
      s._load_review_codes()  # 还原（env 已被 monkeypatch 恢复为空）

  def test_fixed_code_lookup_normalizes_email(self, srv):
    assert srv._review_fixed_code("  AppReview@Mindora316.com ") == REVIEW_CODE
    assert srv._review_fixed_code("other@x.com") is None
    assert srv._review_fixed_code(None) is None


# ── 2. 发送登录验证码 ────────────────────────────────────────────────────────

class TestSendVerifyCode:
  def test_review_account_skips_email(self, srv, monkeypatch):
    monkeypatch.setattr(srv, "send_verify_code_via_163",
                        lambda *a, **k: pytest.fail("审核账号不应发邮件"))
    monkeypatch.setattr(srv, "set_verify_code",
                        lambda *a, **k: pytest.fail("审核账号不应写 Redis 验证码"))
    resp = srv.send_verify_code_handler(_auth_data(srv))
    assert resp.code == 0

  def test_normal_account_untouched(self, srv, monkeypatch):
    """普通账号仍走 Redis + 邮件通道。"""
    calls = {}
    monkeypatch.setattr(srv, "set_verify_code", lambda **kw: calls.setdefault("redis", kw))
    monkeypatch.setattr(srv, "send_verify_code_via_163",
                        lambda *a, **k: {"code": 0, "msg": "ok"})
    resp = srv.send_verify_code_handler(_auth_data(srv, email="real@user.com"))
    assert resp.code == 0 and "redis" in calls


# ── 3. 固定码登录 ────────────────────────────────────────────────────────────

class TestReviewLogin:
  def _mock_user_db(self, srv, monkeypatch):
    """已有活跃用户，避免触碰 MySQL；签名为假 token。"""
    user = type("U", (), {"uid": "review-uid", "email": REVIEW_EMAIL,
                          "salt": "s", "status": 1})()
    monkeypatch.setattr(srv, "get_user_by_email_or_uid", lambda **kw: user)
    monkeypatch.setattr(srv, "sign_token", lambda payload: "fake-jwt")

  def test_fixed_code_login_ok(self, srv, monkeypatch):
    self._mock_user_db(srv, monkeypatch)
    monkeypatch.setattr(srv, "get_verify_code",
                        lambda *a, **k: pytest.fail("审核账号不应读 Redis 验证码"))
    resp = srv.auth_by_verify_code(_auth_data(srv))
    assert resp.code == 0
    assert resp.data.uid == "review-uid"
    assert resp.data.token == "fake-jwt"

  def test_fixed_code_reusable_across_logins(self, srv, monkeypatch):
    """固定码不消费：logout 后同一码可再次登录（调两次均成功）。"""
    self._mock_user_db(srv, monkeypatch)
    for _ in range(2):
      resp = srv.auth_by_verify_code(_auth_data(srv))
      assert resp.code == 0

  def test_wrong_code_rejected(self, srv, monkeypatch):
    self._mock_user_db(srv, monkeypatch)
    with pytest.raises(HTTPException) as ei:
      srv.auth_by_verify_code(_auth_data(srv, code="000000"))
    assert ei.value.status_code == 401


# ── 4. 注销账号流程 ──────────────────────────────────────────────────────────

class TestReviewDeleteAccount:
  def _jwt(self, monkeypatch, srv):
    monkeypatch.setattr(srv, "decode_access_token",
                        lambda t: {"uid": "review-uid", "email": REVIEW_EMAIL})

  def test_send_delete_code_skips_email(self, srv, monkeypatch):
    self._jwt(monkeypatch, srv)
    monkeypatch.setattr(srv, "send_verify_code_via_163",
                        lambda *a, **k: pytest.fail("审核账号不应发邮件"))
    from auth import AuthData
    resp = srv.send_delete_verify_code_handler(AuthData(jwt_token="tok"))
    assert resp.code == 0

  def test_delete_with_fixed_code(self, srv, monkeypatch):
    self._jwt(monkeypatch, srv)
    deleted = {}

    def fake_soft_delete(uid):
      deleted["uid"] = uid
      return {"code": 0}

    monkeypatch.setattr(srv, "soft_delete_user", fake_soft_delete)
    from auth import AuthData
    resp = srv.delete_user_with_code_handler(
        AuthData(jwt_token="tok", verify_code=REVIEW_CODE))
    assert resp.code == 0
    assert deleted["uid"] == "review-uid"

  def test_delete_with_wrong_code_rejected(self, srv, monkeypatch):
    self._jwt(monkeypatch, srv)
    from auth import AuthData
    with pytest.raises(HTTPException) as ei:
      srv.delete_user_with_code_handler(AuthData(jwt_token="tok", verify_code="000000"))
    assert ei.value.status_code == 401

  def test_deleted_review_account_restored_on_next_login(self, srv, monkeypatch):
    """注销（status=0）后再次用固定码登录 → 走恢复分支，账号复活。"""
    user = type("U", (), {"uid": "review-uid", "email": REVIEW_EMAIL,
                          "salt": "s", "status": 0})()
    monkeypatch.setattr(srv, "get_user_by_email_or_uid", lambda **kw: user)
    monkeypatch.setattr(srv, "insert_or_restore_user",
                        lambda *a, **k: {"code": 0, "msg": "restored"})
    monkeypatch.setattr(srv, "sign_token", lambda payload: "fake-jwt")
    resp = srv.auth_by_verify_code(_auth_data(srv))
    assert resp.code == 0
    assert resp.data.uid == "review-uid"
