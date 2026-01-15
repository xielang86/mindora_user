import datetime
import jwt
from fastapi import FastAPI, HTTPException
from auth import AuthRequest, AuthResponse, AuthRequestType, JWTTokenData, AuthData
import logging
from common.email import send_verify_code_via_163, generate_verify_code
from config import Config
import os
import jwt
from datetime import datetime, timedelta
from dotenv import load_dotenv
from db.mysql_db import insert_user, get_user_by_email_or_uid, insert_or_restore_user, get_active_user_by_email_or_uid, soft_delete_user
from db.redis_db import get_verify_code, set_jwt_token, set_verify_code
from common.util import normalize_email
from uid.uuid import generate_uid_and_salt
import logger

logger.init_log("auth_logs")

app = FastAPI(title="Auth Server")


# Mock database for demonstration
# In production, use Redis for codes and PostgreSQL/Mongo for users
mock_db = {
  "verify_codes": {},  # {email: "1234"}
  "users": {}          # {email: {"uid": "uuid", "created_at": "..."}}
}

# 加载配置
load_dotenv()
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS"))
VERIFY_CODE_EXPIRE_SECONDS = int(os.getenv("VERIFY_CODE_EXPIRE_SECONDS"))

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
    data = JWTTokenData(
      uid="uid",
      email=data.email,
      token="jwt_token",
      expire_days= max(1, JWT_EXPIRE_SECONDS / 3600 / 24)
    )
  )

  try:
    normalized_email = normalize_email(data.email)
    verify_code = "1234"
    if Config.Mode != 1:
      verify_code = get_verify_code(normalized_email, data.device_id)

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
      insert_result = insert_user(normalized_email, uid, salt, device_list=data.device_id)
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
    set_jwt_token(uid, data.device_id, jwt_token, JWT_EXPIRE_SECONDS)
    resp.data = JWTTokenData(
      uid=uid,
      email=data.email,
      token=jwt_token,
      expire_days= max(1, JWT_EXPIRE_SECONDS / 3600 / 24)
    )
  
  except Exception as e:
    # 捕获所有异常，返回服务器错误
    resp.code = 500
    resp.msg = "internal server error"
    logging.error(f"error: {e}")
    raise HTTPException(status_code=500, detail="internal server error")

  logging.info(f"resp: {resp}")
  return resp

def decode_access_token(token: str):
  """Decodes and validates the JWT token."""
  try:
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=Config.ALGORITHM)
    return payload
  except jwt.ExpiredSignatureError:
    raise HTTPException(status_code=401, detail="Token expired")
  except jwt.InvalidTokenError:
    raise HTTPException(status_code=401, detail="Invalid token")
 

def auth_by_jwt(data: AuthData) -> AuthResponse:
  resp = AuthResponse(
    request_type = AuthRequestType(AuthRequestType.LOGIN_WITH_JWT),
    code=0,
    msg="Token is valid",
    data= None
  )

  """Decodes and validates the JWT token."""
  payload = decode_access_token(data.jwt_token)

  if Config.Mode == 1:
    return resp

  email = payload.get("email")
  uid = payload.get("uid")

  user = get_active_user_by_email_or_uid(email=None, uid=uid)
  if user is None:
    resp.code = 500
    resp.msg = "cannot find user by jwt_token"
    raise HTTPException(status_code=401, detail="jwt expire")

  return resp

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

# --- Handlers ---
@app.post("/auth", response_model=AuthResponse)
async def handle_auth(request: AuthRequest):
  logging.info(f"request {request}")
  req_type = request.request_type
  data = request.data

  # 1. SEND VERIFY CODE
  if req_type == AuthRequestType.SEND_VERIFY_CODE:
    return send_verify_code_handler(data)

  # 2. LOGIN WITH EMAIL & VERIFY CODE
  elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE:
    return auth_by_verify_code(data)
  # 3. LOGIN WITH JWT
  elif req_type == AuthRequestType.LOGIN_WITH_JWT:
    logging.info(f"login by jwt: {data}")
    return auth_by_jwt(data)
  # 4. DELETE USER
  elif req_type == AuthRequestType.DELETE_USER:
    return del_user(data)

  raise HTTPException(status_code=400, detail="Unsupported request type")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9103)
