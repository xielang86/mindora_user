import datetime
import secrets
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
from db.mysql_db import insert_user, get_user_by_email_or_uid
from db.redis_db import get_verify_code, set_jwt_token

app = FastAPI(title="Auth Server")


# Mock database for demonstration
# In production, use Redis for codes and PostgreSQL/Mongo for users
mock_db = {
  "verify_codes": {},  # {email: "1234"}
  "users": {}          # {email: {"uid": "uuid", "created_at": "..."}}
}

def create_access_token(email: str, uid: str, expires_delta: int = 7) -> str:
  """Generates a JWT token."""
  expire = datetime.datetime.utcnow() + datetime.timedelta(days=expires_delta)
  to_encode = {
    "sub": email,
    "uid": uid,
    "exp": expire
  }
  return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_access_token(token: str):
  """Decodes and validates the JWT token."""
  try:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    return payload
  except jwt.ExpiredSignatureError:
    raise HTTPException(status_code=401, detail="Token expired")
  except jwt.InvalidTokenError:
    raise HTTPException(status_code=401, detail="Invalid token")

def send_verify_code_handler(data: AuthData):
  MY_163_EMAIL = "mindora2026@163.com"
  MY_163_AUTH_CODE = "RZkiYNHsVxLGvVHG"  # deadline=20260412

  # Generate 4-digit code
  verify_code = generate_verify_code(4)
  resp = AuthResponse(
    request_type = AuthRequestType(AuthRequestType.SEND_VERIFY_CODE),
    code=0,
    msg=f"Verify code sent successfully (Mock: {verify_code})",
    data=None
  )

  if Config.Mode == 1:
    mock_db["verify_codes"][data.email] = verify_code
  else:
    status_data = send_verify_code_via_163(MY_163_EMAIL, MY_163_AUTH_CODE, data.email, verify_code)
    resp.code = status_data.get("code")
    resp.msg = status_data.get("msg")
    resp.data = status_data.get("data")

  logging.info(f">>> [EMAIL SENT] To: {data.email}, Code: {verify_code}, return {resp}")
  return resp


# 加载配置
load_dotenv()
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS"))
VERIFY_CODE_EXPIRE_SECONDS = int(os.getenv("VERIFY_CODE_EXPIRE_SECONDS"))


# ------------------- 核心函数：登录/注册一体化 -------------------
def auth_by_verify_code(email: str, device_id: str, verify_code: str) -> dict:
  """
  验证码登录/注册核心函数
  :param email: 用户邮箱
  :param device_id: 设备ID
  :param verify_code: 用户输入的验证码
  :return: {"code": 0/400/401, "msg": "", "data": {"uid": "", "jwt_token": ""}}
  """
  try:
      # 步骤1：校验参数
      if not all([email, device_id, verify_code]):
          return {"code": 400, "msg": "邮箱/设备ID/验证码不能为空", "data": None}
      
      # 步骤2：从Redis获取验证码并校验
      normalized_email = normalize_email(email)
      redis_code = get_verify_code(normalized_email, device_id)
      if not redis_code:
          return {"code": 401, "msg": "验证码已过期或不存在", "data": None}
      if redis_code != verify_code:
          return {"code": 401, "msg": "验证码错误", "data": None}
      
      # 步骤3：查询用户是否存在（新用户/旧用户分支）
      user = get_user_by_email(normalized_email)
      if not user:
          # 新用户：生成UID+插入数据库
          uid, salt = generate_uid_and_salt(normalized_email)
          insert_user(normalized_email, uid, salt)
          msg = "注册并登录成功"
      else:
          # 旧用户：获取已有UID
          uid = user["uid"]
          msg = "登录成功"
      
      # 步骤4：生成JWT Token
      expire_time = datetime.utcnow() + timedelta(seconds=JWT_EXPIRE_SECONDS)
      jwt_token = jwt.encode(
          {
              "uid": uid,
              "email": normalized_email,
              "exp": expire_time
          },
          JWT_SECRET_KEY,
          algorithm="HS256"
      )
      
      # 步骤5：存储JWT Token到Redis（和JWT过期时间一致）
      set_jwt_token(uid, device_id, jwt_token, JWT_EXPIRE_SECONDS)
      
      # 返回结果
      return {
          "code": 0,
          "msg": msg,
          "data": {
              "uid": uid,
              "jwt_token": jwt_token,
              "expire_seconds": JWT_EXPIRE_SECONDS
          }
      }
  
  except Exception as e:
      # 捕获所有异常，返回服务器错误
      return {"code": 500, "msg": f"服务器错误：{str(e)}", "data": None}

# ------------------- 测试示例 -------------------
if __name__ == "__main__":
    # 前提：先往Redis存入测试验证码（模拟发送验证码接口）
    from db_redis import redis_db
    test_email = "test@example.com"
    test_device_id = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
    test_code = "1234"
    # 存入验证码（5分钟过期）
    redis_db.set(f"verify_code:{test_email}:{test_device_id}", test_code, VERIFY_CODE_EXPIRE_SECONDS)
    
    # 测试登录/注册
    result = auth_by_verify_code(
        email=test_email,
        device_id=test_device_id,
        verify_code=test_code
    )
    print("操作结果：", result)

def login_with_email_verify_handler(data: AuthData):
  resp = AuthResponse(
    request_type = AuthRequestType(AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE),
    code=0,
    msg="Login successful",
    data=JWTTokenData(
      uid="uid",
      email=data.email,
      token="xxx",
      expire_days=7
    )
  )

  if Config.Mode == 1:
    return resp
  
  user = get_user_by_email_or_uid(data.email)
  if user == None:  #  new user, need to store in db
    user 

    stored_code = mock_db["verify_codes"].get(data.email)
    # "Register" logic: Create user if they don't exist
    if data.email not in mock_db["users"]:
      mock_db["users"][data.email] = {"uid": str(data.device_id)}
    
    user = mock_db["users"][data.email]
    token = create_access_token(data.email, user["uid"])
    return resp

# --- Handlers ---
@app.post("/auth", response_model=AuthResponse)
async def handle_auth(request: AuthRequest):
  req_type = request.request_type
  data = request.data

  # 1. SEND VERIFY CODE
  if req_type == AuthRequestType.SEND_VERIFY_CODE:
    return send_verify_code_handler(data)

  # 2. LOGIN WITH EMAIL & VERIFY CODE
  elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE:

  # 3. LOGIN WITH JWT
  elif req_type == AuthRequestType.LOGIN_WITH_JWT:
    payload = decode_access_token(data.jwt_token)
    email = payload.get("sub")
    uid = payload.get("uid")

    return AuthResponse(
      request_type = AuthRequestType(req_type),
      code=0,
      msg="Token is valid",
      data=JWTTokenData(
        uid=uid,
        email=email,
        token=data.jwt_token,
        expire_days=1 # Simplified
      )
    )

  # 4. DELETE USER
  elif req_type == AuthRequestType.DELETE:
    # For security, delete usually requires email or valid JWT. 
    # Here we use email for simplicity.
    if data.email in mock_db["users"]:
      del mock_db["users"][data.email]
      return AuthResponse(request_type=req_type, code=0, msg="User deleted")
    
    # return AuthResponse(request_type=AuthRequestType(req_type), code=1, msg="User not found")
    return AuthResponse(request_type=req_type, code=0, msg="User deleted")

  raise HTTPException(status_code=400, detail="Unsupported request type")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9103)