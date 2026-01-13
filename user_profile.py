from typing import Dict, List, Tuple, Any, TypeVar, Optional
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
from enum import Enum
import datetime


# 创建一个泛型类型变量，用于 from_dict 方法
T = TypeVar('T')


class BaseResponse(BaseModel):
  """响应的基类"""
  status: str
  message: str = ""


class UserProfile(BaseModel):
  """用户画像信息"""
  uid: str
  uid_emb: List[float] = Field(default_factory=list)
  long_term_profile: List[Tuple[str, float]] = Field(default_factory=list)
  behaviors: Dict[str, List[Tuple[int, Any]]] = Field(
    default_factory=lambda: {
      "heart_rate": [], "blood_oxygen": [], "sleep_status": [],
      "clicks": [], "plays": []
    }
  )


# --- 请求类 ---
class QueryProfileRequest(BaseModel):
  uid: str
  action: str = "query_profile"


class UpdateProfileRequest(BaseModel):
  action: str = "update_profile"
  user_profile: UserProfile = Field(default_factory=UserProfile)


# --- 响应类 ---
class QueryProfileResponse(BaseResponse):
  profile: Optional[UserProfile] = None


class UpdateProfileResponse(BaseResponse):
  """结构和 BaseResponse 一样"""
  pass


class ErrorResponse(BaseResponse):
  status: str = "error"  # 默认状态为 error

