import datetime
import hashlib
import secrets
import jwt
from fastapi import FastAPI, HTTPException
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
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pydantic import ValidationError
from db.mysql_db import (
  insert_user, get_user_by_email_or_uid, insert_or_restore_user,
  get_active_user_by_email_or_uid, soft_delete_user,
  # web registration additions
  init_web_columns, register_user_with_password, get_user_password_hash,
  get_user_by_phone, register_phone_user, get_or_create_wechat_user,
  init_membership_schema, get_user_rights_info, redeem_redemption_code,
  create_redemption_codes,
)
from db.redis_db import get_verify_code, set_jwt_token, set_verify_code
from common.util import normalize_email
from uid.uuid import generate_uid_and_salt
import logger

load_dotenv()
run_dir = os.getenv("RUN_DIR")
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

# 加载配置
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS"))
VERIFY_CODE_EXPIRE_SECONDS = int(os.getenv("VERIFY_CODE_EXPIRE_SECONDS"))
REDEMPTION_ADMIN_SECRET = os.getenv("REDEMPTION_ADMIN_SECRET", "")

# ── Password helpers (PBKDF2-SHA256, no extra deps) ──────────────────────────

def _hash_password(password: str, salt: str) -> str:
  key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
  return key.hex()

def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
  return secrets.compare_digest(_hash_password(password, salt), stored_hash)

# ── JWT builder ───────────────────────────────────────────────────────────────

def _make_jwt(uid: str, email: str | None) -> tuple[str, int]:
  """Return (jwt_token, expire_days)."""
  expire_time = datetime.now() + timedelta(seconds=JWT_EXPIRE_SECONDS)
  token = jwt.encode(
    {"uid": uid, "email": email or "", "exp": expire_time},
    JWT_SECRET_KEY,
    algorithm=Config.ALGORITHM,
  )
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
  MY_163_EMAIL = "mindora2026@163.com"
  MY_163_AUTH_CODE = "RZkiYNHsVxLGvVHG"  # deadline=20260412

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
    expire_time = datetime.now() + timedelta(seconds=JWT_EXPIRE_SECONDS)
    jwt_token = jwt.encode(
      {
        "uid": uid,
        "email": normalized_email,
        "exp": expire_time
      },
      JWT_SECRET_KEY,
      algorithm=Config.ALGORITHM
    )
    
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
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[Config.ALGORITHM])
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

  payload = decode_access_token(data.jwt_token) 
  if payload is None:
    resp.code = 500
    resp.msg = "internal error"
    raise HTTPException(status_code=500, detail=resp.msg)

  uid = payload.get("uid")
  result = soft_delete_user(uid=uid)
  code = result.get("code")
  if code != 0 and result.get("code")!= 200:
    resp.code = code
    resp.msg = result.get("msg")

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
async def handle_auth(request: AuthRequest):
  logging.info(f"request {request}")
  req_type = request.request_type
  data = request.data

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
