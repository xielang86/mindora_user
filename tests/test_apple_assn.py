"""ASSN V2（App Store Server Notifications）测试。

覆盖：
  1. appAccountToken 派生：官方测试向量（ASSN 文档 §5.1）+ 小写归一
  2. _peek_environment：不验签读 environment 挑 verifier（data/summary/缺省）
  3. 应答语义（§4.3）：RETRYABLE → 500；伪签/畸形/重复/未知类型 → 200；DB 错误 → 500
  4. 事件状态机（§6.1）：SUBSCRIBED 盖章与幂等、DID_RENEW、升降档、退款收回/恢复、
     宽限期、EXPIRED、TEST/无载荷类型不落订阅表
  5. get_active_subscription_tier：环境隔离、宽限期有效、revoked 无效、多订阅取高档
  6. effective_user_level 合并：订阅档与存储档（兑换码）取 MAX，体验期最高优先
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import apple_assn
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus
from common.apple_binding import app_account_token, normalize_app_account_token
from common.user_rights import build_user_rights_payload
from config import subscription_tier_for_product


# ── 1. appAccountToken ───────────────────────────────────────────────────────

class TestAppAccountToken:
  def test_official_vectors(self):
    """ASSN 文档 §5.1：Swift 实机与 Python 交叉验证过的三条向量。"""
    assert app_account_token("user_123") == "4CD9EBA3-2ABA-57F3-AAA6-D0ACFC582FFD"
    assert app_account_token("10086") == "6242E22F-2496-51C9-91C7-D86156B56FC2"
    assert app_account_token("abc@mindora316.com") == "91846DA0-4700-5864-92FA-DD007B457019"

  def test_strip_and_case(self):
    assert app_account_token("  user_123  ") == app_account_token("user_123")
    assert normalize_app_account_token("4CD9EBA3-2ABA-57F3-AAA6-D0ACFC582FFD") == \
        "4cd9eba3-2aba-57f3-aaa6-d0acfc582ffd"
    assert normalize_app_account_token("") is None
    assert normalize_app_account_token(None) is None

  def test_tier_mapping(self):
    assert subscription_tier_for_product("com.mindora316.premium.monthly") == "premium"
    assert subscription_tier_for_product("com.mindora316.premium.yearly") == "premium"
    assert subscription_tier_for_product("com.mindora316.monthly") == "pro"
    assert subscription_tier_for_product("com.mindora316.yearly") == "pro"
    assert subscription_tier_for_product("com.other.product") is None
    assert subscription_tier_for_product(None) is None


# ── 2. _peek_environment ─────────────────────────────────────────────────────

def _unsigned_jws(payload: dict) -> str:
  import base64, json
  seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
  return f"e30.{seg}.Zm9v"  # header/签名段内容无所谓，peek 只读 payload 段


class TestPeekEnvironment:
  def test_data_environment(self):
    jws = _unsigned_jws({"data": {"environment": "Sandbox"}})
    assert apple_assn._peek_environment(jws) == Environment.SANDBOX

  def test_summary_environment(self):
    jws = _unsigned_jws({"summary": {"environment": "Production"}})
    assert apple_assn._peek_environment(jws) == Environment.PRODUCTION

  def test_default_production(self):
    jws = _unsigned_jws({"notificationType": "TEST"})
    assert apple_assn._peek_environment(jws) == Environment.PRODUCTION


# ── 测试夹具：假 tx/renewal/payload + 假 DB ─────────────────────────────────

def _tx(**kw):
  base = dict(
    originalTransactionId="1000000123456789",
    environment="Sandbox",
    appAccountToken=app_account_token("user_123"),
    productId="com.mindora316.monthly",
    transactionId="1000000123456790",
    purchaseDate=1698148900000,
    expiresDate=1698152500000,
    revocationDate=None,
    revocationReason=None,
  )
  base.update(kw)
  return SimpleNamespace(**base)


def _renewal(**kw):
  base = dict(
    autoRenewStatus=1,
    autoRenewProductId="com.mindora316.monthly",
    isInBillingRetryPeriod=None,
    gracePeriodExpiresDate=None,
    expirationIntent=None,
  )
  base.update(kw)
  return SimpleNamespace(**base)


@pytest.fixture()
def fake_db(monkeypatch):
  """替换 apple_assn 里的 DB 依赖为内存假实现，返回记录字典。"""
  state = {"upserts": [], "stamps": [], "logs": [], "uid": None, "log_dup": False}
  monkeypatch.setattr(apple_assn, "upsert_apple_subscription",
                      lambda otid, env, sd, **f: state["upserts"].append(
                        {"otid": otid, "env": env, "signed_date": sd, **f}) or "inserted")
  monkeypatch.setattr(apple_assn, "resolve_uid_for_app_account_token",
                      lambda token: state["uid"])
  monkeypatch.setattr(apple_assn, "stamp_basic_purchase_trial_by_uid",
                      lambda uid: state["stamps"].append(uid) or True)
  monkeypatch.setattr(apple_assn, "log_apple_notification",
                      lambda *a: state["logs"].append(a) or not state["log_dup"])
  return state


def _handle(ntype="SUBSCRIBED", subtype="INITIAL_BUY", tx=None, renewal=None,
            uuid="uuid-1", signed_date=1698148905000):
  payload = SimpleNamespace(
    notificationType=ntype, rawNotificationType=ntype,
    subtype=subtype, rawSubtype=subtype,
    signedDate=signed_date, notificationUUID=uuid,
  )
  return payload, tx, renewal


# ── 3. 应答语义 ──────────────────────────────────────────────────────────────

class TestResponseSemantics:
  def test_retryable_verification_failure_returns_500(self, monkeypatch):
    def boom(_):
      raise VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE)
    monkeypatch.setattr(apple_assn, "_verify", boom)
    assert apple_assn.handle_apple_notification("a.b.c") == 500

  def test_forged_signature_dropped_with_200(self, monkeypatch):
    def boom(_):
      raise VerificationException(VerificationStatus.VERIFICATION_FAILURE)
    monkeypatch.setattr(apple_assn, "_verify", boom)
    assert apple_assn.handle_apple_notification("a.b.c") == 200

  def test_malformed_payload_dropped_with_200(self, monkeypatch):
    monkeypatch.setattr(apple_assn, "_verify",
                        lambda _: (_ for _ in ()).throw(ValueError("bad jws")))
    assert apple_assn.handle_apple_notification("not-a-jws") == 200
    assert apple_assn.handle_apple_notification("") == 200

  def test_processing_db_error_returns_500(self, monkeypatch, fake_db):
    monkeypatch.setattr(apple_assn, "_verify",
                        lambda _: _handle(tx=_tx(), renewal=_renewal()))
    monkeypatch.setattr(apple_assn, "upsert_apple_subscription",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert apple_assn.handle_apple_notification("a.b.c") == 500

  def test_duplicate_notification_returns_200(self, monkeypatch, fake_db):
    fake_db["log_dup"] = True
    monkeypatch.setattr(apple_assn, "_verify",
                        lambda _: _handle(tx=_tx(), renewal=_renewal()))
    assert apple_assn.handle_apple_notification("a.b.c") == 200

  def test_happy_path_returns_200(self, monkeypatch, fake_db):
    monkeypatch.setattr(apple_assn, "_verify",
                        lambda _: _handle(tx=_tx(), renewal=_renewal()))
    assert apple_assn.handle_apple_notification("a.b.c") == 200
    assert len(fake_db["upserts"]) == 1
    assert len(fake_db["logs"]) == 1


# ── 4. 事件状态机 ────────────────────────────────────────────────────────────

class TestEventStateMachine:
  def _apply(self, ntype, subtype=None, tx=None, renewal=None, sd=1698148905000):
    apple_assn._apply_event(ntype, subtype, sd, tx, renewal)

  def test_subscribed_initial_buy_basic_stamps_trial(self, fake_db):
    fake_db["uid"] = "user_123"
    self._apply("SUBSCRIBED", "INITIAL_BUY", tx=_tx(), renewal=_renewal())
    up = fake_db["upserts"][0]
    assert up["tier"] == "pro"
    assert up["user_id"] == "user_123"
    assert up["app_account_token"] == app_account_token("user_123").lower()
    assert up["purchase_date"] is not None and up["expires_date"] is not None
    assert fake_db["stamps"] == ["user_123"]

  def test_subscribed_premium_product_no_trial_stamp(self, fake_db):
    fake_db["uid"] = "user_123"
    self._apply("SUBSCRIBED", "INITIAL_BUY",
                tx=_tx(productId="com.mindora316.premium.monthly"), renewal=_renewal())
    assert fake_db["upserts"][0]["tier"] == "premium"
    assert fake_db["stamps"] == []

  def test_subscribed_unbound_token_parks_without_stamp(self, fake_db):
    """appAccountToken 反查不到 uid：落库挂起、不盖章，等客户端上报认领（§5.2）。"""
    self._apply("SUBSCRIBED", "INITIAL_BUY", tx=_tx(), renewal=_renewal())
    assert "user_id" not in fake_db["upserts"][0]
    assert fake_db["stamps"] == []

  def test_resubscribe_also_stamps(self, fake_db):
    fake_db["uid"] = "user_123"
    self._apply("SUBSCRIBED", "RESUBSCRIBE", tx=_tx(), renewal=_renewal())
    assert fake_db["stamps"] == ["user_123"]

  def test_did_renew_updates_expiry_without_stamp(self, fake_db):
    self._apply("DID_RENEW", None, tx=_tx(expiresDate=1698158800000), renewal=_renewal())
    assert fake_db["upserts"][0]["expires_date"] == datetime.fromtimestamp(1698158800)
    assert fake_db["stamps"] == []

  def test_billing_recovery_clears_retry_flag(self, fake_db):
    self._apply("DID_RENEW", "BILLING_RECOVERY", tx=_tx(), renewal=_renewal())
    assert fake_db["upserts"][0]["is_in_billing_retry"] == 0

  def test_fail_to_renew_marks_retry_and_grace(self, fake_db):
    grace_ms = 1698158800000
    self._apply("DID_FAIL_TO_RENEW", "GRACE_PERIOD", tx=_tx(),
                renewal=_renewal(isInBillingRetryPeriod=True, gracePeriodExpiresDate=grace_ms))
    up = fake_db["upserts"][0]
    assert up["is_in_billing_retry"] == 1
    assert up["grace_period_expires_date"] == datetime.fromtimestamp(grace_ms / 1000)

  def test_grace_period_expired_clears_retry(self, fake_db):
    self._apply("GRACE_PERIOD_EXPIRED", None, tx=_tx(), renewal=_renewal())
    assert fake_db["upserts"][0]["is_in_billing_retry"] == 0

  def test_expired_records_intent(self, fake_db):
    self._apply("EXPIRED", "VOLUNTARY", tx=_tx(), renewal=_renewal(expirationIntent=1))
    up = fake_db["upserts"][0]
    assert up["expiration_intent"] == 1
    assert up["is_in_billing_retry"] == 0

  def test_refund_revokes_entitlement(self, fake_db):
    self._apply("REFUND", None, tx=_tx(revocationDate=1698150000000, revocationReason=0))
    up = fake_db["upserts"][0]
    assert up["revocation_date"] == datetime.fromtimestamp(1698150000)
    assert up["revocation_reason"] == 0

  def test_revoke_without_revocation_date_uses_now(self, fake_db):
    self._apply("REVOKE", None, tx=_tx())
    assert fake_db["upserts"][0]["revocation_date"] is not None

  def test_refund_reversed_restores_entitlement(self, fake_db):
    self._apply("REFUND_REVERSED", None, tx=_tx())
    up = fake_db["upserts"][0]
    assert up["revocation_date"] is None and up["revocation_reason"] is None

  def test_downgrade_keeps_current_tier(self, fake_db):
    """降档下期生效：不动当期 product/tier，只记 auto_renew_product_id。"""
    self._apply("DID_CHANGE_RENEWAL_PREF", "DOWNGRADE",
                tx=_tx(productId="com.mindora316.premium.monthly"),
                renewal=_renewal(autoRenewProductId="com.mindora316.monthly"))
    up = fake_db["upserts"][0]
    assert "tier" not in up and "product_id" not in up
    assert up["auto_renew_product_id"] == "com.mindora316.monthly"

  def test_upgrade_applies_immediately(self, fake_db):
    self._apply("DID_CHANGE_RENEWAL_PREF", "UPGRADE",
                tx=_tx(productId="com.mindora316.premium.monthly"), renewal=_renewal())
    assert fake_db["upserts"][0]["tier"] == "premium"

  def test_auto_renew_disabled_keeps_entitlement(self, fake_db):
    """关闭续订不当场收回权益：只记 auto_renew_status，expires_date 照常落库。"""
    self._apply("DID_CHANGE_RENEWAL_STATUS", "AUTO_RENEW_DISABLED",
                tx=_tx(), renewal=_renewal(autoRenewStatus=0))
    up = fake_db["upserts"][0]
    assert up["auto_renew_status"] == 0
    assert up["expires_date"] is not None
    assert "revocation_date" not in up

  def test_test_notification_no_upsert(self, fake_db):
    self._apply("TEST", None, tx=None, renewal=None)
    assert fake_db["upserts"] == []

  def test_unknown_type_with_tx_logged_and_recorded(self, fake_db):
    """Apple 新增类型带交易载荷：落库留痕，不抛异常（§6.1 兜底）。"""
    self._apply("SOME_FUTURE_TYPE", None, tx=_tx(), renewal=_renewal())
    assert len(fake_db["upserts"]) == 1

  def test_no_tx_payload_logged_only(self, fake_db):
    """summary/externalPurchaseToken 载荷（RENEWAL_EXTENSION 等）：无 tx 不落订阅表。"""
    self._apply("RENEWAL_EXTENSION", "SUMMARY", tx=None, renewal=None)
    assert fake_db["upserts"] == []

  def test_consumption_request_still_records(self, fake_db):
    self._apply("CONSUMPTION_REQUEST", None, tx=_tx(), renewal=_renewal())
    assert len(fake_db["upserts"]) == 1

  def test_unknown_product_tier_recorded_as_unknown(self, fake_db):
    self._apply("SUBSCRIBED", "INITIAL_BUY", tx=_tx(productId="com.future.newplan"))
    assert fake_db["upserts"][0]["tier"] == "unknown"
    assert fake_db["stamps"] == []


# ── 5. get_active_subscription_tier（§6.2 有效性公式 + 环境隔离）─────────────

class TestActiveSubscriptionTier:
  @pytest.fixture()
  def db(self, monkeypatch):
    import db.mysql_db as m
    state = {"rows": []}
    monkeypatch.setattr(m, "_query_all", lambda sql, params: list(state["rows"]))
    return m, state

  def _row(self, tier="premium", env="Production", days=30, revoked=False,
           retry=0, grace_days=None):
    now = datetime.now()
    return {
      "tier": tier,
      "expires_date": now + timedelta(days=days) if days is not None else None,
      "is_in_billing_retry": retry,
      "grace_period_expires_date": (now + timedelta(days=grace_days)) if grace_days else None,
      "revocation_date": now if revoked else None,
      "_env": env,
    }

  def test_active_premium_in_production(self, db):
    m, state = db
    state["rows"] = [self._row(tier="premium")]
    assert m.get_active_subscription_tier("user_123") == "premium"

  def test_basic_subscription_maps_to_pro(self, db):
    m, state = db
    state["rows"] = [self._row(tier="pro")]
    assert m.get_active_subscription_tier("user_123") == "pro"

  def test_expired_subscription_inactive(self, db):
    m, state = db
    state["rows"] = [self._row(days=-1)]
    assert m.get_active_subscription_tier("user_123") is None

  def test_revoked_subscription_inactive(self, db):
    m, state = db
    state["rows"] = [self._row(revoked=True)]
    assert m.get_active_subscription_tier("user_123") is None

  def test_billing_retry_within_grace_period_still_active(self, db):
    m, state = db
    state["rows"] = [self._row(days=-1, retry=1, grace_days=2)]
    assert m.get_active_subscription_tier("user_123") == "premium"

  def test_grace_period_passed_inactive(self, db):
    m, state = db
    state["rows"] = [self._row(days=-3, retry=1, grace_days=None)]
    assert m.get_active_subscription_tier("user_123") is None

  def test_multiple_rows_take_higher_tier(self, db):
    m, state = db
    state["rows"] = [self._row(tier="pro"), self._row(tier="premium")]
    assert m.get_active_subscription_tier("user_123") == "premium"

  def test_unknown_tier_ignored(self, db):
    m, state = db
    state["rows"] = [self._row(tier="unknown")]
    assert m.get_active_subscription_tier("user_123") is None

  def test_environment_isolation(self, monkeypatch):
    """SQL 层环境过滤：默认只查 Production，开关放行 Sandbox（doc §2）。"""
    import db.mysql_db as m
    captured = {}

    def fake_query(sql, params):
      captured["params"] = params
      return []

    monkeypatch.setattr(m, "_query_all", fake_query)
    m.get_active_subscription_tier("user_123")
    assert captured["params"][-1] == "Production"
    m.get_active_subscription_tier("user_123", allow_sandbox=True)
    assert captured["params"][-2:] == ("Production", "Sandbox")
    # 绑定双通道：user_id 直绑 + appAccountToken 前向匹配（小写）
    assert captured["params"][0] == "user_123"
    assert captured["params"][1] == app_account_token("user_123").lower()


# ── 6. effective_user_level 合并（MAX 语义，兼容兑换码授权）──────────────────

class TestEffectiveLevelMerge:
  def test_redeemed_premium_not_downgraded_by_basic_subscription(self):
    """兑换码换来的 premium + 一笔 Basic(pro) 订阅 → 仍 premium（MAX 语义）。"""
    end = datetime.now() + timedelta(days=300)
    payload = build_user_rights_payload("premium", end, subscription_level="pro")
    assert payload["effective_user_level"] == "premium"
    assert payload["subscription_level"] == "pro"

  def test_free_user_with_active_basic_subscription_is_pro(self):
    payload = build_user_rights_payload("free", None, subscription_level="pro")
    assert payload["effective_user_level"] == "pro"
    assert payload["membership_active"] is True

  def test_trial_outranks_everything(self):
    trial = datetime.now() + timedelta(days=7)
    payload = build_user_rights_payload("free", None, trial_end_at=trial,
                                        subscription_level="pro")
    assert payload["effective_user_level"] == "premium"

  def test_expired_stored_level_falls_back_to_subscription(self):
    expired = datetime.now() - timedelta(days=1)
    payload = build_user_rights_payload("premium", expired, subscription_level="pro")
    assert payload["effective_user_level"] == "pro"

  def test_no_subscription_keeps_legacy_behavior(self):
    payload = build_user_rights_payload("free", None)
    assert payload["effective_user_level"] == "free"
    assert payload["subscription_level"] is None
    # membership_active 沿用旧口径：free 恒 True（is_level_active 对 free 直接放行）
    assert payload["membership_active"] is True
