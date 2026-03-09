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


#  一级指标：
# 睡眠得分(sleep_score)： 入睡效率， 睡眠结构, 夜间波动
# 场景偏好(scene_preference)：场景排序
# 睡眠建议(sleep_advice)：建议，文章列表
# 
# 
# 二级指标：
# 入睡效率:  首次入睡时间，入睡前心率，入睡前呼吸频率
# 睡眠结构： 快速动眼睛睡眠，核心睡眠，深度睡眠
# 夜间波动：夜间觉醒，觉醒时长，觉醒类型，心率波动，呼吸波动
# 
# 三级（苹果健康直接有，后继我们会选择部分自己算）：
# 首次入睡时间，入睡前心率，入睡前呼吸频率，快速动眼睛睡眠，核心睡眠，深度睡眠，夜间觉醒，觉醒时长，觉醒类型，心率波动，呼吸波动, 体温
# 
# 时间维度统计：
# 睡眠得分（week， month）： avg, 序列
# 睡眠时长(week， month)： 序列(by day), sum
# 睡眠趋势（week， month）：模型分析
# Onset efficiency（week， month）： 场景次数(sum)
# Time in bed： avg
# Heart rate: avg

class SleepResult(BaseModel):
  timestamp: int = Field(..., description="数据记录的时间戳（秒级）")
  sleep_score: Optional[float] = Field(None, description="睡眠得分，范围0-100") 
  fall_sleep_efficiency: Optional[float] = Field(None, description="入睡效率，范围0-100")
  sleep_struct: Optional[float] = Field(None, description="睡眠结构，包含快速动眼、核心、深度睡眠占比，单位%")
  sleep_var: Optional[float] = Field(None, description="夜间波动，包含觉醒次数、觉醒时长、心率波动等，单位%")

  first_sleep_time: Optional[int] = Field(None, description="首次入睡时间，单位分钟")
  heart_rate_before_sleep: Optional[float] = Field(None, description="入睡前心率，单位bpm")
  bpm_before_sleep: Optional[float] = Field(None, description="入睡前呼吸频率，单位次/分钟")

  rem_sleep_duration: Optional[float] = Field(None, description="fast eye movement sleep 单位分钟")
  core_sleep_duration_core: Optional[float] = Field(None, description="core sleep 单位分钟")
  deep_sleep_duration: Optional[float] = Field(None, description="deep sleep sleep 单位分钟")

  night_awake_times: Optional[int] = Field(None, description="夜间觉醒次数")
  night_awake_duration: Optional[int] = Field(None, description="夜间觉醒时长，单位分钟")
  night_awake_type: Optional[str] = Field(None, description="夜间觉醒类型，如自然觉醒、环境干扰、身体不适等")
  heart_rate_var: Optional[float] = Field(None, description="心率波动，单位bpm")
  respiratory_var: Optional[float] = Field(None, description="呼吸频率波动，单位次/分钟")
  
  time_in_bed: Optional[int] = Field(None, description="躺床时间，单位分钟")
  avg_heart_rate: Optional[float] = Field(None, description="平均心率，单位bpm")
  avg_respiratory: Optional[float] = Field(None, description="平均呼吸频率，单位次/分钟")
  avg_temperature: Optional[float] = Field(None, description="体温，单位摄氏度")

  scene_preference: List[Tuple[str, float]] = Field(None, description="场景偏好，如喜欢的睡眠场景名称")

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

  sleep_data:  List[Tuple[int, SleepResult]] = Field(default_factory=list)

  sleep_analysis: Dict[str, Any] = Field(
    default_factory=lambda: {
      "sleep_trend_week": "", 
      "sleep_trend_month": "",
      "scene": {"title":"", "music":"", "text":"", "image_url": ""},
      "sleep_advice": "",
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
