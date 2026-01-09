import datetime
import secrets
import jwt
from typing import Dict
from fastapi import FastAPI, HTTPException, Depends
from auth import AuthRequest, AuthResponse, AuthRequestType, JWTTokenData, AuthData
import logging

app = FastAPI(title="Auth Server")

# --- Configuration & Mock DB ---
SECRET_KEY = "your-secret-key-keep-it-safe"
ALGORITHM = "HS256"

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

# --- Handlers ---
@app.post("/auth", response_model=AuthResponse)
async def handle_auth(request: AuthRequest):
  req_type = request.request_type
  data = request.data

  # 1. SEND VERIFY CODE
  if req_type == AuthRequestType.SEND_VERIFY_CODE:
    # Generate 4-digit code
    code = "".join([str(secrets.randbelow(10)) for _ in range(4)])
    mock_db["verify_codes"][data.email] = code
    
    # NOTE: In a real app, you would call an SMTP/Email API here.
    logging.info(f">>> [EMAIL SENT] To: {data.email}, Code: {code}")
    
    return AuthResponse(
      request_type = AuthRequestType(req_type),
      code=0,
      msg=f"Verify code sent successfully (Mock: {code})",
      data=None
    )

  # 2. LOGIN WITH EMAIL & VERIFY CODE
  elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE:
    stored_code = mock_db["verify_codes"].get(data.email)
    
    # if not stored_code or stored_code != data.verify_code:
    #   return AuthResponse(request_type = AuthRequestType(req_type), code=1, msg="Invalid or expired code")

    # "Register" logic: Create user if they don't exist
    if data.email not in mock_db["users"]:
      mock_db["users"][data.email] = {"uid": str(data.device_id)}
    
    user = mock_db["users"][data.email]
    token = create_access_token(data.email, user["uid"])
    
    # Clear code after use
    # del mock_db["verify_codes"][data.email]

    return AuthResponse(
      request_type = AuthRequestType(req_type),
      code=0,
      msg="Login successful",
      data=JWTTokenData(
        uid=user["uid"],
        email=data.email,
        token=token,
        expire_days=7
      )
    )

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