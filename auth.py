"""auth.py — 嵌入式设备精简版：仅保留 user_server /login 所需的数据结构。

相对完整版的裁剪：
  - 删除 UserData（MySQL 映射）、JWTTokenData、AuthResponse、UUIDEncoder、测试函数
    —— 以上只被 auth_server 使用，设备端不部署 auth_server
  - EmailStr → str：去掉 email-validator 这个 pip 依赖
  - normalize_user_level → 内联等级集合：去掉 common/user_rights.py 依赖
  - 删除被覆盖的第一个 model_config（死配置，pydantic 只生效最后一个）

协议保持与完整版一致：App 端发来的请求格式无需任何改动。
"""
from typing import Optional
from pydantic import (
    BaseModel,
    Field,
    model_validator,
    field_validator,
    ConfigDict,
)
from uuid import UUID
from enum import StrEnum
from datetime import datetime

# 用户等级合法值（与 common/user_rights.py 的等级保持一致）
VALID_USER_LEVELS = {"free", "pro", "premium"}


# 定义请求类型枚举（区分不同操作）
class AuthRequestType(StrEnum):
  SEND_VERIFY_CODE = "send_verify_code"                          # 发送邮箱验证码
  LOGIN_WITH_EMAIL_VERIFY_CODE = "login_with_email_verify_code"  # email+验证码登录/注册
  LOGIN_WITH_JWT = "login_with_jwt"                              # JWT令牌登录
  DELETE_USER = "delete_user"                                    # 旧：JWT直接删除（保留兼容）
  SEND_DELETE_VERIFY_CODE = "send_delete_verify_code"            # 登录后请求删除账号验证码
  DELETE_USER_WITH_CODE = "delete_user_with_code"                # 登录后提交验证码删除账号
  # ── Web site registration & login ─────────────────────────────────────────
  REGISTER_WITH_EMAIL_PASSWORD = "register_with_email_password"  # email+验证码+密码 注册
  LOGIN_WITH_EMAIL_PASSWORD = "login_with_email_password"        # email+密码 登录
  SEND_SMS_CODE = "send_sms_code"                                # 发送手机短信验证码
  REGISTER_WITH_PHONE = "register_with_phone"                    # 手机号+SMS验证码 注册
  LOGIN_WITH_PHONE_SMS = "login_with_phone_sms"                  # 手机号+SMS验证码 登录/自动注册
  WECHAT_CALLBACK = "wechat_callback"                            # 微信OAuth code换token
  REDEEM_REDEMPTION_CODE = "redeem_redemption_code"              # 兑换权益码
  GENERATE_REDEMPTION_CODES = "generate_redemption_codes"        # 生成权益码（后台）
  QUERY_USER_RIGHTS = "query_user_rights"                        # 查询用户权益

  def __str__(self):
    return self.value


class AuthData(BaseModel):
  """autho data model（merge send verify_code /login with email verify code/JWT login）- Pydantic v2"""
  # 可选字段（根据请求类型动态校验必填）
  email: str | None = Field(None, description="用户邮箱，send_verify code/login_with_email_verify_code 必填")
  device_id: UUID | None = Field(None, description="设备唯一标识（UUID格式），send_verify_code/login_with_email_verify_code 必填")
  verify_code: str | None = Field(None, description="4-6位数字验证码")
  jwt_token: str | None = Field(None, description="JWT登录令牌，login_with_jwt 必填")
  # ── Web site fields ─────────────────────────────────────────────────────
  phone: str | None = Field(None, description="手机号（11位中国大陆），register_with_phone/login_with_phone_sms 必填")
  password: str | None = Field(None, description="登录密码（>=8位），register_with_email_password/login_with_email_password 必填")
  wechat_code: str | None = Field(None, description="微信OAuth code，wechat_callback 必填")
  state: str | None = Field(None, description="微信OAuth state")
  redemption_code: str | None = Field(None, description="兑换码，redeem_redemption_code 必填")
  batch_id: str | None = Field(None, description="兑换码批次号，generate_redemption_codes 必填")
  target_level: str | None = Field(None, description="目标用户等级，例如 free/pro/premium")
  duration_days: int | None = Field(None, description="兑换后持续天数")
  quantity: int | None = Field(None, description="生成兑换码数量")
  code_expire_at: datetime | None = Field(None, description="兑换码自身过期时间")
  admin_secret: str | None = Field(None, description="后台生成兑换码口令")

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

  @field_validator("redemption_code", "batch_id", "admin_secret")
  def check_optional_non_empty_text(cls, v):
    if v is not None:
      value = v.strip()
      if not value:
        raise ValueError("field cannot be blank")
      return value
    return v

  @field_validator("target_level")
  def check_target_level(cls, v):
    if v is None:
      return v
    normalized = v.strip().lower()
    if normalized not in VALID_USER_LEVELS:
      raise ValueError("target_level must be one of: free, pro, premium")
    return normalized

  @field_validator("duration_days", "quantity")
  def check_positive_int(cls, v):
    if v is not None and v <= 0:
      raise ValueError("must be greater than 0")
    return v

  model_config = ConfigDict(use_enum_values=True)


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
    elif req_type == AuthRequestType.LOGIN_WITH_JWT:
      if data.jwt_token is None:
        raise ValueError(
          f"request_type={req_type}时，data.jwt_token必填"
        )

    # 场景3.1：登录后请求删除账号验证码
    elif req_type == AuthRequestType.SEND_DELETE_VERIFY_CODE:
      if data.jwt_token is None:
        raise ValueError(
          f"request_type={req_type}时，data.jwt_token必填"
        )

    # 场景3.2：登录后提交验证码删除账号
    elif req_type == AuthRequestType.DELETE_USER_WITH_CODE:
      missing = [f for f in ["jwt_token", "verify_code"] if getattr(data, f) is None]
      if missing:
        raise ValueError(
          f"request_type={req_type}时，data中以下字段必填：{missing}"
        )

    # 场景3.3：旧版JWT直接删除（保留兼容）
    elif req_type == AuthRequestType.DELETE_USER:
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

    elif req_type == AuthRequestType.REDEEM_REDEMPTION_CODE:
      missing = [f for f in ["jwt_token", "redemption_code"] if getattr(data, f) is None]
      if missing:
        raise ValueError(f"request_type={req_type}时，data中以下字段必填：{missing}")

    elif req_type == AuthRequestType.GENERATE_REDEMPTION_CODES:
      missing = [f for f in ["admin_secret", "batch_id", "target_level", "duration_days", "quantity"] if getattr(data, f) is None]
      if missing:
        raise ValueError(f"request_type={req_type}时，data中以下字段必填：{missing}")

    elif req_type == AuthRequestType.QUERY_USER_RIGHTS:
      if data.jwt_token is None:
        raise ValueError(f"request_type={req_type}时，data.jwt_token必填")

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

  # 注意：完整版这里有两段 model_config，pydantic 只生效最后一段，
  # 本精简版直接保留生效的那份（非 strict、允许额外字段、枚举存值）。
  model_config = ConfigDict(use_enum_values=True)
