from typing import Dict, List, Tuple, Any, Optional
import time

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
  msg: str = ""


class UserProfile(BaseModel):
  """用户画像信息"""
  uid_emb: List[float] = Field(default_factory=list)
  basic_info: Optional[Dict[str, str]] = Field(default_factory=dict)
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


class ProfileData(BaseModel):
  uid: Optional[str] = Field(None, description="uid, just for debug")
  jwt_token: str | None = Field(None, description="JWT token，in wan should be fixed")
  user_profile: Optional[UserProfile] = Field(None, description="user profile")


class ProfileRequest(BaseModel):
  request_type: str = Field("query_profile", description="query| update")
  timestamp : int = Field(..., description="请求发送时间戳（秒级），必填")
  version : str = Field("1.0", description="version, needed, such as 1.0")
  data: ProfileData

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    missing_fields = []
    if self.data.jwt_token is None and self.data.uid is None:
      missing_fields.append("uid")
      missing_fields.append("jwt_token")
    if missing_fields:
      raise ValueError(
        f"request ：{missing_fields} must have one"
      )
    
    return self
  

# --- 响应类 ---
class ProfileResponse(BaseResponse):
  request_type: str = Field("query_profile", description="query| update")
  data: Optional[ProfileData]


class InvalidOrExpiredTokenResp(BaseResponse):
  code : int = 401
  msg : str = "token invalid or expired"

class InvalidReqFormatResp(BaseResponse):
  code : int = 400
  msg : str = "invalid request format"

if __name__ == "__main__":
  update_req = {
    "request_type": "update_profile",
    "timestamp": int(time.time()),
    "version" : "1.0",
    "data": {
      "uid": "client007",
      "jwt_token": "jwt_token_dummy",
      "user_profile":
      {
        "long_term_profile": [], 
        "behaviors": {
          "heart_rate": [
           ( 
                  1000000000,
                  78
           ),
           ( 
                  100000001,
                  80
           ) 
          ],  
          "blood_oxygen": [], 
          "sleep_status": [], 
          "clicks": [
            ( 
                  1000000000,
                  "product_page_1"
            ),
            ( 
                  1000000001,
                  "checkout_button"
            ) 
          ],  
          "plays": []
        },
        "mindora_record":
        {
          "sleep.scene.cocos_island_moonlight": [(100000, 600)], 
          "sleep.scene.amalfi_breeze": [(1000001, 600)],
          "sleep.scene.kyoto_forest": [],
          "sleep.scene.andaman_rainforest_sanctuary": [],
          "sleep.scene.bhutan_misty_forest": [],
          "sleep.scene.sedona_red_rock_peace" : [],
          "sleep.scene.fogo_island_cookie_box": [],
          "sleep.scene.seychelles_moonlight_lullaby": []
        }
      }
    }
  }

  try:
    req = ProfileRequest(** update_req)
    print(f"succ for {req}")
  except ValidationError as e:
    print("\n❌ 测试（user profile）失败：", e.errors()[0]["msg"])

  try:
    req2 = ProfileRequest.model_validate(update_req)
    print(f"succ for {req2}")
  except ValidationError as e:
    print("\n❌ 测试（user profile）失败：", e.errors()[0]["msg"])


  query = {
    "request_type": "query_profile",
    "timestamp": int(time.time()),
    "version" : "1.0",
    "data": {
      "uid": "client007",
      "jwt_token": "jwt_token_dummy",
    }
  }

  try:
    req = ProfileRequest(** query)
    req2 = ProfileRequest.model_validate(query)
    print(f"succ query for {req} and {req2}")
  except ValidationError as e:
    print("\n❌ 测试（query profile）失败：", e.errors()[0]["msg"])
