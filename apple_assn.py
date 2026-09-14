"""ASSN V2：App Store Server Notifications 接收与处理（服务端-AppStore订阅通知接入(ASSN V2).md）。

设计要点（全部来自文档）：
- 验签是唯一防线（接口公网裸露、无鉴权 header）：三层 JWS 都要验（§4.2），
  用 Apple 官方 app-store-server-library，根证书离线内置（certs/AppleRootCA-G3.cer）
- 应答语义（§4.3）：处理成功/丢弃 → 200；只有 RETRYABLE_VERIFICATION_FAILURE
  （OCSP 网络抖动）与 DB 内部错误 → 500，让 Apple 按 1h/12h/24h/48h/72h 重投
- 幂等（§7.2①）：notificationUUID 落 apple_notification_log 去重
- 乱序保护（§7.2②）：apple_subscription 只接受 signedDate 更新的写入
- 环境隔离（§2/§7.2③）：主键带 environment；鉴权只认 Production，
  Sandbox 放行靠 Config.APPLE_SUBSCRIPTION_SANDBOX_ENTITLEMENT 显式开关
- 未知 notificationType 必须落日志 + 200，绝不抛异常/5xx（§6.1）
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
  SignedDataVerifier,
  VerificationException,
  VerificationStatus,
)

from common.apple_binding import normalize_app_account_token
from config import Config, subscription_tier_for_product
from db.mysql_db import (
  log_apple_notification,
  resolve_uid_for_app_account_token,
  stamp_basic_purchase_trial_by_uid,
  upsert_apple_subscription,
)

# 体验期第②段盖章点（§8）：SUBSCRIBED 且 subtype ∈ {INITIAL_BUY, RESUBSCRIBE} 且 Basic 档
_TRIAL_STAMP_SUBTYPES = {"INITIAL_BUY", "RESUBSCRIBE"}

_verifiers: dict[Environment, SignedDataVerifier] | None = None


def _get_verifiers() -> dict[Environment, SignedDataVerifier]:
  """懒加载双环境 verifier（构造顺序固定：root_certs, online_checks, environment, bundle_id[, app_apple_id]）。"""
  global _verifiers
  if _verifiers is None:
    root_certs = []
    for path in Config.APPLE_ROOT_CERT_PATHS:
      with open(path, "rb") as f:
        root_certs.append(f.read())
    if not root_certs:
      raise RuntimeError("APPLE_ROOT_CERT_PATHS is empty; ASSN verification unavailable")
    _verifiers = {
      Environment.SANDBOX: SignedDataVerifier(
        root_certs, Config.APPLE_ASSN_ONLINE_CHECKS, Environment.SANDBOX, Config.APPLE_BUNDLE_ID,
      ),
      Environment.PRODUCTION: SignedDataVerifier(
        root_certs, Config.APPLE_ASSN_ONLINE_CHECKS, Environment.PRODUCTION,
        Config.APPLE_BUNDLE_ID, Config.APPLE_APP_ID,
      ),
    }
  return _verifiers


def _peek_environment(signed_payload: str) -> Environment:
  """不验签地读出 environment，仅用于挑 verifier。这里读到的内容一概不信任（§4.2）。"""
  seg = signed_payload.split(".")[1]
  body = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
  # data / summary / externalPurchaseToken 互斥，一条通知只带其中一个
  for key in ("data", "summary", "externalPurchaseToken"):
    value = body.get(key)
    if isinstance(value, dict) and value.get("environment"):
      return Environment(value["environment"])
  return Environment.PRODUCTION


def _ms_to_dt(ms: int | None) -> datetime | None:
  """Apple 毫秒时间戳 → naive datetime（与代码库 datetime.now() 口径一致）。"""
  return datetime.fromtimestamp(ms / 1000) if ms else None


def _enum_raw(value):
  """官方库枚举（含未知值时的 raw 字符串）→ 原始值字符串。"""
  if value is None:
    return None
  return str(getattr(value, "value", value))


def _int_or_none(value) -> int | None:
  raw = _enum_raw(value)
  if raw is None:
    return None
  try:
    return int(raw)
  except (TypeError, ValueError):
    return None


def _verify(signed_payload: str):
  """三层验签（§4.2）。成功返回 (verifier, payload, tx, renewal)；失败抛 VerificationException。"""
  verifier = _get_verifiers()[_peek_environment(signed_payload)]
  payload = verifier.verify_and_decode_notification(signed_payload)

  tx = renewal = None
  # payload.data 不必然存在（data/summary/externalPurchaseToken 互斥）；
  # TEST 通知的 data 里没有 signedTransactionInfo / signedRenewalInfo —— 必须判空
  if payload.data is not None:
    if payload.data.signedTransactionInfo:
      tx = verifier.verify_and_decode_signed_transaction(payload.data.signedTransactionInfo)
    if payload.data.signedRenewalInfo:
      renewal = verifier.verify_and_decode_renewal_info(payload.data.signedRenewalInfo)
  return payload, tx, renewal


def handle_apple_notification(signed_payload: str) -> int:
  """处理一条 ASSN 通知，返回应答给 Apple 的 HTTP 状态码（§4.3）。

  200 = 成功或丢弃（伪签/未知类型/重复投递）；500 = 可重试失败（OCSP 抖动、DB 错误）。
  """
  if not signed_payload:
    return 200

  try:
    payload, tx, renewal = _verify(signed_payload)
  except VerificationException as e:
    if e.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
      # 验签过程本身失败（OCSP 网络不通/超时），不代表签名无效 —— 500 让 Apple 重投
      logging.warning("apple_assn: retryable verification failure, ask Apple to retry: %s", e)
      return 500
    # 签名/证书/身份确实不对：200 丢弃 + 告警，5xx 只会让 Apple 反复重投无效消息
    logging.warning("apple_assn: verification failed (%s), notification dropped", e.status)
    return 200
  except Exception:
    # 无法解析的非法请求（不是 JWS、缺段等）：不值得重投，200 丢弃
    logging.warning("apple_assn: malformed signedPayload dropped", exc_info=True)
    return 200

  ntype = _enum_raw(payload.notificationType) or payload.rawNotificationType or ""
  subtype = _enum_raw(payload.subtype) or payload.rawSubtype
  signed_date = int(payload.signedDate or 0)
  notification_uuid = payload.notificationUUID or ""

  try:
    _apply_event(ntype, subtype, signed_date, tx, renewal)
    # 处理成功后写幂等日志（§7.2①）。主键冲突 = 并发/重投已处理过，正常
    first = log_apple_notification(notification_uuid, ntype, subtype, signed_date, signed_payload)
    if not first:
      logging.info("apple_assn: duplicate notification %s (%s/%s), already processed",
                   notification_uuid, ntype, subtype)
  except Exception:
    # DB 等内部错误：500 让 Apple 重投。重投时幂等由 upsert 的 signedDate 守卫 +
    # 体验期 NULL 守卫保证，重复处理无害（故日志放在处理后写，避免"已记日志但
    # 处理失败"的死信）
    logging.exception("apple_assn: processing failed (%s/%s uuid=%s), ask Apple to retry",
                      ntype, subtype, notification_uuid)
    return 500

  logging.info("apple_assn: processed %s/%s uuid=%s", ntype, subtype, notification_uuid)
  return 200


def _apply_event(ntype: str, subtype: str | None, signed_date: int, tx, renewal) -> None:
  """事件状态机（§6.1）。tx 为 None 的类型（TEST/summary 载荷/未知类型）只落日志。"""
  if ntype == "TEST":
    logging.info("apple_assn: TEST notification received (link check OK)")
    return

  if ntype == "CONSUMPTION_REQUEST":
    # Apple 正在处理退款申请、索要消费数据，12 小时内应回应（§6.1）——现阶段先告警
    logging.warning("apple_assn: CONSUMPTION_REQUEST received, manual response needed within 12h")

  if tx is None:
    # 未知类型 / 无交易载荷（RENEWAL_EXTENSION 走 summary、EXTERNAL_PURCHASE_TOKEN 等）：
    # 原样落日志并 200，不能抛异常（§6.1：Apple 会新增类型）
    logging.info("apple_assn: notification %s/%s has no transaction payload, logged only",
                 ntype, subtype)
    return

  otid = tx.originalTransactionId
  environment = _enum_raw(tx.environment) or "Production"
  token = normalize_app_account_token(tx.appAccountToken)
  tier = subscription_tier_for_product(tx.productId) or "unknown"

  # 账号绑定（§5）：UUIDv5 不可逆，靠前序认领记录反查；查不到则 user_id 悬空，
  # 等客户端 report_subscription 带 original_transaction_id 来认领（§5.2）
  user_id = resolve_uid_for_app_account_token(token)

  fields: dict = {
    "app_account_token": token,
    "product_id": tx.productId or "",
    "tier": tier,
    "latest_transaction_id": tx.transactionId or "",
    "purchase_date": _ms_to_dt(tx.purchaseDate) or datetime.now(),
    "expires_date": _ms_to_dt(tx.expiresDate),
    "raw_payload": None,
  }
  if user_id:
    fields["user_id"] = user_id
  if renewal is not None:
    fields["auto_renew_status"] = _int_or_none(renewal.autoRenewStatus) or 0
    fields["auto_renew_product_id"] = renewal.autoRenewProductId
    if renewal.isInBillingRetryPeriod is not None:
      fields["is_in_billing_retry"] = 1 if renewal.isInBillingRetryPeriod else 0
    if renewal.gracePeriodExpiresDate:
      fields["grace_period_expires_date"] = _ms_to_dt(renewal.gracePeriodExpiresDate)

  # ── 事件特异处理（§6.1 事件表）──────────────────────────────────────────
  if ntype == "SUBSCRIBED":
    pass  # 基础字段已够：建记录/重新激活
  elif ntype == "DID_RENEW":
    if subtype == "BILLING_RECOVERY":
      fields["is_in_billing_retry"] = 0  # 扣款恢复，清除重试标记
  elif ntype == "DID_CHANGE_RENEWAL_STATUS":
    # 用户只是开/关续订，当期权益到 expires_date 自然有效，不动档位
    pass
  elif ntype == "DID_CHANGE_RENEWAL_PREF":
    if subtype == "UPGRADE":
      pass  # 升档立即生效：tx 已是新产品，tier 随基础字段更新
    elif subtype == "DOWNGRADE":
      # 降档下期才生效：当期保持原档位，只记下期产品
      fields.pop("product_id", None)
      fields.pop("tier", None)
  elif ntype == "DID_FAIL_TO_RENEW":
    fields["is_in_billing_retry"] = 1
    # 无宽限期时权益按 expires_date 自然到期，不主动收回
  elif ntype == "GRACE_PERIOD_EXPIRED":
    fields["is_in_billing_retry"] = 0  # 宽限期结束：grace 已过，有效性公式自然判负
  elif ntype == "EXPIRED":
    fields["is_in_billing_retry"] = 0
    if renewal is not None:
      fields["expiration_intent"] = _int_or_none(renewal.expirationIntent)
  elif ntype in ("REFUND", "REVOKE"):
    # 退款成功 / 家庭共享被撤销：收回权益
    fields["revocation_date"] = _ms_to_dt(tx.revocationDate) or datetime.now()
    fields["revocation_reason"] = _int_or_none(tx.revocationReason)
  elif ntype == "REFUND_REVERSED":
    fields["revocation_date"] = None  # 退款被撤销：恢复权益
    fields["revocation_reason"] = None
  else:
    # PRICE_INCREASE / OFFER_REDEEMED / RENEWAL_EXTENDED / ONE_TIME_CHARGE / …
    # 未特异处理的类型：落库留痕即可
    logging.info("apple_assn: %s/%s recorded without specific handling", ntype, subtype)

  result = upsert_apple_subscription(otid, environment, signed_date, **fields)
  if result == "stale":
    logging.info("apple_assn: stale notification ignored (otid=%s signed_date=%d)", otid, signed_date)

  # 体验期第②段盖章（§8）：SUBSCRIBED + Basic 档 + 能定位到 uid。
  # 与客户端 report_subscription 共用 IS NULL 守卫，天然去重；
  # uid 暂时定位不到时等客户端上报认领后由其盖章兜底
  if ntype == "SUBSCRIBED" and subtype in _TRIAL_STAMP_SUBTYPES and tier == "pro" and user_id:
    try:
      granted = stamp_basic_purchase_trial_by_uid(user_id)
      if granted:
        logging.info("apple_assn: basic-purchase premium trial stamped for uid=%s", user_id)
    except Exception:
      # 盖章失败不影响通知应答（客户端上报路径会补盖，NULL 守卫幂等）
      logging.exception("apple_assn: trial stamp failed for uid=%s", user_id)
