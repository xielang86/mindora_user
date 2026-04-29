from typing import Dict, List, Tuple, Any, Optional
import time

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    model_validator,
    field_validator,
    ValidationError
)


class BaseResponse(BaseModel):
  """响应的基类"""
  code: int = 0
  msg: str = ""

# -------------------------- 子模型定义（对应data下一级字段的嵌套结构） --------------------------
class UserAddress(BaseModel):
  """用户地址信息模型"""
  province: str = Field(..., description="省份")
  city: str = Field(..., description="城市")
  detail: str = Field(..., description="详细地址")
  zip_code: Optional[str] = Field(None, description="邮政编码")

class UserSocial(BaseModel):
  """用户社交账号信息模型"""
  wechat: Optional[str] = Field(None, description="微信号")
  phone: str = Field(..., description="手机号")
  email: Optional[EmailStr] = Field(None, description="邮箱")

class UserPreference(BaseModel):
  """用户偏好设置模型"""
  theme: str = Field(default="light", description="界面主题")
  language: str = Field(default="zh-CN", description="语言")
  notify: bool = Field(default=True, description="是否开启通知")

class Address(BaseModel):
  id: str = Field(..., description="地址唯一 ID")
  is_default: bool = Field(..., description="是否默认地址")
  region: str = Field(..., description="省市区或区域信息")
  detail: str = Field(..., description="详细地址")
  name: str = Field(..., description="收件人姓名")
  phone: str = Field(..., description="收件人电话")

class Profile(BaseModel):
  nickname: Optional[str] = Field("", description="昵称")
  gender: Optional[str] = Field("", description="性别展示值")
  age: Optional[str] = Field("", description="年龄展示值")
  birthday: Optional[str] = Field("", description="生日展示值，格式为 yyyy.MM.dd")
  email: Optional[str] = Field("", description="联系邮箱")
  phone: Optional[str] = Field("", description="联系电话")
  address_list: List[Address] = Field(default_factory=list, description="地址列表")
  avatar_base64: Optional[str] = Field("", description="头像的 Base64 内容")
  avatar_mime_type: Optional[str] = Field("image/jpeg", description="头像 MIME 类型")


class SleepElement(BaseModel):
  start_time: int = Field(..., description="睡眠阶段开始时间戳（秒级）")
  duration: float = Field(..., description="睡眠阶段持续时长，单位分钟")
  sleep_type: str = Field(..., description="睡眠阶段类型，如REM、core,deep,rem,awake")

class SleepResult(BaseModel):
  timestamp: int = Field(..., description="数据 update 时间戳（秒级）")
  sleep_quality: Optional[float] = Field(None, description="睡眠得分，范围0-100") 
  soe: Optional[float] = Field(None, description="入睡效率，范围0-100")
  sleep_arch_index: Optional[float] = Field(None, description="睡眠结构，包含快速动眼、核心、深度睡眠占比，单位%")
  night_var_index: Optional[float] = Field(None, description="夜间波动，包含觉醒次数、觉醒时长、心率波动等，单位%")

  first_sleep_time: Optional[str] = Field(None, description="首次入睡时间, 00:00")
  hr_before_sleep: Optional[float] = Field(None, description="入睡前心率，单位bpm")
  rr_before_sleep: Optional[float] = Field(None, description="入睡前呼吸频率，单位次/分钟")

  hrv: Optional[float] = Field(None, description="心率波动，单位bpm")
  respiratory_var: Optional[float] = Field(None, description="呼吸频率波动，单位次/分钟")

  avg_heart_rate: Optional[float] = Field(None, description="平均心率，单位bpm")
  avg_respiratory: Optional[float] = Field(None, description="平均呼吸频率，单位次/分钟")
  avg_temperature: Optional[float] = Field(None, description="体温，单位摄氏度")

  scene_preference: List[Tuple[str, float]] = Field(None, description="场景偏好，如喜欢的睡眠场景名称")
  # the recent sleep status sequence, with start_time, duration and sleep_type, used for sleep analysis and advice generation
  sleep_status: List[SleepElement] = Field(default_factory=list, description="the seq for the sleep status, with start_time, duration and sleep_type")

  @property
  def sequence_summaries(self):
    awake_types = {}
    for seq in self.sleep_status:
      if seq.sleep_type == "awake":
        awake_types[seq.sleep_type] = awake_types.get(seq.sleep_type, 0) + 1

    max_awake_type = max(awake_types, key=awake_types.get) if awake_types else None

    return {
      "rem_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "rem"),
      "core_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "core"),
      "deep_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "deep"),
      "night_awake_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "awake"),
      "night_awake_count": sum(1 for seq in self.sleep_status if seq.sleep_type == "awake"),
      "night_awake_type": max_awake_type,
      "time_in_bed": sum(seq.duration for seq in self.sleep_status) 
    }
  

class UserProfile(BaseModel):
  """用户画像信息"""
  uid_emb: List[float] = Field(default_factory=list)
  basic_info: Optional[Dict[str, str]] = Field(default_factory=dict)
  long_term_profile: List[Tuple[str, float]] = Field(default_factory=list)

  behaviors: Dict[str, List[Tuple[int, Any]]] = Field(
    default_factory=lambda: {
      # 生命体征
      "heart_rate": [], "blood_oxygen": [], "resting_heart_rate": [],
      "heart_rate_variability_sdnn": [], "respiratory_rate": [],
      "sleeping_wrist_temperature": [], "body_temperature": [],
      # 睡眠状态
      "sleep_status": [],
      "sleep_stage_deep": [], "sleep_stage_rem": [], "sleep_stage_light": [],
      # 交互行为
      "clicks": [], "plays": [],
    }
  )

  # only the recent 7 days sleep data will be returned to app, and used for sleep analysis and advice generation, such as the data of yestoday night
  sleep_data:  List[SleepResult] = Field(default_factory=list)

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

  profile: Optional[Profile] = None


class ProfileData(BaseModel):
  uid: Optional[str] = Field(None, description="uid, just for debug")
  jwt_token: str | None = Field(None, description="JWT token，in wan should be fixed")
  user_profile: Optional[UserProfile] = Field(None, description="user profile")


class ProfileRequest(BaseModel):
  request_type: str = Field("query_profile", description="query| update| analysis_overview| insight| daily_report| weekly_report| month_report")
  timestamp: int = Field(..., description="请求发送时间戳（秒级），必填")
  version: str = Field("1.0", description="version, needed, such as 1.0")
  data: ProfileData
  modules: Optional[List[str]] = Field(default_factory=list, description="Modules to include in the response")

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
  request_type: str = Field("query_profile", description="query| update| analysis_overview| insight| daily_report| weekly_report| month_report")
  data: Optional[Dict[str, Any]] = Field(None, description="Response data based on modules")


class InvalidOrExpiredTokenResp(BaseResponse):
  code : int = 401
  msg : str = "token invalid or expired"

class InvalidReqFormatResp(BaseResponse):
  code : int = 400
  msg : str = "invalid request format"

# -------------------------- 分析接口模型 --------------------------
class AnalysisData(BaseModel):
  uid: Optional[str] = Field(None, description="用户ID")
  jwt_token: Optional[str] = Field(None, description="JWT token")
  language: str = Field("en", description="语言代码，如 zh-Hans / en / ja")
  date: Optional[str] = Field(None, description="当前自然日 yyyy-MM-dd")
  timezone: str = Field("UTC", description="时区 ID，如 Asia/Shanghai")
  start_date: Optional[str] = Field(None, description="统计起始日期 yyyy-MM-dd")
  end_date: Optional[str] = Field(None, description="统计结束日期 yyyy-MM-dd")
  modules: List[str] = Field(default_factory=list, description="需要返回的模块列表")

class AnalysisRequest(BaseModel):
  request_type: str = Field(..., description="analysis_overview|analysis_sleep_day|analysis_sleep_week|analysis_sleep_month|analysis_explore")
  version: str = Field("1.0")
  timestamp: int = Field(..., description="请求时间戳（秒级）")
  data: AnalysisData

  @model_validator(mode='after')
  def validate_auth(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    return self

class AnalysisResponse(BaseResponse):
  request_type: str
  data: Optional[Dict[str, Any]] = None


# -------------------------- 睡眠分析与建议接口 --------------------------
class SleepAdviceData(BaseModel):
  """Payload for /sleep_advice requests."""
  uid: Optional[str] = Field(None, description="用户ID（debug/内网使用）")
  jwt_token: Optional[str] = Field(None, description="JWT token")
  language: str = Field("en", description="返回语言, e.g. zh-Hans / en / ja")
  date: Optional[str] = Field(None, description="目标日期 yyyy-MM-dd，默认最近一晚")
  timezone: str = Field("UTC", description="时区 ID, 如 Asia/Shanghai")
  # Optional thematic focus, e.g. ["deep_sleep", "onset"]; empty = full
  focus: List[str] = Field(default_factory=list, description="希望重点关注的维度")


class SleepAdviceRequest(BaseModel):
  """Request wrapper for the sleep-analysis-and-advice endpoint.

  The server uses the user's recent sleep_data + mindora_record to produce:
    * a free-form analysis paragraph (2-4 sentences)
    * a short list of personalised, actionable advice bullets
    * optional structured highlights (one-liner per pillar)
  """
  request_type: str = Field("sleep_analysis_advice",
                            description="必须为 sleep_analysis_advice")
  version: str = Field("1.0")
  timestamp: int = Field(..., description="请求时间戳（秒级）")
  data: SleepAdviceData

  @model_validator(mode='after')
  def validate_auth(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    if self.request_type != "sleep_analysis_advice":
      raise ValueError(
        f"request_type must be 'sleep_analysis_advice', got '{self.request_type}'"
      )
    return self


class SleepAdviceResult(BaseModel):
  """Structured LLM output for sleep analysis + advice."""
  analysis: str = Field("", description="LLM 生成的整体睡眠分析段落")
  advice: List[str] = Field(default_factory=list,
                            description="个性化、可执行的建议要点")
  highlights: Dict[str, str] = Field(
    default_factory=dict,
    description="按维度给出的一句话亮点, e.g. {'onset': '...', 'deep': '...'}"
  )
  date: Optional[str] = Field(None, description="分析对应的日期 yyyy-MM-dd")
  language: str = Field("en", description="回显请求的语言代码")
  llm_used: bool = Field(True, description="False 代表回退到默认文案")


class SleepAdviceResponse(BaseResponse):
  request_type: str = Field("sleep_analysis_advice")
  data: Optional[SleepAdviceResult] = None


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
