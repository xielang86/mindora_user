"""App Store 账号绑定：appAccountToken 派生（ASSN V2 文档 §5）。

客户端（SubscriptionManager.swift）购买时把 UUIDv5(uid) 盖进交易的 appAccountToken，
Apple 在 signedTransactionInfo 里原样带回。服务端对每个 uid 复算同一 UUID 即可绑定，
标准库即可，不需要第三方依赖。

注意：
- 必须是 uuid5（SHA-1），不是 uuid3（MD5），值完全不同
- name 是 uid 去首尾空白后的 UTF-8 字节
- 比较/入库前统一转小写（doc §5.1 建议）
"""
from __future__ import annotations

import uuid

# 客户端写死的命名空间 UUID（SubscriptionManager.swift:697）
APP_ACCOUNT_TOKEN_NAMESPACE = uuid.UUID("3D2B7A10-9E4C-5F8B-8C1D-2A6E4F0B7C93")


def app_account_token(uid: str) -> str:
  """uid → appAccountToken（大写 UUID 字符串，与 Apple 通知里的格式一致）。"""
  return str(uuid.uuid5(APP_ACCOUNT_TOKEN_NAMESPACE, uid.strip())).upper()


def normalize_app_account_token(token: str | None) -> str | None:
  """入库/比对前统一成小写；空值归一为 None。"""
  if not token:
    return None
  normalized = token.strip().lower()
  return normalized or None
