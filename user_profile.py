from dataclasses import dataclass, asdict, field
from typing import Dict, List, Tuple, Any, Type, TypeVar

# 创建一个泛型类型变量，用于 from_dict 方法
T = TypeVar('T')

@dataclass
class BaseResponse:
  """响应的基类"""
  status: str
  message: str = ""

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    return cls(**data)

@dataclass
class UserProfile:
  """用户画像信息"""
  uid: str
  uid_emb : List[float] = field(default_factory=list)
  long_term_profile: List[Tuple[str, float]] = field(default_factory=list)
  behaviors: Dict[str, List[Tuple[int, Any]]] = field(default_factory=lambda: {
    "heart_rate": [], "blood_oxygen": [], "sleep_status": [],
    "clicks": [], "plays": []
  })

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

  @staticmethod
  def from_dict(data: Dict[str, Any]) -> 'UserProfile':
    return UserProfile(**data)

# --- 请求类 ---

@dataclass
class QueryProfileRequest:
  uid: str
  action: str = "query_profile"

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

  @staticmethod
  def from_dict(data: Dict[str, Any]) -> 'QueryProfileRequest':
    return QueryProfileRequest(uid=data["uid"])

@dataclass
class UpdateProfileRequest:
  uid: str
  action: str = "update_profile"
  uid_emb: List[float] = field(default_factory=list)
  long_term_profile: List[Tuple[str, float]] = field(default_factory=list)
  behaviors: Dict[str, List[Tuple[int, Any]]] = field(default_factory = dict)

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

  @staticmethod
  def from_dict(data: Dict[str, Any]) -> 'UpdateProfileRequest':
    return UpdateProfileRequest(uid=data["uid"], behaviors=data["behaviors"], long_term_profile=data["long_term_profile"])
    
# --- 响应类 ---

@dataclass
class QueryProfileResponse(BaseResponse):
  profile: UserProfile = None

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> 'QueryProfileResponse':
    profile_data = data.get("profile")
    profile_obj = UserProfile.from_dict(profile_data) if profile_data else None
    return cls(status=data["status"], message=data.get("message", ""), profile=profile_obj)

@dataclass
class UpdateProfileResponse(BaseResponse):
  pass # 结构和 BaseResponse 一样

@dataclass
class ErrorResponse(BaseResponse):
  status: str = "error" # 默认状态为 error