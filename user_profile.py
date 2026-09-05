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
# 播放统计（behaviors.plays → mindora_record）支持的内容 cmd 前缀：
# 引导式场景 SOP（sleep.scene.）+ 纯音乐（sleep.pure_music.；裸 pure_music. 兼容端上早期上报）。
# 推荐侧（llm/reco.py）仍只推引导式场景，不受此影响。
SCENE_CMD_PREFIXES = ("sleep.scene.", "sleep.pure_music.", "pure_music.")

def short_scene_id(scene_id: str) -> str:
  """播放 cmd（可带 SCENE_CMD_PREFIXES 前缀）→ 短 id，用于展示名与统计键归一。"""
  for prefix in SCENE_CMD_PREFIXES:
    if scene_id.startswith(prefix):
      return scene_id[len(prefix):]
  return scene_id


class Address(BaseModel):
  id: str = Field(..., description="地址唯一 ID")
  is_default: bool = Field(..., description="是否默认地址")
  region: str = Field(..., description="兼容字段：国家名+state+city+district 拼出的展示串，勿用于发货、勿解析")
  detail: str = Field(..., description="兼容字段：street1+street2+zip 拼出的展示串，勿用于发货")
  name: str = Field(..., description="收件人姓名")
  phone: str = Field(..., description="收件人电话，E.164 格式（带 + 与国际区号）")
  # 结构化字段（同步商城发货用；存量地址/老客户端可能缺失，全部选填）
  country_code: Optional[str] = Field(None, description="国家/地区，ISO 3166-1 alpha-2（US/CN/GB）")
  state_code: Optional[str] = Field(None, description="州/省代码：美/加两位字母码（CA），中国民政部数字省级码（33）")
  state: Optional[str] = Field(None, description="州/省显示名（面单打印值）")
  city: Optional[str] = Field(None, description="城市")
  district: Optional[str] = Field(None, description="区/县（仅中国等三级行政区划国家填）")
  street1: Optional[str] = Field(None, description="街道地址（门牌号 + 街道名）")
  street2: Optional[str] = Field(None, description="门牌/套间/单元，选填")
  zip: Optional[str] = Field(None, description="邮编（美国必填 \\d{5} 或 \\d{5}-\\d{4}；无邮编国家为空）")

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
  weight: Optional[float] = Field(None, description="体重，单位 kg")
  height: Optional[float] = Field(None, description="身高，单位 cm")
  language: str = Field("zh-CN", description="语言")
  religion: Optional[str] = Field("", description="宗教信仰")
  coacher: Optional[str] = Field("", description="导师/风格偏好")


# 睡眠健康问卷（onboarding/设置页收集的睡眠相关字段）
class SleepHealthProfile(BaseModel):
  sleep_quality: Optional[str] = Field(None, description="自评睡眠质量")
  health_problems: Optional[str] = Field(None, description="健康问题")
  weekly_sleep_duration: Optional[str] = Field(None, description="周睡眠时长")
  weekly_sleep_status: Optional[str] = Field(None, description="周睡眠状态")
  weekly_exercise_volume: Optional[str] = Field(None, description="周运动量")
  improvement_goal: Optional[str] = Field(None, description="睡眠改善目标")
  meditation_experience: Optional[str] = Field(None, description="冥想经验")
  stress_type: Optional[str] = Field(None, description="压力类型")
  medication_status: Optional[str] = Field(None, description="用药情况")


# 睡眠模式设置（对应 GUI/data/config 中的 profile.sleepMode）
class SleepModeProfile(BaseModel):
  is_smart: Optional[bool] = Field(None, description="是否开启智能睡眠模式")
  start_time: Optional[str] = Field(None, description="睡眠模式开始时间，如 22:00")
  end_time: Optional[str] = Field(None, description="睡眠模式结束时间，如 08:00")
  sleep_light: Optional[int] = Field(None, description="助眠灯光等级")
  screen_display: Optional[int] = Field(None, description="屏幕显示设置")
  essential_oil: Optional[int] = Field(None, description="精油/香氛设置")
  voice_guidance: Optional[int] = Field(None, description="语音引导设置")
  wake_light: Optional[int] = Field(None, description="唤醒灯光等级")


# 建议新增：环境与敏感度
class EnvironmentalSensitivity(BaseModel):
  light_sensitivity: str = Field("normal", description="光敏感度：normal, sensitive")
  noise_sensitivity: str = Field("normal", description="声敏感度：normal, sensitive")
  room_base_noise: Optional[float] = Field(None, description="卧室底噪分贝值")


class StageVitals(BaseModel):
  """单个睡眠阶段内的平均体征（用于各周期阶段解读：心率/呼吸/体温）"""
  avg_heart_rate: Optional[float] = Field(None, description="该阶段平均心率，单位bpm")
  avg_respiratory: Optional[float] = Field(None, description="该阶段平均呼吸频率，单位次/分钟")
  avg_temperature: Optional[float] = Field(None, description="该阶段平均体温，单位摄氏度")


class NightEvent(BaseModel):
  """夜间事件：环境监测与设备交互日志（用于清醒/各周期时段的归因解读）

  event_type 取值：
    noise        环境噪音事件（声音监测）
    dialog       语音交互/对话记录（声音监测）
    screen       屏幕控制日志（用户是否活动）
    light        光感监测（环境灯光变化）
    intervention 设备干预动作（如播放自然噪屏蔽噪音）
    bed_exit     红外/雷达传感器检测到离床
    bed_activity 红外/雷达传感器检测到持续活动
  """
  timestamp: int = Field(..., description="事件发生时间戳（秒级）")
  event_type: str = Field(..., description="事件类型：noise/dialog/screen/light/intervention/bed_exit/bed_activity")
  duration: Optional[float] = Field(None, description="事件持续时长，单位秒")
  detail: Optional[str] = Field(None, description="事件详情，如噪音分贝、干预动作名称、屏幕操作内容")


class SleepElement(BaseModel):
  start_time: int = Field(..., description="睡眠阶段开始时间戳（秒级）")
  duration: float = Field(..., description="睡眠阶段持续时长，单位分钟")
  sleep_type: str = Field(..., description="睡眠阶段类型，如REM、core,deep,rem,awake")
  vitals: Optional[StageVitals] = Field(None, description="该阶段内的平均体征（心率/呼吸/体温）")
  events: List[NightEvent] = Field(default_factory=list, description="该阶段内发生的环境/设备事件（噪音、屏幕、语音、干预等）")

def _classify_awake_type(awake_count: int, awake_minutes: float) -> Optional[str]:
  """夜间觉醒类型（统计派生标签）。

  上游（HealthKit sleep_stage_awake / 设备上报）只有 awake 段+时长，没有子类型，
  因此按当夜觉醒次数与总时长分级；多语言由 explore 的 LLM 报告本地化覆盖。
  """
  if awake_count <= 0:
    return None
  if awake_count <= 2 and awake_minutes <= 10:
    return "Brief awakening"        # 偶发短暂觉醒
  if awake_count >= 5:
    return "Frequent awakenings"    # 频繁觉醒
  if awake_minutes >= 30:
    return "Prolonged awakening"    # 长时间觉醒
  return "Moderate awakening"       # 中等


class SleepResult(BaseModel):
  timestamp: int = Field(..., description="数据 update 时间戳（秒级）")
  sleep_quality: Optional[float] = Field(None, description="睡眠得分，范围0-100") 
  soe: Optional[float] = Field(None, description="入睡效率，范围0-100")
  onset: Optional[float] = Field(None, description="入睡时长，范围0-100, minites")
  sleep_arch_index: Optional[float] = Field(None, description="睡眠结构，包含快速动眼、核心、深度睡眠占比，单位%")
  night_var_index: Optional[float] = Field(None, description="夜间波动，包含觉醒次数、觉醒时长、心率波动等，单位%")

  first_sleep_time: Optional[str] = Field(None, description="首次入睡时间（首次进入快速动眼期时间）, 00:00")
  bed_time: Optional[str] = Field(None, description="开始卧床时间, 23:30")
  wake_time: Optional[str] = Field(None, description="起床时间, 07:30")
  hr_before_sleep: Optional[float] = Field(None, description="入睡前心率，单位bpm")
  rr_before_sleep: Optional[float] = Field(None, description="入睡前呼吸频率，单位次/分钟")

  hrv: Optional[float] = Field(None, description="心率波动，单位bpm")
  respiratory_var: Optional[float] = Field(None, description="呼吸频率波动，单位次/分钟")
  # 当夜心率范围（update_profile 时从 behaviors.heart_rate 按睡眠窗口算出并持久化；
  # behaviors 序列有 MAX_BEHAVIOR_LEN 截断，请求时再算会丢历史窗口数据）
  hr_min: Optional[float] = Field(None, description="当夜睡眠窗口内心率最小值，单位bpm")
  hr_max: Optional[float] = Field(None, description="当夜睡眠窗口内心率最大值，单位bpm")

  avg_heart_rate: Optional[float] = Field(None, description="平均心率，单位bpm")
  avg_respiratory: Optional[float] = Field(None, description="平均呼吸频率，单位次/分钟")
  avg_temperature: Optional[float] = Field(None, description="体温，单位摄氏度")

  scene_preference: Optional[List[Tuple[str, float]]] = Field(None, description="场景偏好，如喜欢的睡眠场景名称")

  # 用户输入：起床后睡眠自评（快速点选提交）
  self_rating: Optional[int] = Field(None, description="睡眠自评分数，范围1-5")
  self_rating_tags: List[str] = Field(default_factory=list, description="睡眠自评标签，如 醒来精神好/夜间易醒/做梦多")

  # 环境均值（光感/声音监测的整晚概况）
  avg_light_level: Optional[float] = Field(None, description="睡间平均环境光照，单位lux")
  avg_noise_level: Optional[float] = Field(None, description="睡间平均环境噪音，单位分贝dB")

  # 睡眠目标完成度（相对 sleep_plan 的本晚达成情况，范围0-100）
  goal_achieved: Optional[float] = Field(None, description="本晚睡眠目标完成度，范围0-100")

  # the recent sleep status sequence, with start_time, duration and sleep_type, used for sleep analysis and advice generation
  sleep_status: List[SleepElement] = Field(default_factory=list, description="the seq for the sleep status, with start_time, duration and sleep_type")

  # 无法归属到具体阶段的整晚事件（如卧床前的环境光、夜间离床等）
  night_events: List[NightEvent] = Field(default_factory=list, description="整晚环境/设备事件（噪音、对话、屏幕、光感、干预、离床等）")

  # 来源标记：None=客户端（Mindora 设备/App）直接上报；"healthkit"=服务端从健康
  # behaviors 序列合成（sleep_session_builder）。合成行在同晚修正值到达时被重算覆盖，
  # 且与同晚设备上报行共存时让位（设备数据更准）
  source: Optional[str] = Field(None, description="记录来源：None=客户端上报；healthkit=服务端合成")

  @property
  def sequence_summaries(self):
    # 觉醒类型由次数+总时长统计派生（原实现按 sleep_type 取众数，key 恒为 "awake"，恒返回 "awake"）
    awake_count = sum(1 for seq in self.sleep_status if seq.sleep_type == "awake")
    awake_duration = sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "awake")

    return {
      "rem_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "rem"),
      "core_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "core"),
      "deep_sleep_duration": sum(seq.duration for seq in self.sleep_status if seq.sleep_type == "deep"),
      "night_awake_duration": awake_duration,
      "night_awake_count": awake_count,
      "night_awake_type": _classify_awake_type(awake_count, awake_duration),
      "time_in_bed": sum(seq.duration for seq in self.sleep_status)
    }

# -------------------------- 睡眠统计辅助 --------------------------

def _most_common(items: List[Any]) -> Optional[Any]:
  """Return the most common item in a list, or None if empty."""
  if not items:
    return None
  counts: Dict[Any, int] = {}
  for item in items:
    counts[item] = counts.get(item, 0) + 1
  return max(counts, key=counts.get)


def compute_recent_sleep_stats(profile: "UserProfile", days: int = 7) -> Dict[str, Any]:
  """Compute aggregated sleep statistics from the most recent `days` records.

  Stats are calculated locally from ``profile.sleep_data`` so callers never need
  to send raw sleep sequences to an LLM.  Returns averages for quality, onset,
  stage durations / percentages, awakenings, and the typical first-sleep time.
  """
  if not profile or not profile.sleep_data:
    return {}

  recent = profile.sleep_data[-days:]
  if not recent:
    return {}

  def _avg(values: List[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 1) if nums else None

  stats: Dict[str, Any] = {
    "record_count": len(recent),
    "avg_sleep_quality": _avg([r.sleep_quality for r in recent]),
    "avg_soe": _avg([r.soe for r in recent]),
    "avg_onset_min": _avg([r.onset for r in recent]),
    "avg_hr_before_sleep": _avg([r.hr_before_sleep for r in recent]),
    "avg_rr_before_sleep": _avg([r.rr_before_sleep for r in recent]),
    "avg_heart_rate": _avg([r.avg_heart_rate for r in recent]),
    "avg_respiratory": _avg([r.avg_respiratory for r in recent]),
    "avg_temperature": _avg([r.avg_temperature for r in recent]),
    "avg_hrv": _avg([r.hrv for r in recent]),
    "avg_self_rating": _avg([r.self_rating for r in recent]),
    "avg_goal_achieved": _avg([r.goal_achieved for r in recent]),
    "typical_first_sleep_time": _most_common([r.first_sleep_time for r in recent if r.first_sleep_time]),
    "typical_bed_time": _most_common([r.bed_time for r in recent if r.bed_time]),
    "typical_wake_time": _most_common([r.wake_time for r in recent if r.wake_time]),
  }

  # Aggregate stage stats across the week.
  total_time_in_bed = 0.0
  total_deep = 0.0
  total_core = 0.0
  total_rem = 0.0
  total_awake = 0.0
  total_awake_count = 0

  for record in recent:
    summ = record.sequence_summaries if record.sleep_status else {}
    total_time_in_bed += summ.get("time_in_bed", 0)
    total_deep += summ.get("deep_sleep_duration", 0)
    total_core += summ.get("core_sleep_duration", 0)
    total_rem += summ.get("rem_sleep_duration", 0)
    total_awake += summ.get("night_awake_duration", 0)
    total_awake_count += summ.get("night_awake_count", 0)

  stats["avg_time_in_bed_min"] = round(total_time_in_bed / len(recent), 1) if recent else 0
  stats["avg_deep_min"] = round(total_deep / len(recent), 1) if recent else 0
  stats["avg_core_min"] = round(total_core / len(recent), 1) if recent else 0
  stats["avg_rem_min"] = round(total_rem / len(recent), 1) if recent else 0
  stats["avg_awake_min"] = round(total_awake / len(recent), 1) if recent else 0
  stats["avg_awake_count"] = round(total_awake_count / len(recent), 1) if recent else 0

  denom = total_time_in_bed or 1
  stats["avg_deep_pct"] = round(total_deep / denom * 100, 1)
  stats["avg_rem_pct"] = round(total_rem / denom * 100, 1)
  stats["avg_core_pct"] = round(total_core / denom * 100, 1)
  stats["avg_awake_pct"] = round(total_awake / denom * 100, 1)

  # Recent scene title from mindora_record (only the most recently used title).
  recent_scene_title = None
  latest_ts = 0
  # Most frequently used scene in the requested window.
  cutoff_ts = int(time.time()) - days * 86400
  scene_counts: Dict[str, int] = {}
  for scene_key, records in (profile.mindora_record or {}).items():
    if not isinstance(records, list) or not records:
      continue
    title = short_scene_id(scene_key).replace("_", " ").title()
    for entry in records:
      if isinstance(entry, (list, tuple)) and len(entry) >= 1:
        try:
          ts = int(entry[0])
        except (TypeError, ValueError):
          continue
        if ts > latest_ts:
          latest_ts = ts
          recent_scene_title = title
        if ts >= cutoff_ts:
          scene_counts[title] = scene_counts.get(title, 0) + 1

  stats["recent_scene_title"] = recent_scene_title

  weekly_top_scene = max(scene_counts, key=scene_counts.get) if scene_counts else None
  stats["weekly_top_scene_title"] = weekly_top_scene
  stats["weekly_top_scene_count"] = scene_counts.get(weekly_top_scene, 0) if weekly_top_scene else 0

  return stats

# -------------------------- 助眠场景推荐模型 --------------------------
class SleepStage(BaseModel):
  """助眠阶段模型"""
  cmd_name: Optional[str] = Field(None, description="背景图/SOP流程名")
  stage_name: Optional[str] = Field(None, description="阶段名字，如Relax, Induce, Deep, Waken")
  audio_file: Optional[str] = Field(None, description="背景音文件名")
  guide_file: Optional[str] = Field(None, description="引导语文件名")
  light_scene: Optional[str] = Field(None, description="灯光场景名")
  aroma_mode: Optional[str] = Field(None, description="香氛模式名")

class SleepScenario(BaseModel):
  """完整的助眠流程方案"""
  scenario_id: Optional[str] = Field(None, description="方案唯一ID")
  scenario_name: Optional[str] = Field(None, description="方案展示名称")
  stages: List[SleepStage] = Field(default_factory=list, description="包含四个睡眠阶段")

# -------------------------- 洞察页分析结果（对应 mindora_advice.md 模块0-5） --------------------------
class InsightModule(BaseModel):
  """单个洞察模块的 LLM 分析结果"""
  module_id: int = Field(..., description="模块编号 0-5")
  title: str = Field("", description="模块标题")
  content: str = Field("", description="主体洞察文案（只解释发生了什么+可能的原因+Mindora做了什么）")
  evidence: List[str] = Field(default_factory=list, description="证据句：基于哪些数据、什么条件得出结论")
  action: str = Field("", description="可行动建议")
  visible: bool = Field(True, description="是否展示（如模块3无觉醒/干预记录时不展示）")

class SleepInsightReport(BaseModel):
  """洞察页 6 模块分析结果，与 mindora_advice.md 的模块0-5一一对应"""
  date: Optional[str] = Field(None, description="分析对应日期 yyyy-MM-dd")
  language: str = Field("en", description="文案语言代码")
  generated_at: Optional[int] = Field(None, description="生成时间戳（秒级）")
  llm_used: bool = Field(True, description="False 代表 LLM 未参与/生成失败")
  greeting: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=0),
    description="模块0｜顶部问候与洞察引导（Context & Trust）",
  )
  onset: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=1),
    description="模块1｜入睡洞察（Sleep Onset Insight）",
  )
  architecture: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=2),
    description="模块2｜睡眠结构洞察（Sleep Architecture Insight）",
  )
  intervention: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=3),
    description="模块3｜夜间波动 & Mindora 干预（Intervention Insight）",
  )
  scene_preference: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=4),
    description="模块4｜场景偏好与推荐（Preference & Recommendation）",
  )
  micro_education: InsightModule = Field(
    default_factory=lambda: InsightModule(module_id=5),
    description="模块5｜轻量睡眠知识提示（Micro Education，可选）",
  )

# -------------------------- /analysis 文案报告（LLM 异步生成，按周期序列存储） --------------------------
class AnalysisTextReport(BaseModel):
  """单个周期（日/周/月）的 /analysis 文案报告。

  只在 update_profile 的后台 LLM 更新中生成；/analysis 请求时纯查库，
  不再同步调用 LLM。modules 结构与《服务端分析接口.md》响应一致。
  """
  request_type: str = Field(..., description="analysis_overview/analysis_sleep_day/analysis_explore/analysis_sleep_week/analysis_sleep_month")
  date: str = Field(..., description="日级: 当日 yyyy-MM-dd；周/月: end_date")
  start_date: Optional[str] = Field(None, description="周/月统计起始日期")
  end_date: Optional[str] = Field(None, description="周/月统计结束日期")
  language: str = Field("en", description="文案语言代码")
  generated_at: int = Field(..., description="生成时间戳（秒级）")
  llm_used: bool = Field(True, description="False 代表 LLM 未参与/生成失败")
  modules: Dict[str, Any] = Field(default_factory=dict, description="接口文档响应结构的模块字典")

# 各类报告的保留条数：日级 30（与 sleep_data 同序列长度）、周 10、月 12
ANALYSIS_REPORT_KEYS = (
  "analysis_overview", "analysis_sleep_day", "analysis_explore",
  "analysis_sleep_week", "analysis_sleep_month",
)
ANALYSIS_REPORT_RETENTION = {
  "analysis_overview": 30,
  "analysis_sleep_day": 30,
  "analysis_explore": 30,
  "analysis_sleep_week": 10,
  "analysis_sleep_month": 12,
}

class SleepPlan(BaseModel):
  """睡眠计划：设备端制定的睡眠目标（用于周/月数据的目标达成率）"""
  target_bed_time: Optional[str] = Field(None, description="目标入睡/卧床时间, 23:30")
  target_wake_time: Optional[str] = Field(None, description="目标起床时间, 07:30")
  target_duration_min: Optional[float] = Field(None, description="目标睡眠时长，单位分钟")
  updated_at: Optional[int] = Field(None, description="计划最近更新时间戳（秒级）")

# -------------------------- 首页弹窗 / 站内消息（tanchuang_suvey.md 第一部分） --------------------------
class PopupState(BaseModel):
  """单个弹窗在该用户侧的服务端状态（频控 + 埋点统计），按 popup_id 存储"""
  show_count: int = Field(0, description="曝光次数（report_popup impression 累计）")
  click_count: int = Field(0, description="点击次数（report_popup click 累计）")
  last_impression_at: Optional[int] = Field(None, description="最近一次曝光秒级时间戳（用于冷却判断）")
  dismissed: bool = Field(False, description="用户是否手动关闭过（display_rule.dismiss_stops=true 时不再下发）")

class InboxMessage(BaseModel):
  """站内消息：popup push_message=true（survey 类恒落）时落地一条，按 popup_id 去重"""
  message_id: str = Field(..., description="消息唯一 ID")
  popup_id: str = Field(..., description="来源弹窗 ID（去重键）")
  title: str = Field(..., description="消息标题")
  subtitle: str = Field("", description="消息副标题")
  action_type: str = Field("dismiss", description="点击跳转动作：survey/url/route/dismiss")
  action_payload: Dict[str, Any] = Field(default_factory=dict, description="跳转参数，与弹窗 action_payload 一致")
  created_at: int = Field(..., description="落地时间戳（秒级）")
  read: bool = Field(False, description="是否已读")

# -------------------------- 调查问卷（tanchuang_suvey.md 第二部分） --------------------------
class SurveyAnswer(BaseModel):
  """单题作答；未作答的非必答题以空值占位（选择题 option_ids=[]，文本题 text=\"\"）"""
  question_id: str = Field(..., description="对应题目 ID")
  type: str = Field(..., description="single_choice/multi_choice/text，与题目 type 一致")
  option_ids: List[str] = Field(default_factory=list, description="所选选项 ID（单选长度1，多选可多个）")
  text: str = Field("", description="文本题填写内容")

class GiftDelivery(BaseModel):
  """问卷礼品收货信息（问卷带 reward.gift_type=physical/virtual 时 submit_survey 必填）"""
  type: str = Field(..., description="physical（实体寄地址）/ virtual（发邮箱）")
  address_id: Optional[str] = Field(None, description="客户端本地地址簿 ID，服务端不认识，仅作日志参考")
  name: Optional[str] = Field(None, description="收件人姓名（physical）")
  phone: Optional[str] = Field(None, description="收件人电话，E.164 格式（physical）")
  region: Optional[str] = Field(None, description="兼容展示串：国家名+state+city+district，勿用于发货（physical）")
  detail: Optional[str] = Field(None, description="兼容展示串：street1+street2+zip，勿用于发货（physical）")
  email: Optional[str] = Field(None, description="接收邮箱（virtual）")
  # 结构化字段（physical，与 address_list 元素一致；发货以这些为准，老客户端快照可能缺失）
  country_code: Optional[str] = Field(None, description="国家/地区，ISO 3166-1 alpha-2（physical）")
  state_code: Optional[str] = Field(None, description="州/省代码：美/加字母码，中国数字省级码（physical）")
  state: Optional[str] = Field(None, description="州/省显示名（physical）")
  city: Optional[str] = Field(None, description="城市（physical）")
  district: Optional[str] = Field(None, description="区/县，仅中国填（physical）")
  street1: Optional[str] = Field(None, description="街道地址（physical）")
  street2: Optional[str] = Field(None, description="门牌/套间，选填（physical）")
  zip: Optional[str] = Field(None, description="邮编（physical）")

class SurveySubmission(BaseModel):
  """一次问卷提交记录；同一 uid+survey_id 幂等，存于 UserProfile.survey_submissions"""
  submission_id: str = Field(..., description="提交记录 ID")
  survey_id: str = Field(..., description="问卷 ID")
  submitted_at: int = Field(..., description="提交时刻秒级时间戳")
  duration_seconds: Optional[int] = Field(None, description="填写总耗时（秒）")
  answers: List[SurveyAnswer] = Field(default_factory=list, description="全部题目的作答（与 questions[] 一一对应）")
  gift_delivery: Optional[GiftDelivery] = Field(None, description="礼品收货信息")
  reward_granted: bool = Field(True, description="是否已发放奖励（重复提交时为 False）")

# -------------------------- 陪伴足迹（peibanzuji.md） --------------------------
class FootprintDay(BaseModel):
  """陪伴足迹单日记录；同一 uid+date 多次上报由服务端幂等合并（布尔取 OR、计数取大、首活跃取小）"""
  date: str = Field(..., description="自然日 yyyy-MM-dd（按客户端 timezone 归属）")
  app_active: bool = Field(False, description="当天是否打开过 App")
  sleep_companion: bool = Field(False, description="当天设备与 App 是否同时有效使用")
  plan_completed: bool = Field(False, description="当天是否完成睡眠计划节点（里程碑判定用）")
  app_open_count: int = Field(0, description="当天打开次数（客户端报当天累计值，合并取大）")
  companion_minutes: int = Field(0, description="当天助眠陪伴分钟数（累计值，合并取大）")
  first_active_at: Optional[int] = Field(None, description="当天首次活跃秒级时间戳（合并取小）")

# -------------------------- 睡眠计划同步（睡眠计划同步接口.md） --------------------------
# 注意与上方设备端 SleepPlan（target_bed_time 等，供 goal_achieved 用）不是一回事：
# 这里是 App 端账号级计划，服务端是唯一事实源，含墓碑（deleted=true）记录。
class SleepPlanCycle(BaseModel):
  """计划周期；fixed_daily=官方预设每天执行，weekdays=自定义按周几"""
  type: Optional[str] = Field(None, description="fixed_daily | weekdays")
  days_count: Optional[int] = Field(None, description="fixed_daily 必填：官方预设总天数（习惯养成21/时差修正7/焦虑修复14）")
  weekdays: List[int] = Field(default_factory=list, description="weekdays 必填：1=周一…7=周日，须至少3天连续")
  repeats_weekly: Optional[bool] = Field(None, description="weekdays 必填；false 时仅执行一轮")


class SyncedSleepPlan(BaseModel):
  """睡眠计划同步对象（上报与下发同构）。

  除 plan_id/updated_at 外全部可选：墓碑只需 plan_id/deleted/deleted_at/updated_at 四个字段；
  完整计划的字段校验在服务端合并层（sleep_plan_service）做，不在模型层卡死。
  """
  plan_id: str = Field(..., description="客户端生成的标准大写 UUID，全局主键（uid+plan_id upsert）")
  kind: Optional[str] = Field(None, description="custom | habit_formation | jet_lag | anxiety_relief")
  custom_name: str = Field("", description="仅 kind=custom 有效，≤10 字符")
  target_minutes: Optional[int] = Field(None, description="目标睡眠时长（分钟），≥60")
  sleep_time: Optional[int] = Field(None, description="入睡时刻，0-1439（当天分钟数，设备当前时区本地时间）")
  wake_time: Optional[int] = Field(None, description="起床时刻，0-1439；≤sleep_time 表示次日")
  cycle: Optional[SleepPlanCycle] = None
  reminder_enabled: Optional[bool] = None
  reminder_minutes_before: Optional[int] = Field(None, description="提前分钟数：15/30/60/90")
  alarm_sound_id: Optional[str] = Field(None, description="temple_bell | sunlit_coast | rainforest_mist")
  sleep_aid_category: Optional[str] = Field(None, description="推荐策略：smart | preference | trending")
  status: Optional[str] = Field(None, description="not_enabled | active | completed | stopped（stopped 仅兼容历史）")
  timezone: Optional[str] = Field(None, description="仅审计：该计划最后保存时的时区，不参与触发时刻计算")
  created_at: Optional[int] = Field(None, description="创建时刻秒级时间戳")
  updated_at: int = Field(..., description="最近更新秒级时间戳，冲突解决依据（后写胜出）")
  activated_at: Optional[int] = Field(None, description="本轮开启起算时刻；status != active 时必须为 null")
  deleted: bool = Field(False, description="墓碑标记")
  deleted_at: Optional[int] = Field(None, description="删除时刻，deleted=true 时必填")


class SleepPlanSyncData(BaseModel):
  uid: Optional[str] = Field(None, description="debug 用；正式请求以 jwt_token 为准")
  jwt_token: Optional[str] = None
  language: Optional[str] = Field(None, description="界面语言，如 zh-Hans / en")
  timezone: Optional[str] = Field(None, description="设备当前时区（IANA），触发时刻换算与日历日完成判定都靠它")
  device_id: Optional[str] = Field(None, description="设备唯一标识，多端冲突排查/审计用")
  last_sync_at: Optional[int] = Field(None, description="上次同步成功时服务端返回的 server_time；首次不传")
  plans: List[SyncedSleepPlan] = Field(default_factory=list, description="sync_plans：自 last_sync_at 以来有变更的计划（含墓碑）")


class SleepPlanSyncRequest(BaseModel):
  request_type: str = Field(..., description="sync_plans | query_plans")
  timestamp: int = Field(..., description="请求发送时间戳（秒级）")
  version: str = Field("1.0")
  data: SleepPlanSyncData

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    if self.request_type == "sync_plans":
      pass  # plans 可为空数组（无变更时仅拉取）
    elif self.request_type != "query_plans":
      raise ValueError(f"unknown request_type: {self.request_type}")
    return self


class UserProfile(BaseModel):
  """用户画像信息"""
  uid_emb: List[float] = Field(default_factory=list)
  basic_info: Optional[Dict[str, str]] = Field(default_factory=dict)
  long_term_profile: List[Tuple[str, float]] = Field(default_factory=list)

  # 睡眠计划（设备端制定的睡眠目标，配合 SleepResult.goal_achieved 计算达成率）
  sleep_plan: Optional[SleepPlan] = Field(None, description="用户的睡眠目标计划")
  # App 端账号级睡眠计划（睡眠计划同步接口.md；服务端为唯一事实源，含墓碑记录）
  sleep_plans: List[SyncedSleepPlan] = Field(default_factory=list, description="账号级睡眠计划全量（含 deleted 墓碑）")
  sleep_plans_synced_at: Optional[int] = Field(None, description="最近一次计划同步的 server_time（秒）")

  # App 端账号级睡眠计划（睡眠计划同步接口.md；服务端为唯一事实源，含墓碑记录）
  sleep_plans: List[SyncedSleepPlan] = Field(default_factory=list, description="账号级睡眠计划全量（含 deleted 墓碑）")
  sleep_plans_synced_at: Optional[int] = Field(None, description="最近一次计划同步的 server_time（秒）")

  # 洞察页 6 模块 LLM 分析结果（mindora_advice.md 模块0-5）
  sleep_insight: Optional[SleepInsightReport] = Field(None, description="洞察页6模块睡眠分析结果")

  # /analysis 文案报告序列（LLM 异步生成；日级保留30、周10、月12，见 ANALYSIS_REPORT_RETENTION）
  analysis_reports: Dict[str, List[AnalysisTextReport]] = Field(
    default_factory=lambda: {key: [] for key in ANALYSIS_REPORT_KEYS},
    description="按 request_type 分组的分析文案报告序列",
  )

  # 睡眠健康问卷（onboarding/设置页收集的睡眠相关字段）
  sleep_health: Optional[SleepHealthProfile] = Field(None, description="睡眠健康问卷")

  # 睡眠模式设置（对应 GUI/data/config 中的 profile.sleepMode）
  sleep_mode: Optional[SleepModeProfile] = Field(None, description="睡眠模式设置")

  # 新增：存储推荐的助眠候选方案
  sleep_scenarios_reco: Optional[List[SleepScenario]] = Field(default_factory=list, description="推荐的候选助眠流程列表")
  standard_sop_reco: List[SleepScenario] = Field(default_factory=list, description="推荐的标准SOP流程列表")

  @field_validator("standard_sop_reco", mode="before")
  @classmethod
  def normalize_standard_sop_reco(cls, value):
    if not isinstance(value, list):
      return value

    normalized = []
    for item in value:
      if isinstance(item, str):
        normalized.append({
          "scenario_id": None,
          "scenario_name": item,
          "stages": [{
            "cmd_name": item,
            "stage_name": None,
            "audio_file": None,
            "guide_file": None,
            "light_scene": None,
            "aroma_mode": None,
          }]
        })
      else:
        normalized.append(item)
    return normalized

  behaviors: Dict[str, List[Tuple[int, Any]]] = Field(
    default_factory=lambda: {
      # 生命体征（v1 口径，健康数据同步接口.md；v2 客户端不再写 heart_rate）
      "heart_rate": [], "blood_oxygen": [], "resting_heart_rate": [],
      "heart_rate_variability_sdnn": [], "respiratory_rate": [],
      "sleeping_wrist_temperature": [], "body_temperature": [],
      # 生命体征（v2 口径，健康数据同步接口_0814.md：全部裁剪到睡眠跨度）
      "sleep_heart_rate_min": [], "sleep_heart_rate_max": [],
      "sleep_heart_rate_variability_sdnn": [], "sleep_respiratory_rate": [],
      "sleep_body_temperature": [],
      # 睡眠状态
      "sleep_status": [],
      "sleep_stage_deep": [], "sleep_stage_rem": [], "sleep_stage_light": [],
      # 睡眠阶段（v2 新增轨）
      "sleep_stage_unspecified": [], "sleep_stage_awake": [], "sleep_in_bed": [],
      # 交互行为
      "clicks": [], "plays": [],
    }
  )

  # 健康数据按自然日的口径版本登记（yyyy-MM-dd → health_schema_version）。
  # update_profile 时按请求 timezone 把 behaviors 时间戳归日写入；缺省/老数据按 1 处理。
  # 对账接口 query_health_sync_state 据此回答版本，"有哪些天"则按 behaviors 实际数据现算。
  health_sync_days: Dict[str, int] = Field(default_factory=dict)

  # 画像变更版本号：服务端维护、单调递增，每次 save_profile 落盘前 +1（谁改的都会
  # 触发：App update、设备 update、后台 LLM 写回、读路径补缺）。设备端用
  # query_revision 做 3s 轻量探测，变了才全量 query_profile；自己推云成功后记下响应
  # 里的新 revision 做回显过滤。客户端只读——update 时客户端携带的值被忽略
  # （合并分支不拷贝此字段，新建分支重置为 0，见 _apply_basic_update）。
  revision: int = Field(0, description="画像版本号（服务端维护，单调递增；query_revision 探测用）")

  # only the recent 7 days sleep data will be returned to app, and used for sleep analysis and advice generation, such as the data of yestoday night
  sleep_data:  List[SleepResult] = Field(default_factory=list)

  sleep_analysis: Dict[str, Any] = Field(
    default_factory=lambda: {
      # 数值快照（每次 update_profile 重算，7 天窗口语义天然滚动，仅存当前一份）：
      # most_used_scene(_7d) 与 best_sleep_quality_scene_7d 是 /analysis 骨架
      # （weekly_best / onset_efficiency / scene_preference）的直接数据源；
      # insight_memory 是洞察长期记忆（建议历史/昨日首页主题，见 insight_rules）。
      # 历史上的 sleep_trend_week/month 与 scene{title,music,text,image_url}
      # 为死字段（从未有写入方，趋势已由 rule_trend 现算），已移除。
      "most_used_scene": None,
      "most_used_scene_7d": None,
      "best_sleep_quality_scene_7d": None,
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

  # 最近一次请求的客户端环境（每次 update_profile 显式携带时刷新）：
  # 每日 LLM 分析触发门的自然日口径用 last_request_timezone（缺省 UTC），
  # sleep_insight/analysis_reports 文案语言用 last_request_language（缺省 en）
  last_request_timezone: Optional[str] = Field(None, description="最近一次请求的客户端时区 ID，如 Asia/Shanghai")
  last_request_language: Optional[str] = Field(None, description="最近一次请求的界面语言，如 zh-Hans / en")
  last_request_at: Optional[int] = Field(None, description="最近一次带环境信息的请求时间戳（秒）")

  # 首页弹窗 / 站内消息 / 调查问卷 / 陪伴足迹（tanchuang_suvey.md, peibanzuji.md）
  popup_states: Dict[str, PopupState] = Field(
    default_factory=dict,
    description="弹窗服务端状态（频控+埋点），按 popup_id",
  )
  inbox_messages: List[InboxMessage] = Field(
    default_factory=list,
    description="站内消息（弹窗 push_message 落地，按 popup_id 去重）",
  )
  survey_submissions: Dict[str, SurveySubmission] = Field(
    default_factory=dict,
    description="问卷提交记录，按 survey_id（同一 uid+survey_id 幂等）",
  )
  footprint_days: Dict[str, FootprintDay] = Field(
    default_factory=dict,
    description="陪伴足迹日记录，按 yyyy-MM-dd",
  )


class ProfileData(BaseModel):
  uid: Optional[str] = Field(None, description="uid, just for debug")
  jwt_token: str | None = Field(None, description="JWT token，in wan should be fixed")
  user_profile: Optional[UserProfile] = Field(None, description="user profile")
  language: Optional[str] = Field(None, description="界面语言，如 en、zh-Hans（健康同步 v2 必填）")
  timezone: Optional[str] = Field(None, description="客户端时区，如 Asia/Shanghai；健康数据按自然日归类的日界依据")
  health_schema_version: Optional[int] = Field(
    None,
    description="健康数据口径版本（健康数据同步接口_0814.md §8.3）；缺省按 1 处理",
  )
  # query_health_sync_state 对账窗口（§8.4）
  start_date: Optional[str] = Field(None, description="对账起始自然日 yyyy-MM-dd")
  end_date: Optional[str] = Field(None, description="对账结束自然日 yyyy-MM-dd")
  # 三态开关：None=由服务端按数据自动决策（有未分析的新夜晚则每天生成一次）；
  # 显式 True=客户端要求跳过；显式 False=客户端要求立即强制生成
  skip_sleep_scenarios_reco_update: Optional[bool] = Field(
    None,
    description="助眠场景推荐(sleep_scenarios_reco)开关：None=自动 / True=跳过 / False=强制，不影响睡眠分析",
  )
  skip_sleep_analysis_update: Optional[bool] = Field(
    None,
    description="睡眠分析(sleep_insight + analysis cache)开关：None=自动 / True=跳过 / False=强制，不影响场景推荐",
  )
  # query_profile 响应裁剪开关：sleep_data 和 behaviors 是画像的体积大头，
  # 不需要时可要求服务端不携带，减小响应包；不携带 behaviors 时连同
  # health_sync_days（健康数据对账状态）一起去掉
  include_sleep_data: bool = Field(True, description="query_profile 响应是否携带 sleep_data，默认 True")
  include_behaviors: bool = Field(True, description="query_profile 响应是否携带 behaviors，默认 True；为 False 时同时剔除 health_sync_days")


class ProfileRequest(BaseModel):
  request_type: str = Field("query_profile", description="query_profile | update_profile（client_request.md 契约）")
  timestamp: int = Field(..., description="请求发送时间戳（秒级），必填")
  version: str = Field("1.0", description="version, needed, such as 1.0")
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
  request_type: str = Field("query_profile", description="query_profile | update_profile（client_request.md 契约）")
  data: Optional[Dict[str, Any]] = Field(None, description="Response data")


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


# -------------------------- /popup 请求模型（popup_survey.md） --------------------------
class PopupData(BaseModel):
  uid: Optional[str] = Field(None, description="用户标识（debug 用）")
  jwt_token: Optional[str] = Field(None, description="当前登录态 JWT")
  language: str = Field("zh-Hans", description="当前 App 语言，如 zh-Hans / en")
  timezone: Optional[str] = Field(None, description="IANA 时区标识，供统计/按地区投放参考")
  app_version: Optional[str] = Field(None, description="客户端版本，用于灰度投放")
  platform: Optional[str] = Field(None, description="ios / android")
  placement: str = Field("home", description="展示位，当前固定 home")
  # query_popups scope=history 字段（popup_survey.md 2.1 历史消息恢复）
  scope: str = Field("active", description="active=投放查询（缺省）/ history=历史消息恢复")
  popup_ids: Optional[List[str]] = Field(None, description="仅 scope=history：非空按 ID 精确恢复（含过期条目）；空/缺省拉该 uid 全量有效消息")
  # report_popup 字段
  popup_id: Optional[str] = Field(None, description="report_popup 必填，对应下发的弹窗 ID")
  event: Optional[str] = Field(None, description="report_popup 必填：impression/click/dismiss")
  event_at: Optional[int] = Field(None, description="事件秒级时间戳，缺省用 timestamp")

class PopupRequest(BaseModel):
  request_type: str = Field(..., description="query_popups | report_popup")
  timestamp: int = Field(..., description="请求发送时间戳（秒级）")
  version: str = Field("1.0")
  data: PopupData

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    if self.data.scope not in ("active", "history"):
      raise ValueError(f"unknown scope: {self.data.scope}")
    if self.request_type == "report_popup":
      if not self.data.popup_id:
        raise ValueError("report_popup 时 data.popup_id 必填")
      if self.data.event not in ("impression", "click", "dismiss"):
        raise ValueError("report_popup 时 data.event 必须是 impression/click/dismiss")
    elif self.request_type != "query_popups":
      raise ValueError(f"unknown request_type: {self.request_type}")
    return self

# -------------------------- /survey 请求模型（tanchuang_suvey.md） --------------------------
class SurveyData(BaseModel):
  uid: Optional[str] = Field(None, description="用户标识（debug 用）")
  jwt_token: Optional[str] = Field(None, description="当前登录态 JWT")
  language: str = Field("zh-Hans", description="当前 App 语言")
  survey_id: Optional[str] = Field(None, description="问卷 ID，query_survey/submit_survey 必填")
  answers: List[SurveyAnswer] = Field(default_factory=list, description="submit_survey 必填，须覆盖问卷全部题目")
  submitted_at: Optional[int] = Field(None, description="提交时刻秒级时间戳，缺省用 timestamp")
  duration_seconds: Optional[int] = Field(None, description="填写总耗时（秒）")
  gift_delivery: Optional[GiftDelivery] = Field(None, description="问卷有礼品时必填")

class SurveyRequest(BaseModel):
  request_type: str = Field(..., description="query_survey | submit_survey")
  timestamp: int = Field(..., description="请求发送时间戳（秒级）")
  version: str = Field("1.0")
  data: SurveyData

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    if not self.data.survey_id:
      raise ValueError("data.survey_id 必填")
    if self.request_type == "submit_survey" and not self.data.answers:
      raise ValueError("submit_survey 时 data.answers 必填且须覆盖全部题目")
    elif self.request_type not in ("query_survey", "submit_survey"):
      raise ValueError(f"unknown request_type: {self.request_type}")
    return self

# -------------------------- /companion_footprint 请求模型（peibanzuji.md） --------------------------
class FootprintData(BaseModel):
  uid: Optional[str] = Field(None, description="用户标识（debug 用）")
  jwt_token: Optional[str] = Field(None, description="当前登录态 JWT")
  timezone: str = Field("UTC", description="IANA 时区标识；自然日归属以该时区计算")
  # upload_footprint 字段
  days: List[FootprintDay] = Field(default_factory=list, description="upload_footprint 必填，可按自然日批量补传")
  # query_footprint 字段
  scope: Optional[str] = Field(None, description="query_footprint 必填：month（单月）/ year（全年）")
  year: Optional[int] = Field(None, description="query_footprint 必填，目标年份")
  month: Optional[int] = Field(None, description="scope=month 必填，目标月份 1-12")

class FootprintRequest(BaseModel):
  request_type: str = Field(..., description="upload_footprint | query_footprint")
  timestamp: int = Field(..., description="请求发送时间戳（秒级）")
  version: str = Field("1.0")
  data: FootprintData

  @model_validator(mode='after')
  def validate_data_by_request_type(self):
    if self.data.jwt_token is None and self.data.uid is None:
      raise ValueError("uid or jwt_token must be provided")
    if self.request_type == "upload_footprint":
      if not self.data.days:
        raise ValueError("upload_footprint 时 data.days 必填且非空")
    elif self.request_type == "query_footprint":
      if self.data.scope not in ("month", "year"):
        raise ValueError("query_footprint 时 data.scope 必须是 month/year")
      if self.data.year is None:
        raise ValueError("query_footprint 时 data.year 必填")
      if self.data.scope == "month" and not (1 <= (self.data.month or 0) <= 12):
        raise ValueError("scope=month 时 data.month 必填且为 1-12")
    else:
      raise ValueError(f"unknown request_type: {self.request_type}")
    return self


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
