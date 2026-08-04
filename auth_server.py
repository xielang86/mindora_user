import datetime
import hashlib
import secrets
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from auth import AuthRequest, AuthResponse, AuthRequestType, JWTTokenData, AuthData
import logging
from common.email import send_verify_code_via_163, generate_verify_code
from common.sms import send_verify_code_via_sms
from common import wechat as wechat_svc
from config import Config
import os
import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pydantic import ValidationError
from db.mysql_db import (
  insert_user, get_user_by_email_or_uid, insert_or_restore_user,
  get_active_user_by_email_or_uid, soft_delete_user, get_user_contact,
  # web registration additions
  init_web_columns, register_user_with_password, get_user_password_hash,
  get_user_by_phone, register_phone_user, get_or_create_wechat_user,
  init_membership_schema, get_user_rights_info, redeem_redemption_code,
  create_redemption_codes,
)
from db.redis_db import get_verify_code, set_jwt_token, set_verify_code, delete_verify_code
from common.util import normalize_email
from common.jwt_keys import sign_token, verify_token
from uid.uuid import generate_uid_and_salt
import logger

load_dotenv()
run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))
logger.init_log(f"{run_dir}/auth_logs")

app = FastAPI(title="Auth Server")

app.add_middleware(
  CORSMiddleware,
  allow_origins=["http://localhost:8080", "http://127.0.0.1:8080",
                 "http://192.168.1.0/24"],  # adjust for LAN access
  allow_origin_regex=r"http://192\.168\.\d+\.\d+:\d+",
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

# Ensure auth-related schema exists when DB permissions allow it
try:
  init_membership_schema()
except Exception as _e:
  logging.warning("init_membership_schema failed (OK on first run): %s", _e)


# Mock database for demonstration
# In production, use Redis for codes and PostgreSQL/Mongo for users
mock_db = {
  "verify_codes": {},  # {email: "1234"}
  "users": {}          # {email: {"uid": "uuid", "created_at": "..."}}
}

# 加载配置（JWT 签名密钥改为 RS256 私钥文件，见 common/jwt_keys.py；
# JWT_SECRET_KEY 仅作为旧 HS256 token 的验签回退，不再用于签发）
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS"))
VERIFY_CODE_EXPIRE_SECONDS = int(os.getenv("VERIFY_CODE_EXPIRE_SECONDS"))
REDEMPTION_ADMIN_SECRET = os.getenv("REDEMPTION_ADMIN_SECRET", "")

# 邮件发件人配置
MY_163_EMAIL = "mindora2026@163.com"
MY_163_AUTH_CODE = "RZkiYNHsVxLGvVHG"  # deadline=20260412

# 删除账号验证码使用的固定 device_id，避免与登录验证码冲突
DELETE_VERIFY_CODE_DEVICE_ID = "delete"

# 业务响应码：与 AuthResponse 的 code 字段语义保持一致（2 = JWT token 过期）
JWT_EXPIRED_AUTH_CODE = 2

# 删除账号流程专用业务码
DELETE_VERIFY_SEND_FAILED_CODE = 409  # 注销验证码发送失败（邮件/SMS 通道异常）
DELETE_DB_FAILED_CODE = 500           # 注销时 DB 软删除失败，统一为服务端内部错误


def _auth_code_from_http_exc(exc: HTTPException) -> int:
  """将 JWT/认证类 HTTPException 映射为业务响应码。"""
  if exc.status_code == 401 and exc.detail == "Token expired":
    return JWT_EXPIRED_AUTH_CODE
  return exc.status_code


def _normalize_mac(mac: str | None) -> str:
  """统一 MAC 地址格式：去空、去分隔符、转小写。"""
  if not mac:
    return ""
  return mac.strip().lower().replace(":", "").replace("-", "").replace(".", "")


def _get_client_ip(request: Request) -> str:
  """获取客户端真实 IP，优先读取 X-Forwarded-For / X-Real-IP。"""
  forwarded = request.headers.get("X-Forwarded-For")
  if forwarded:
    return forwarded.split(",")[0].strip()
  real_ip = request.headers.get("X-Real-IP")
  if real_ip:
    return real_ip.strip()
  if request.client:
    return request.client.host
  return ""


def _is_legacy_delete_allowed(request: Request) -> tuple[bool, str]:
  """
  校验旧版 DELETE_USER 接口是否允许当前请求调用。
  返回 (allowed, reason)。
  """
  allowed_ips = Config.DELETE_USER_ALLOWED_IPS
  allowed_macs = Config.DELETE_USER_ALLOWED_MACS

  # 未配置任何白名单时默认拒绝，避免旧接口被意外开放
  if not allowed_ips and not allowed_macs:
    return False, "旧版删除接口未配置允许列表，已禁用"

  client_ip = _get_client_ip(request).lower()
  if allowed_ips and client_ip not in allowed_ips:
    return False, f"当前 IP {client_ip} 不在允许列表中"

  raw_mac = request.headers.get(Config.DELETE_USER_MAC_HEADER)
  client_mac = _normalize_mac(raw_mac)
  if allowed_macs:
    if not client_mac:
      return False, "请求缺少允许的 MAC 地址"
    if client_mac not in allowed_macs:
      return False, f"当前 MAC {raw_mac} 不在允许列表中"

  return True, ""


# ── Password helpers (PBKDF2-SHA256, no extra deps) ──────────────────────────

def _hash_password(password: str, salt: str) -> str:
  key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
  return key.hex()

def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
  return secrets.compare_digest(_hash_password(password, salt), stored_hash)

# ── JWT builder ───────────────────────────────────────────────────────────────

def _make_jwt(uid: str, email: str | None) -> tuple[str, int]:
  """Return (jwt_token, expire_days)."""
  expire_time = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRE_SECONDS)
  token = sign_token({"uid": uid, "email": email or "", "exp": expire_time})
  return token, max(1, int(JWT_EXPIRE_SECONDS / 86400))


def _safe_email(email: str | None, uid: str) -> str:
  if email and "@" in email:
    return email
  return f"{uid[:12]}@mindora.local"


def _build_token_data(uid: str, email: str | None, token: str, expire_days: int) -> JWTTokenData:
  rights_info = get_user_rights_info(uid)
  level_end_at = rights_info.get("level_end_at")
  if isinstance(level_end_at, str):
    try:
      level_end_at = datetime.fromisoformat(level_end_at)
    except ValueError:
      logging.warning("invalid level_end_at format for uid=%s: %s", uid, level_end_at)
      level_end_at = None
  return JWTTokenData(
    uid=uid,
    email=_safe_email(email, uid),
    token=token,
    expire_days=expire_days,
    user_level=rights_info.get("stored_user_level", "free"),
    effective_user_level=rights_info.get("effective_user_level", "free"),
    level_end_at=level_end_at,
    rights=rights_info.get("rights"),
  )

# ── SMS verification code key ─────────────────────────────────────────────────

def _sms_code_key(phone: str, device_id: str) -> tuple[str, str]:
  """Return (redis_email_arg, redis_device_arg) reusing the existing redis helpers."""
  return f"sms:{phone}", str(device_id) if device_id else "web"


# =============================================================================
# Existing handlers
# =============================================================================

def send_verify_code_handler(data: AuthData):
  # Generate 4-digit code
  verify_code = "1234"
  resp = AuthResponse(
    request_type = AuthRequestType(AuthRequestType.SEND_VERIFY_CODE),
    code=0,
    msg=f"Verify code sent successfully (Mock: {verify_code})",
    data=None
  )

  if Config.Mode == 1:
    mock_db["verify_codes"][data.email] = verify_code
  else:
    verify_code = generate_verify_code(4)
    set_verify_code(email=data.email, device_id=data.device_id, code=verify_code, expire_seconds=VERIFY_CODE_EXPIRE_SECONDS)
    status_data = send_verify_code_via_163(MY_163_EMAIL, MY_163_AUTH_CODE, data.email, verify_code)
    resp.code = status_data.get("code")
    resp.msg = status_data.get("msg")
    resp.data = None

  logging.info(f">>> [EMAIL SENT] To: {data.email}, Code: {verify_code}, return {resp}")
  return resp


def auth_by_verify_code(data: AuthData) -> AuthResponse:
  """
  验证码登录/注册核心函数
  :param email: 用户邮箱
  :param device_id: 设备ID
  :param verify_code: 用户输入的验证码
  :return: {"code": 0/400/401, "msg": "", "data": {"uid": "", "jwt_token": ""}}
  """
  resp = AuthResponse(
    request_type = AuthRequestType(AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE),
    code=0,
    msg="Login successful",
    data=None,
  )

  try:
    normalized_email = normalize_email(data.email)
    device_id = str(data.device_id)
    verify_code = "1234"
    if Config.Mode != 1:
      verify_code = get_verify_code(normalized_email, device_id)

    if not verify_code:
      resp.code = 401
      resp.msg = "验证码已过期或不存在"
      logging.info(f"verify: {verify_code} vs {data.verify_code}")
      raise HTTPException(status_code=401, detail="验证码已过期或不存在")

    if verify_code != data.verify_code:
      resp.code = 401
      resp.msg = "验证码错误"
      logging.info(f"verify: {verify_code} vs {data.verify_code}")
      raise HTTPException(status_code=401, detail="verify code error")
    
    user = get_user_by_email_or_uid(email=normalized_email)
    uid = None
    if not user:
      # new user：gen UID and insert to db
      uid, salt = generate_uid_and_salt(normalized_email)
      insert_result = insert_user(normalized_email, uid, salt, device_list=device_id)
      if insert_result < 1:
        resp.code = 500
        resp.msg = f"insert new user error for {data}, result={insert_result}"
        logging.error(f"insert error = {insert_result} for {normalized_email}, {uid}, {salt}")
        raise HTTPException(status_code=500, detail="internal server error")
    elif user.status == 0:
      # has been soft deleted, update the record
      result = insert_or_restore_user(user.email, user.uid, user.salt, data.device_id) 
      resp.code = result.get("code")
      resp.msg = result.get("msg")
      if resp.code != 0 and resp.code != 200:
        logging.error(f"insert error = {result} for {normalized_email}, {uid}, {user.salt}")
        raise HTTPException(status_code=500, detail="internal server error")
      uid= user.uid
    else: 
      uid = user.uid
    
    # 步骤4：生成JWT Token
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=JWT_EXPIRE_SECONDS)
    jwt_token = sign_token({
      "uid": uid,
      "email": normalized_email,
      "exp": expire_time,
    })
    
    # 步骤5：存储JWT Token到Redis（和JWT过期时间一致）
    set_jwt_token(uid, device_id, jwt_token, JWT_EXPIRE_SECONDS)
    resp.data = _build_token_data(
      uid=uid,
      email=normalized_email,
      token=jwt_token,
      expire_days=max(1, int(JWT_EXPIRE_SECONDS / 3600 / 24)),
    )
  
  except HTTPException:
    raise
  except Exception as e:
    # 捕获所有异常，返回服务器错误
    resp.code = 500
    resp.msg = "internal server error"
    logging.exception("auth_by_verify_code failed")
    raise HTTPException(status_code=500, detail="internal server error")

  logging.info(f"resp: {resp}")
  return resp

def decode_access_token(token: str):
  """Decodes and validates the JWT token."""
  try:
    payload = verify_token(token)
    return payload
  except jwt.ExpiredSignatureError:
    raise HTTPException(status_code=401, detail="Token expired")
  except jwt.InvalidTokenError:
    raise HTTPException(status_code=401, detail="Invalid token")
 

def auth_by_jwt(data: AuthData) -> AuthResponse:
  """Decodes and validates the JWT token."""
  payload = decode_access_token(data.jwt_token)

  uid = payload.get("uid")
  email = payload.get("email")

  if Config.Mode == 1:
    token, expire_days = _make_jwt(uid, email)
    return AuthResponse(
      request_type=AuthRequestType(AuthRequestType.LOGIN_WITH_JWT),
      code=0,
      msg="Token is valid",
      data=_build_token_data(uid=uid, email=email, token=token, expire_days=expire_days),
    )

  user = get_active_user_by_email_or_uid(email=None, uid=uid)
  if user is None:
    raise HTTPException(status_code=401, detail="cannot find user by jwt_token")

  token, expire_days = _make_jwt(uid, email)
  set_jwt_token(uid, "jwt_refresh", token, JWT_EXPIRE_SECONDS)

  return AuthResponse(
    request_type=AuthRequestType(AuthRequestType.LOGIN_WITH_JWT),
    code=0,
    msg="Token is valid",
    data=_build_token_data(uid=uid, email=user.email or email, token=token, expire_days=expire_days),
  )

def del_user(data: AuthData) -> AuthResponse:
  resp = AuthResponse(request_type=AuthRequestType.DELETE_USER, code=0, msg="User deleted")
  if Config.Mode == 1:
    return resp

  try:
    payload = decode_access_token(data.jwt_token)
  except HTTPException as e:
    resp.code = _auth_code_from_http_exc(e)
    resp.msg = e.detail
    return resp

  if payload is None:
    resp.code = 500
    resp.msg = "internal error"
    return resp

  uid = payload.get("uid")
  result = soft_delete_user(uid=uid)
  code = result.get("code")
  if code != 0 and code != 200:
    resp.code = code
    resp.msg = result.get("msg")

  return resp


def _resolve_delete_verify_contact(uid: str, jwt_email: str | None) -> tuple[str, bool]:
  """
  根据 uid 与 JWT 中的 email 决定验证码发送地址。
  优先使用真实邮箱；否则尝试手机号发送 SMS。
  返回 (contact: str, is_email: bool)
  """
  contact = ""
  is_email = True

  # 1. 优先使用 JWT 中的真实邮箱
  if jwt_email and "@" in jwt_email and not jwt_email.endswith(("@phone.local", "@wechat.local")):
    contact = jwt_email
  else:
    # 2. 从 DB 读取 email / phone
    row = get_user_contact(uid) if Config.Mode != 1 else None
    email = (row.get("email") or "").strip() if row else ""
    phone = (row.get("phone") or "").strip() if row else ""

    if email and "@" in email and not email.endswith(("@phone.local", "@wechat.local")):
      contact = email
    elif phone:
      contact = phone
      is_email = False

  return contact, is_email


def send_delete_verify_code_handler(data: AuthData) -> AuthResponse:
  """登录后请求删除账号验证码"""
  resp = AuthResponse(
    request_type=AuthRequestType.SEND_DELETE_VERIFY_CODE,
    code=0,
    msg="验证码已发送",
    data=None,
  )

  try:
    payload = decode_access_token(data.jwt_token)
  except HTTPException as e:
    resp.code = _auth_code_from_http_exc(e)
    resp.msg = e.detail
    return resp

  uid = payload.get("uid")
  jwt_email = payload.get("email")

  if Config.Mode == 1:
    mock_db["verify_codes"][f"{uid}@{DELETE_VERIFY_CODE_DEVICE_ID}"] = "1234"
    resp.msg = "验证码已发送 (Mock: 1234)"
    return resp

  user = get_active_user_by_email_or_uid(email=None, uid=uid)
  if user is None:
    raise HTTPException(status_code=401, detail="用户不存在或已被注销")

  contact, is_email = _resolve_delete_verify_contact(uid, jwt_email)
  if not contact:
    raise HTTPException(status_code=400, detail="用户未绑定有效的邮箱或手机号")

  verify_code = generate_verify_code(4)
  set_verify_code(
    email=contact,
    device_id=DELETE_VERIFY_CODE_DEVICE_ID,
    code=verify_code,
    expire_seconds=VERIFY_CODE_EXPIRE_SECONDS,
  )

  if is_email:
    status_data = send_verify_code_via_163(MY_163_EMAIL, MY_163_AUTH_CODE, contact, verify_code)
    resp.code = status_data.get("code", 0)
    resp.msg = status_data.get("msg", "邮件发送失败")
  else:
    status_data = send_verify_code_via_sms(contact, verify_code)
    resp.code = status_data.get("code", 0)
    resp.msg = status_data.get("msg", "短信发送失败")

  # 发送通道失败统一使用 409，不暴露底层服务商的具体错误码
  if resp.code != 0:
    resp.code = DELETE_VERIFY_SEND_FAILED_CODE
    logging.error("send_delete_verify_code failed for uid=%s contact=%s: %s", uid, contact, resp.msg)
    # 发送失败时删除已写入的验证码，避免脏数据
    delete_verify_code(contact, DELETE_VERIFY_CODE_DEVICE_ID)
    raise HTTPException(status_code=500, detail=resp.msg)

  logging.info(">>> [DELETE VERIFY CODE SENT] uid=%s contact=%s is_email=%s", uid, contact, is_email)
  return resp


def delete_user_with_code_handler(data: AuthData) -> AuthResponse:
  """登录后提交验证码删除账号"""
  resp = AuthResponse(
    request_type=AuthRequestType.DELETE_USER_WITH_CODE,
    code=0,
    msg="账号已注销",
    data=None,
  )

  try:
    payload = decode_access_token(data.jwt_token)
  except HTTPException as e:
    resp.code = _auth_code_from_http_exc(e)
    resp.msg = e.detail
    return resp

  uid = payload.get("uid")
  jwt_email = payload.get("email")

  if Config.Mode == 1:
    stored = mock_db["verify_codes"].get(f"{uid}@{DELETE_VERIFY_CODE_DEVICE_ID}")
    if stored != data.verify_code:
      raise HTTPException(status_code=401, detail="验证码错误或已过期")
    mock_db["verify_codes"].pop(f"{uid}@{DELETE_VERIFY_CODE_DEVICE_ID}", None)
    return resp

  user = get_active_user_by_email_or_uid(email=None, uid=uid)
  if user is None:
    raise HTTPException(status_code=401, detail="用户不存在或已被注销")

  contact, _ = _resolve_delete_verify_contact(uid, jwt_email)
  if not contact:
    raise HTTPException(status_code=400, detail="用户未绑定有效的邮箱或手机号")

  stored_code = get_verify_code(contact, DELETE_VERIFY_CODE_DEVICE_ID)
  if not stored_code or stored_code != data.verify_code:
    raise HTTPException(status_code=401, detail="验证码错误或已过期")

  # 消费验证码，防止重放
  delete_verify_code(contact, DELETE_VERIFY_CODE_DEVICE_ID)

  result = soft_delete_user(uid=uid)
  code = result.get("code")
  if code != 0 and code != 200:
    # DB 层错误统一映射为服务端内部错误，不暴露底层 DB 细节
    resp.code = DELETE_DB_FAILED_CODE
    resp.msg = result.get("msg", "账号注销失败")
    raise HTTPException(status_code=500, detail=resp.msg)

  return resp


# =============================================================================
# Web site handlers
# =============================================================================

def register_with_email_password_handler(data: AuthData) -> AuthResponse:
  """邮箱+验证码+密码注册"""
  resp = AuthResponse(
    request_type=AuthRequestType.REGISTER_WITH_EMAIL_PASSWORD,
    code=0, msg="注册成功", data=None,
  )
  normalized_email = normalize_email(data.email)

  # 1. Check duplicate
  if get_user_by_email_or_uid(email=normalized_email):
    raise HTTPException(status_code=400, detail="该邮箱已注册")

  # 2. Verify email code (reuses existing email code flow)
  stored_code = None
  if Config.Mode != 1:
    stored_code = get_verify_code(normalized_email, str(data.device_id) if data.device_id else "web")
  else:
    stored_code = "1234"

  if not stored_code:
    raise HTTPException(status_code=401, detail="验证码已过期或不存在")
  if stored_code != data.verify_code:
    raise HTTPException(status_code=401, detail="验证码错误")

  # 3. Hash password
  uid, salt = generate_uid_and_salt(normalized_email)
  pw_hash = _hash_password(data.password, salt)

  # 4. Insert user
  result = register_user_with_password(
    normalized_email, uid, salt, pw_hash,
    str(data.device_id) if data.device_id else "web",
  )
  if result < 1:
    logging.error("register_user_with_password failed for %s", normalized_email)
    raise HTTPException(status_code=500, detail="注册失败，请稍后重试")

  # 5. Return JWT
  token, expire_days = _make_jwt(uid, normalized_email)
  if Config.Mode != 1:
    set_jwt_token(uid, str(data.device_id) if data.device_id else "web", token, JWT_EXPIRE_SECONDS)

  resp.data = _build_token_data(uid=uid, email=normalized_email, token=token, expire_days=expire_days)
  logging.info("Registered (email+password): %s uid=%s", normalized_email, uid)
  return resp


def login_with_email_password_handler(data: AuthData) -> AuthResponse:
  """邮箱+密码登录"""
  resp = AuthResponse(
    request_type=AuthRequestType.LOGIN_WITH_EMAIL_PASSWORD,
    code=0, msg="登录成功", data=None,
  )
  normalized_email = normalize_email(data.email)

  # 1. Fetch user
  user = get_active_user_by_email_or_uid(email=normalized_email)
  if not user:
    raise HTTPException(status_code=401, detail="邮箱或密码错误")

  # 2. Verify password
  stored_hash = get_user_password_hash(normalized_email)
  if not stored_hash:
    raise HTTPException(status_code=401, detail="该账号未设置密码，请使用验证码登录")
  if not _verify_password(data.password, stored_hash, user.salt):
    raise HTTPException(status_code=401, detail="邮箱或密码错误")

  # 3. Return JWT
  token, expire_days = _make_jwt(user.uid, normalized_email)
  if Config.Mode != 1:
    set_jwt_token(user.uid, str(data.device_id) if data.device_id else "web", token, JWT_EXPIRE_SECONDS)

  resp.data = _build_token_data(uid=user.uid, email=normalized_email, token=token, expire_days=expire_days)
  logging.info("Login (email+password): %s uid=%s", normalized_email, user.uid)
  return resp


def send_sms_code_handler(data: AuthData) -> AuthResponse:
  """发送手机短信验证码"""
  phone = data.phone
  resp = AuthResponse(
    request_type=AuthRequestType.SEND_SMS_CODE,
    code=0, msg="验证码已发送", data=None,
  )
  code = "1234" if Config.Mode == 1 else generate_verify_code(6)

  if Config.Mode != 1:
    sms_email, sms_device = _sms_code_key(phone, data.device_id)
    set_verify_code(sms_email, sms_device, code, VERIFY_CODE_EXPIRE_SECONDS)
    result = send_verify_code_via_sms(phone, code)
    resp.code = result.get("code")
    resp.msg  = result.get("msg")
  else:
    logging.info("[DEV SMS mock] phone=%s code=%s", phone, code)

  logging.info("SMS code sent: phone=%s code=%s", phone, code)
  return resp


def register_or_login_with_phone_handler(data: AuthData, is_register: bool) -> AuthResponse:
  """手机号+SMS验证码 注册 or 登录（登录时若无账号自动注册）"""
  req_type = (AuthRequestType.REGISTER_WITH_PHONE if is_register
              else AuthRequestType.LOGIN_WITH_PHONE_SMS)
  resp = AuthResponse(request_type=req_type, code=0, msg="成功", data=None)
  phone = data.phone

  # 1. Verify SMS code
  if Config.Mode != 1:
    sms_email, sms_device = _sms_code_key(phone, data.device_id)
    stored_code = get_verify_code(sms_email, sms_device)
    if not stored_code:
      raise HTTPException(status_code=401, detail="验证码已过期或不存在")
    if stored_code != data.verify_code:
      raise HTTPException(status_code=401, detail="验证码错误")

  # 2. Check if user exists
  user = get_user_by_phone(phone)

  if is_register and user and user.status == 1:
    raise HTTPException(status_code=400, detail="该手机号已注册")

  if user is None or user.status == 0:
    # Auto-register
    uid, salt = generate_uid_and_salt(phone)
    result = register_phone_user(phone, uid, salt, str(data.device_id) if data.device_id else "web")
    if result < 1:
      logging.error("register_phone_user failed for %s", phone)
      raise HTTPException(status_code=500, detail="注册失败，请稍后重试")
    user = get_user_by_phone(phone)
    resp.msg = "注册并登录成功"
  else:
    resp.msg = "登录成功"

  # 3. Return JWT
  token, expire_days = _make_jwt(user.uid, user.email)
  if Config.Mode != 1:
    set_jwt_token(user.uid, str(data.device_id) if data.device_id else "web", token, JWT_EXPIRE_SECONDS)

  resp.data = _build_token_data(
    uid=user.uid,
    email=user.email or f"{phone}@phone.local",
    token=token,
    expire_days=expire_days,
  )
  logging.info("Phone auth: phone=%s uid=%s", phone, user.uid)
  return resp


def wechat_callback_handler(data: AuthData) -> AuthResponse:
  """微信OAuth code换token，自动注册/登录"""
  resp = AuthResponse(
    request_type=AuthRequestType.WECHAT_CALLBACK,
    code=0, msg="微信登录成功", data=None,
  )

  if not wechat_svc.is_wechat_enabled():
    raise HTTPException(status_code=503, detail="微信登录未配置，请联系管理员")

  try:
    token_data = wechat_svc.exchange_code(data.wechat_code)
    openid   = token_data["access_token"]   # note: field is access_token
    openid   = token_data["openid"]
    wx_token = token_data["access_token"]
    unionid  = token_data.get("unionid")
    info     = wechat_svc.get_user_info(wx_token, openid)
  except Exception as e:
    logging.error("WeChat OAuth error: %s", e)
    raise HTTPException(status_code=400, detail=f"微信授权失败：{e}")

  nickname   = info.get("nickname", "微信用户")
  avatar_url = info.get("headimgurl", "")

  user = get_or_create_wechat_user(openid, unionid, nickname, avatar_url)

  token, expire_days = _make_jwt(user.uid, user.email)
  if Config.Mode != 1:
    set_jwt_token(user.uid, "wechat", token, JWT_EXPIRE_SECONDS)

  resp.data = _build_token_data(
    uid=user.uid,
    email=user.email or f"{openid[:8]}@wechat.local",
    token=token,
    expire_days=expire_days,
  )
  logging.info("WeChat login: openid=%s uid=%s", openid, user.uid)
  return resp


def query_user_rights_handler(data: AuthData) -> AuthResponse:
  payload = decode_access_token(data.jwt_token)
  uid = payload.get("uid")
  rights_info = get_user_rights_info(uid)
  return AuthResponse(
    request_type=AuthRequestType.QUERY_USER_RIGHTS,
    code=0,
    msg="success",
    data=rights_info,
  )


def redeem_redemption_code_handler(data: AuthData) -> AuthResponse:
  payload = decode_access_token(data.jwt_token)
  uid = payload.get("uid")
  result = redeem_redemption_code(uid, data.redemption_code)
  if result["code"] != 0:
    raise HTTPException(status_code=result["code"], detail=result["msg"])
  return AuthResponse(
    request_type=AuthRequestType.REDEEM_REDEMPTION_CODE,
    code=0,
    msg=result["msg"],
    data=result["data"],
  )


def generate_redemption_codes_handler(data: AuthData) -> AuthResponse:
  if not REDEMPTION_ADMIN_SECRET:
    raise HTTPException(status_code=503, detail="REDEMPTION_ADMIN_SECRET is not configured")
  if data.admin_secret != REDEMPTION_ADMIN_SECRET:
    raise HTTPException(status_code=403, detail="invalid admin secret")

  generated = create_redemption_codes(
    batch_id=data.batch_id,
    target_level=data.target_level,
    duration_days=data.duration_days,
    quantity=data.quantity,
    expire_at=data.code_expire_at,
    created_by="auth_server",
  )
  return AuthResponse(
    request_type=AuthRequestType.GENERATE_REDEMPTION_CODES,
    code=0,
    msg="redemption codes generated",
    data={
      "batch_id": data.batch_id,
      "target_level": data.target_level,
      "duration_days": data.duration_days,
      "quantity": len(generated),
      "expire_at": data.code_expire_at.isoformat() if data.code_expire_at else None,
      "codes": generated,
    },
  )


# --- Handlers ---
@app.post("/auth", response_model=AuthResponse)
async def handle_auth(request: AuthRequest, raw_request: Request):
  logging.info(f"request {request}")
  req_type = request.request_type
  data = request.data

  # 旧版 DELETE_USER 接口：先校验 IP/MAC 白名单
  if req_type == AuthRequestType.DELETE_USER:
    allowed, reason = _is_legacy_delete_allowed(raw_request)
    if not allowed:
      logging.warning("[DELETE_USER] rejected: %s", reason)
      return AuthResponse(
        request_type=AuthRequestType.DELETE_USER,
        code=403,
        msg=reason,
        data=None,
      )

  # 1. SEND EMAIL VERIFY CODE
  if req_type == AuthRequestType.SEND_VERIFY_CODE:
    return send_verify_code_handler(data)

  # 2. LOGIN/REGISTER WITH EMAIL VERIFY CODE (original device flow)
  elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE:
    return auth_by_verify_code(data)

  # 3. LOGIN WITH JWT
  elif req_type == AuthRequestType.LOGIN_WITH_JWT:
    logging.info(f"login by jwt: {data}")
    return auth_by_jwt(data)

  # 4. DELETE USER
  elif req_type == AuthRequestType.DELETE_USER:
    return del_user(data)

  # 4.1 登录后请求删除账号验证码
  elif req_type == AuthRequestType.SEND_DELETE_VERIFY_CODE:
    return send_delete_verify_code_handler(data)

  # 4.2 登录后提交验证码删除账号
  elif req_type == AuthRequestType.DELETE_USER_WITH_CODE:
    return delete_user_with_code_handler(data)

  # ── Web site flows ────────────────────────────────────────────────────────
  # 5. REGISTER: email + verify code + password
  elif req_type == AuthRequestType.REGISTER_WITH_EMAIL_PASSWORD:
    return register_with_email_password_handler(data)

  # 6. LOGIN: email + password
  elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_PASSWORD:
    return login_with_email_password_handler(data)

  # 7. SEND SMS CODE
  elif req_type == AuthRequestType.SEND_SMS_CODE:
    return send_sms_code_handler(data)

  # 8. REGISTER: phone + SMS code
  elif req_type == AuthRequestType.REGISTER_WITH_PHONE:
    return register_or_login_with_phone_handler(data, is_register=True)

  # 9. LOGIN: phone + SMS code (auto-register if new)
  elif req_type == AuthRequestType.LOGIN_WITH_PHONE_SMS:
    return register_or_login_with_phone_handler(data, is_register=False)

  # 10. WECHAT: exchange code for token
  elif req_type == AuthRequestType.WECHAT_CALLBACK:
    return wechat_callback_handler(data)

  elif req_type == AuthRequestType.REDEEM_REDEMPTION_CODE:
    return redeem_redemption_code_handler(data)

  elif req_type == AuthRequestType.GENERATE_REDEMPTION_CODES:
    return generate_redemption_codes_handler(data)

  elif req_type == AuthRequestType.QUERY_USER_RIGHTS:
    return query_user_rights_handler(data)

  raise HTTPException(status_code=400, detail="Unsupported request type")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
  if isinstance(exc, HTTPException):
    raise exc
  if isinstance(exc, ValidationError):
    logging.exception("response/request validation failed")
    return JSONResponse(
      status_code=500,
      content={"code": 500, "msg": "validation failed", "detail": str(exc)},
    )
  logging.exception("Unhandled auth server exception")
  return JSONResponse(
    status_code=500,
    content={"code": 500, "msg": "internal server error", "detail": str(exc)},
  )

@app.get("/auth/wechat/qrcode")
async def wechat_qrcode():
  """Return WeChat QR-code page URL for PC scan-to-login."""
  if not wechat_svc.is_wechat_enabled():
    raise HTTPException(status_code=503, detail="微信登录未配置")
  url, state = wechat_svc.get_qrcode_url()
  return {"qrcode_url": url, "state": state}


@app.get("/health")
async def health():
  return {"status": "ok", "service": "auth_server"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9103)
