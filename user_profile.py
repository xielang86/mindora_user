from typing import Dict, List, Tuple, Any, Optional
from pydantic import (
    BaseModel,
    Field,
    model_validator,
    field_validator,
    ValidationError
)


class BaseResponse(BaseModel):
  """响应的基类"""
  code: int = 0
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
  mindora_record: Dict[str, List[Tuple[Any, Any]]] = Field(
    default_factory=lambda: {
      "sleep.scene.cocos_island_moonlight": [], 
      "sleep.scene.amalfi_breeze": [],
      "sleep.scene.kyoto_forest": [],
      "sleep.scene.andaman_rainforest_sanctuary": [],
      "sleep.scene.bhutan_misty_forest": [],
      "sleep.scene.sedona_red_rock_peace" : [],
      "sleep.scene.fogo_island_cookie_box": [],
      "sleep.scene.seychelles_moonlight_lullaby": []
    }
  )


# --- 请求类 ---
class QueryProfileRequest(BaseModel):
  uid: str
  jwt_token: str | None = Field(None, description="JWT token，in wan should be fixed")
  action: str = "query_profile"

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    missing_fields = []
    if self.jwt_token is None and self.uid is None:
      missing_fields.append("uid")
      missing_fields.append("jwt_token")
    if missing_fields:
      raise ValueError(
        f"request ：{missing_fields} must have one"
      )
    
    return self


class UpdateProfileRequest(BaseModel):
  action: str = "update_profile"
  jwt_token: str | None = Field(None, description="JWT token，in wan should be fixed")
  user_profile: UserProfile = Field(default_factory=UserProfile)

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    missing_fields = []
    if self.jwt_token is None and self.user_profile.uid is None:
      missing_fields.append("user_profile.uid")
      missing_fields.append("jwt_token")
    if missing_fields:
      raise ValueError(
        f"request ：{missing_fields} must have one"
      )
    return self

# --- 响应类 ---
class QueryProfileResponse(BaseResponse):
  profile: Optional[UserProfile] = None


class UpdateProfileResponse(BaseResponse):
  """结构和 BaseResponse 一样"""
  pass

class InvalidOrExpiredTokenResp(BaseResponse):
  code : int = 401
  message : str = "token invalid or expired"

class InvalidReqFormatResp(BaseResponse):
  code : int = 400
  message : str = "invalid request format"

if __name__ == "__main__":
  update_req = {
    "action": "update_profile",
    "user_profile":
    {
    "uid": "client007",
    "long_term_profile": [], 
    "behaviors": {
      "heart_rate": [
        [
              1000000000,
              78
        ],
        [
              100000001,
              80
        ]
      ],  
      "blood_oxygen": [], 
      "sleep_status": [], 
      "clicks": [
         [
              1000000000,
              "product_page_1"
         ],
         [
              1000000001,
              "checkout_button"
         ]
      ],  
      "plays": []
    }   
  }
  }

  try:
    req = UpdateProfileRequest(** update_req)
  except ValidationError as e:
    print("\n❌ 测试（user profile）失败：", e.errors()[0]["msg"])

  try:
    req2 = UpdateProfileRequest.model_validate(update_req)
  except ValidationError as e:
    print("\n❌ 测试（user profile）失败：", e.errors()[0]["msg"])

  print(f"succ for {req}")


  query = {'uid': 'client007', 'action': 'query_profile'}
  try:
    req = QueryProfileRequest(** query)
    req2 = QueryProfileRequest.model_validate(query)
  except ValidationError as e:
    print("\n❌ 测试（query profile）失败：", e.errors()[0]["msg"])

  print(f"succ query for {req}")

