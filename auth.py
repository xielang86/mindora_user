from typing import Optional
from pydantic import (
    BaseModel,
    Field,
    EmailStr,
    model_validator,
    field_validator,
    ConfigDict,
    ValidationError
)
from uuid import UUID
from enum import Enum,StrEnum
from datetime import datetime
import json

class UserData(BaseModel):
  # 核心字段：与MySQL字段名一致，指定类型
  uid: str
  email: str
  salt: str
  # 可选字段：MySQL中允许为空/有默认值的字段
  status: Optional[int] = Field(default=1)  # 默认值匹配MySQL的DEFAULT 1
  device_list: str
  # 时间字段：自动将MySQL返回的字符串/ datetime 对象转为 datetime 类型
  register_time: datetime
  update_time: Optional[datetime] = None

  # 【可选】字段名映射（若MySQL是下划线，想返回驼峰JSON）
  model_config = {
      "alias_generator": lambda x: x.replace("_", " ").title().replace(" ", ""),  # 下划线转驼峰
      "populate_by_name": True  # 允许通过原字段名（如register_time）赋值
  }

# 定义请求类型枚举（区分不同操作）
class AuthRequestType(StrEnum):
  SEND_VERIFY_CODE = "send_verify_code"                          # 发送邮箱验证码
  LOGIN_WITH_EMAIL_VERIFY_CODE = "login_with_email_verify_code"  # email+验证码登录/注册
  LOGIN_WITH_JWT = "login_with_jwt"                              # JWT令牌登录
  DELETE_USER = "delete_user"
  # ── Web site registration & login ─────────────────────────────────────────
  REGISTER_WITH_EMAIL_PASSWORD = "register_with_email_password"  # email+验证码+密码 注册
  LOGIN_WITH_EMAIL_PASSWORD = "login_with_email_password"        # email+密码 登录
  SEND_SMS_CODE = "send_sms_code"                                # 发送手机短信验证码
  REGISTER_WITH_PHONE = "register_with_phone"                    # 手机号+SMS验证码 注册
  LOGIN_WITH_PHONE_SMS = "login_with_phone_sms"                  # 手机号+SMS验证码 登录/自动注册
  WECHAT_CALLBACK = "wechat_callback"                            # 微信OAuth code换token

  def __str__(self):
    return self.value

class AuthData(BaseModel):
  """autho data model（merge send verify_code /login with email verify code/JWT login）- Pydantic v2"""
  # 可选字段（根据请求类型动态校验必填）
  email: EmailStr | None = Field(None, description="用户邮箱，send_verify code/login_with_email_verify_code 必填")
  device_id: UUID | None = Field(None, description="设备唯一标识（UUID格式），send_verify_code/login_with_email_verify_code 必填")
  verify_code: str | None = Field(None, description="4-6位数字验证码")
  jwt_token: str | None = Field(None, description="JWT登录令牌，login_with_jwt 必填")
  # ── Web site fields ─────────────────────────────────────────────────────
  phone: str | None = Field(None, description="手机号（11位中国大陆），register_with_phone/login_with_phone_sms 必填")
  password: str | None = Field(None, description="登录密码（>=8位），register_with_email_password/login_with_email_password 必填")
  wechat_code: str | None = Field(None, description="微信OAuth code，wechat_callback 必填")
  state: str | None = Field(None, description="微信OAuth state")

  @field_validator("verify_code")
  def check_verify_code_format(cls, v):
    if v is not None and not (v.isdigit() and 4 <= len(v) <= 6):
      raise ValueError("verify code must be 4-6 digits")
    return v

  @field_validator("phone")
  def check_phone_format(cls, v):
    import re
    if v is not None and not re.fullmatch(r"1[3-9]\d{9}", v.strip()):
      raise ValueError("phone must be 11-digit mainland China mobile number")
    return v.strip() if v else v

  @field_validator("password")
  def check_password(cls, v):
    import re
    if v is not None:
      if len(v) < 8:
        raise ValueError("password must be at least 8 characters")
      if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError("password must contain letters and digits")
    return v

  @field_validator("jwt_token")
  def check_jwt_token_not_blank(cls, v):
    if v is not None and v.strip() == "":
      raise ValueError("JWT token empty")
    return v

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra = {
      "examples": {
        "send_verify_code": {
          "email": "user@example.com",
          "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
        },
        "login_with_email_verify_code": {
          "email": "user@example.com",
          "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
          "verify_code": "1234"
        },
        "login_with_jwt": {
          "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        },

        "delete_user": {
          "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        }
      }
    }
  )

# 自定义JSON编码器：处理UUID类型
class UUIDEncoder(json.JSONEncoder):
  def default(self, obj):
    if isinstance(obj, uuid.UUID):
      # 将UUID对象转为字符串
      return str(obj)
    # 其他类型按默认逻辑处理
    return super().default(obj)


class AuthRequest(BaseModel):
  """auth request model（merge send verify_code /email login/JWT login）- Pydantic v2"""
  # 核心：请求类型，用于区分不同操作
  request_type: AuthRequestType = Field(..., description="认证请求类型：send_verify_code/login_with_email_verify_code/login_with_jwt")
  
  timestamp : int = Field(..., description="请求发送时间戳（秒级），必填")
  version : str = Field("1.0", description="version, needed, such as 1.0")
  data: AuthData = Field(..., description="AuthData, needed")

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    """根据请求类型校验AuthData的必填字段"""
    req_type = self.request_type
    data = self.data

    # 场景1：发送验证码（send_verify_code）
    if req_type == AuthRequestType.SEND_VERIFY_CODE:
      missing_fields = []
      if data.email is None:
        missing_fields.append("email")
      if data.device_id is None:
        missing_fields.append("device_id")
      if missing_fields:
        raise ValueError(
          f"request_type={req_type}时，data中以下字段必填：{missing_fields}"
        )
      # 该场景下，verify_code/jwt_token必须为None（可选校验，避免脏数据）
      if data.verify_code is not None or data.jwt_token is not None:
        raise ValueError(
          f"request_type={req_type}时，data.verify_code/data.jwt_token必须为None"
        )

    # 场景2：邮箱验证码登录（login_with_email_verify_code）
    elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE:
      missing_fields = []
      if data.email is None:
        missing_fields.append("email")
      if data.device_id is None:
        missing_fields.append("device_id")
      if data.verify_code is None:
        missing_fields.append("verify_code")
      if missing_fields:
        raise ValueError(
          f"request_type={req_type}时，data中以下字段必填：{missing_fields}"
        )
      # 该场景下，jwt_token必须为None
      if data.jwt_token is not None:
        raise ValueError(
          f"request_type={req_type}时，data.jwt_token必须为None"
        )

    # 场景3：JWT登录（login_with_jwt）
    elif req_type == AuthRequestType.LOGIN_WITH_JWT or req_type == AuthRequestType.DELETE_USER:
      if data.jwt_token is None:
        raise ValueError(
          f"request_type={req_type}时，data.jwt_token必填"
        )

    # ── Web site scenarios ────────────────────────────────────────────────
    elif req_type == AuthRequestType.REGISTER_WITH_EMAIL_PASSWORD:
      missing = [f for f in ["email", "verify_code", "password"] if getattr(data, f) is None]
      if missing:
        raise ValueError(f"request_type={req_type}时，data中以下字段必填：{missing}")

    elif req_type == AuthRequestType.LOGIN_WITH_EMAIL_PASSWORD:
      missing = [f for f in ["email", "password"] if getattr(data, f) is None]
      if missing:
        raise ValueError(f"request_type={req_type}时，data中以下字段必填：{missing}")

    elif req_type == AuthRequestType.SEND_SMS_CODE:
      if data.phone is None:
        raise ValueError(f"request_type={req_type}时，phone必填")

    elif req_type in (AuthRequestType.REGISTER_WITH_PHONE, AuthRequestType.LOGIN_WITH_PHONE_SMS):
      missing = [f for f in ["phone", "verify_code"] if getattr(data, f) is None]
      if missing:
        raise ValueError(f"request_type={req_type}时，data中以下字段必填：{missing}")

    elif req_type == AuthRequestType.WECHAT_CALLBACK:
      if data.wechat_code is None:
        raise ValueError(f"request_type={req_type}时，wechat_code必填")

    return self

  # mode='after'：所有字段基础校验完成后，再执行该校验（对应原 skip_on_failure=True）
  @field_validator("timestamp")
  def check_timestamp_valid(cls, v):
    if v is not None:
      if not isinstance(v, int) or v <= 0:
        raise ValueError("timestamp must be positive number in secs")
      current_ts = int(datetime.now().timestamp())
      if v > current_ts + 120 or v < current_ts - 120:
        raise ValueError(f"timestamp eror; currrent timestamp：{current_ts} and v = {v}）")
    return v

  model_config = ConfigDict(
    strict=True,        # 严格类型检查（如int不能自动转str）
    extra="forbid",     # 禁止传入模型未定义的字段
    use_enum_values=False # 保留枚举对象，而非字符串值（便于判断）
  )

  model_config = ConfigDict(
    use_enum_values=True,
    json_schema_extra = {
      "examples": {
        "send_verify_code": {
          "request_type": "send_verify_code",
          "version": "1.0",
          "timestamp": 1735689600,
          "data": {
            "email": "user@example.com",
            "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
          }
        },

        "login_with_email_verify_code": {
          "request_type": "login_with_email_verify_code",
          "version": "1.0",
          "timestamp": 1735689600,
          "data": {
            "email": "user@example.com",
            "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
            "verify_code": "1234"
          }
        },

        "login_with_jwt": {
          "request_type": "login_with_jwt",
          "version": "1.0",
          "timestamp": 1735689600,
          "data" : {
            "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
          }
        }
      }
    }
  )

class JWTTokenData(BaseModel):
  uid: str = Field(..., description="用户唯一ID，必填，非空字符串")
  email: EmailStr = Field(..., description="用户邮箱，必填，符合邮箱格式，非空")
  token: str = Field(..., description="JWT Token，必填，非空字符串")
  expire_days: int = Field(..., description="Token过期天数，必填，必须大于0")

  @field_validator("uid", "token")
  def validate_non_empty_string(cls, v):
    """验证uid/token为非空字符串"""
    # 先判断是否是字符串（防止传入非字符串类型）
    if not isinstance(v, str):
      raise ValueError(f"必须是字符串类型，当前类型：{type(v)}")
    # 去除首尾空格后判断是否为空（避免全空格的无效值）
    if not v.strip():
      raise ValueError("不能为空字符串（也不能全是空格）")
    return v.strip()  # 可选：返回去空格后的值，保证数据整洁

  @field_validator("expire_days")
  def validate_expire_days_positive(cls, v):
    # 先判断是否是数值类型
    if not isinstance(v, (int)):
      raise ValueError(f"必须是整数，当前类型：{type(v)}")
    # 验证大于0
    if v <= 0:
      raise ValueError(f"过期天数必须大于0，当前值：{v}")
    return v

  # 模型配置：开启枚举值、关闭额外字段等（通用最佳实践）
  model_config = ConfigDict(
    strict=True,  # 严格类型检查（比如不允许int自动转str）
    extra="forbid"  # 禁止传入模型未定义的字段，避免脏数据
  )
  
class AuthResponse(BaseModel):
  """auth request model（merge send verify_code /email login/JWT login）- Pydantic v2"""
  # 核心：请求类型，用于区分不同操作
  # request_type: AuthRequestType = Field(..., description="认证请求类型：send_verify_code/login_with_email_verify_code/login_with_jwt")
  request_type: str = Field(..., description="认证请求类型：send_verify_code/login_with_email_verify_code/login_with_jwt")
  code: int = Field(0, description="响应状态码：0=成功，1： 验证码错误/过期,2： jwt token 过期,3 : 请求发送过于频繁")
  # 响应提示信息（默认空字符串）
  msg: str = Field("", description="响应提示信息")
  data: Optional[JWTTokenData] = Field(None, description="响应数据：login_with_email_verify_code 时必须非空")

  @model_validator(mode='after')
  def validate_data_when_email_login(self):
    if self.request_type == AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE and self.data is None:
      raise ValueError(
        f"request_type={self.request_type.value}时，data字段必须不为None"
      )
    return self

  model_config = ConfigDict(
    strict=True,        # 严格类型检查（如int不能自动转str）
    extra="forbid",     # 禁止传入模型未定义的字段
    use_enum_values=True# 保留枚举对象，而非字符串值（便于判断）
  )

def test_request(): 
  ts = int(datetime.now().timestamp())
  # 测试1：合法场景 - send_verify_code（email+device_id必填）
  valid_send_code = {
    "request_type": "send_verify_code",
    "data": {
      "email": "user@example.com",
      "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
    },
    "timestamp": ts
  }
  try:
    req1 = AuthRequest(** valid_send_code)
    print("✅ 测试1（send_verify_code）通过：", req1.model_dump())
  except ValidationError as e:
    print("❌ 测试1失败：", e)

  # 测试2：合法场景 - login_with_email_verify_code（email+device_id+verify_code必填）
  valid_email_login = {
    "request_type": "login_with_email_verify_code",
    "data": {
      "email": "user@example.com",
      "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
      "verify_code": "1234"
    },
    "timestamp": ts
  }
  try:
    req2 = AuthRequest(** valid_email_login)
    print("\n✅ 测试2（login_with_email_verify_code）通过：", req2.model_dump())
  except ValidationError as e:
    print("❌ 测试2失败：", e)

  # 测试3：合法场景 - login_with_jwt（jwt_token必填）
  valid_jwt_login = {
    "request_type": "login_with_jwt",
    "data": {
      "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    },
    "timestamp": ts
  }
  try:
    req3 = AuthRequest(** valid_jwt_login)
    print("\n✅ 测试3（login_with_jwt）通过：", req3.model_dump())
  except ValidationError as e:
    print("❌ 测试3失败：", e)

  # 测试4：非法场景 - send_verify_code缺少device_id
  invalid_send_code = {
    "request_type": "send_verify_code",
    "data": {
      "email": "user@example.com"
      # 缺少device_id
    },
    "timestamp": ts
  }
  try:
    req4 = AuthRequest(** invalid_send_code)
  except ValidationError as e:
    print("\n❌ 测试4（send_verify_code缺device_id）失败（符合预期）：", e.errors()[0]["msg"])

  # 测试5：非法场景 - login_with_email_verify_code验证码非4位
  invalid_verify_code = {
    "request_type": "login_with_email_verify_code",
    "data": {
      "email": "user@example.com",
      "device_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
      "verify_code": "12345"  # 5位数字
    },
    "timestamp": ts
  }
  try:
    req5 = AuthRequest(** invalid_verify_code)
  except ValidationError as e:
    print("\n❌ 测试5（验证码非4位）失败（符合预期）：", e.errors()[0]["msg"])

  # 测试6：非法场景 - login_with_jwt传入email
  invalid_jwt_login = {
    "request_type": "login_with_jwt",
    "data": {
      "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
      "email": "user@example.com"  # 该场景不允许传email
    },
    "timestamp": ts
  }
  try:
    req6 = AuthRequest(** invalid_jwt_login)
  except ValidationError as e:
    print("\n❌ 测试6（jwt登录传email）失败（符合预期）：", e.errors()[0]["msg"])


def test_response():
  # response 测试1：合法场景 - login_with_email_verfiy_code 且 data 非空（通过）
  valid_email_login = {
    "request_type": AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE,
    "code": 0,
    "msg": "登录成功",
    "data": {
        "uid": "123456",
        "email": "user@example.com",
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "expire_days": 7
    }
  }
  try:
    resp1 = AuthResponse(** valid_email_login)
    print("✅ response 测试1（email_login+data非空）：验证通过")
    print("响应数据：", resp1.model_dump())
  except ValidationError as e:
    print("❌ response 测试1失败：", e)

  # response 测试2：非法场景 - email_login 但 data=None（失败）
  invalid_email_login = {
    "request_type": AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE,
    "code": 0,
    "msg": "登录成功",
    "data": None
  }
  try:
    resp2 = AuthResponse(** invalid_email_login)
  except ValidationError as e:
    print("\n❌ response 测试2（email_login+data=None）：验证失败（符合预期）")
    print("错误信息：", e.errors()[0]["msg"])

  # response 测试3：合法场景 - send_verify_code 且 data=None（通过）
  valid_send_code = {
    "request_type": AuthRequestType.SEND_VERIFY_CODE,
    "code": 0,
    "msg": "验证码发送成功",
    "data": None
  }
  try:
    resp3 = AuthResponse(** valid_send_code)
    print("\n✅ response 测试3（send_verify_code+data=None）：验证通过")
  except ValidationError as e:
    print("❌ response 测试3失败：", e)

  # response 测试4：非法场景 - email_login 但 data.token 为空（失败）
  invalid_email_login_token = {
    "request_type": AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE,
    "code": 0,
    "msg": "登录成功",
    "data": {
      "uid": "123456",
      "email": "user@example.com",
      "token": "",  # 空token
      "expire_days": 7
    }
  }
  try:
    resp4 = AuthResponse(** invalid_email_login_token)
  except ValidationError as e:
    print("\n❌ response 测试4（email_login+data.token为空）：验证失败（符合预期）")
    print("错误信息：", e.errors()[0]["msg"])

if __name__ == "__main__":
  test_request()
  test_response()
