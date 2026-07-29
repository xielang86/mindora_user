"""JWT 密钥加载与签发/验签助手（RS256 改造）。

密钥布局：
  - 私钥：仅云端（auth_server 签发 token 用），通过 JWT_PRIVATE_KEY_PATH 或
    JWT_PRIVATE_KEY（inline PEM）提供，绝不进代码库/设备镜像
  - 公钥：云端 user_server + 嵌入式设备验签用，通过 JWT_PUBLIC_KEY_PATH 或
    JWT_PUBLIC_KEY 提供；公钥不是秘密，可直接打进设备镜像
  - 旧 HS256 token 兼容：验签时若 RS256 失败且配置了 JWT_SECRET_KEY，
    回退尝试 HS256，避免线上已签发 token 立即失效

注意：PyJWT 的 RS256 依赖 cryptography 包（签发/验签两侧都需要）。
"""
import logging
import os
from typing import Optional

import jwt

from config import Config

_private_key: Optional[str] = None
_public_key: Optional[str] = None


def _load_key(path_env: str, inline_env: str, default_path: str) -> Optional[str]:
  """优先读 inline PEM 环境变量，其次读 key 文件路径。"""
  inline = os.getenv(inline_env)
  if inline:
    # 允许 inline PEM 用 \n 转义存储（systemd/docker env 常见写法）
    return inline.replace("\\n", "\n")
  path = os.getenv(path_env, default_path)
  try:
    with open(path, "r", encoding="utf-8") as f:
      return f.read()
  except FileNotFoundError:
    return None


def get_private_key() -> Optional[str]:
  global _private_key
  if _private_key is None:
    _private_key = _load_key("JWT_PRIVATE_KEY_PATH", "JWT_PRIVATE_KEY", Config.JWT_PRIVATE_KEY_PATH)
    if _private_key is None:
      logging.warning("JWT private key not found (JWT_PRIVATE_KEY_PATH/JWT_PRIVATE_KEY)")
  return _private_key


def get_public_key() -> Optional[str]:
  global _public_key
  if _public_key is None:
    _public_key = _load_key("JWT_PUBLIC_KEY_PATH", "JWT_PUBLIC_KEY", Config.JWT_PUBLIC_KEY_PATH)
    if _public_key is None:
      logging.warning("JWT public key not found (JWT_PUBLIC_KEY_PATH/JWT_PUBLIC_KEY)")
  return _public_key


def sign_token(payload: dict) -> str:
  """用 RS256 私钥签发 token。payload 需自带 exp 等声明。"""
  key = get_private_key()
  if not key:
    raise RuntimeError("JWT private key not configured, cannot sign token")
  return jwt.encode(payload, key, algorithm=Config.ALGORITHM)


def verify_token(token: str) -> dict:
  """验签 token，返回 payload。

  先按 RS256 验签；失败且配置了旧 JWT_SECRET_KEY 时回退 HS256（兼容旧 token）。
  失败抛 jwt.InvalidTokenError / jwt.ExpiredSignatureError，由调用方处理。
  """
  key = get_public_key()
  if key:
    try:
      return jwt.decode(token, key, algorithms=[Config.ALGORITHM])
    except jwt.ExpiredSignatureError:
      raise
    except jwt.InvalidTokenError:
      pass  # 继续尝试旧算法

  legacy_secret = os.getenv("JWT_SECRET_KEY")
  if legacy_secret:
    return jwt.decode(token, legacy_secret, algorithms=[Config.LEGACY_ALGORITHM])

  # 无可用验签方式：抛出与上面一致的异常类型
  raise jwt.InvalidTokenError("no usable JWT verification key")
