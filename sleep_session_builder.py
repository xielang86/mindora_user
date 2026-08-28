"""sleep_session_builder.py — 从健康行为序列（behaviors）按晚合成 SleepResult。

背景：健康数据同步接口_0814.md 只约定了 behaviors 的存储口径；而分析链路
（/analysis 各得分、_analysis_needed 触发门、LLM 上下文、零数据兜底替换）全部读
profile.sleep_data。iOS 健康同步从不上报 SleepResult —— 只靠 HealthKit 的用户
sleep_data 永远为空，分析页所有 score 都是空值。本模块在 update_profile 合并完
behaviors 后，把 sleep_stage_* / sleep_in_bed / sleep_* 体征序列聚合成每晚安的
SleepResult 行（source="healthkit" 标记）。

规则：
  - 会话聚类：5 条阶段轨（互斥，md §4.1）的区间按起点排序扫一遍，相邻区间间隔
    > SESSION_GAP_SECONDS 切新会话；总睡眠 < MIN_SESSION_ASLEEP_SECONDS 的碎片丢弃
  - 阶段映射：deep→deep / rem→rem / light→core / unspecified→core / awake→awake
    （SleepElement.duration 单位是分钟，behaviors 区间是秒）
  - onset 按 md §6.1：lightsOut = inBed 起点（严格早于会话起点才采用，否则取会话起点），
    终点 = 首次累计睡够 5 分钟（≤1 分钟碎醒不打断）；第一段即睡着 → 不可测（None）；
    上限 180 分钟
  - 得分为派生启发式（本文件底部注明公式）：sleep_quality 由时长/效率/结构加权，
    soe 由 onset 推出，sleep_arch_index 由 deep+rem 占比，night_var_index 由觉醒情况
  - hr_min/hr_max 不在这里填：profile_service._update_night_hr_range 按会话起点
    配对 v2 的 sleep_heart_rate_min/max 写入
  - 设备（Mindora）上报的 SleepResult 优先：会话窗口内已有非合成行则跳过该晚；
    合成行在同晚行为修正值到达后由重算覆盖（窗口匹配替换，见 profile_service）
"""
import datetime
from typing import Optional

from user_profile import SleepElement, SleepResult

# 相邻阶段区间间隔超过该值视为两晚（夜间区间基本连续，间隔 3 小时只可能是白天）
SESSION_GAP_SECONDS = 3 * 3600
# 总睡眠低于该值的会话视为脏数据碎片（误触发的几分钟区间），不生成夜晚记录
MIN_SESSION_ASLEEP_SECONDS = 30 * 60
# 单条区间时长上限，超过视为坏样本丢弃
MAX_INTERVAL_SECONDS = 16 * 3600

# onset 口径常量（md §6.1）
_ONSET_SUSTAINED_SECONDS = 300   # 首次「持续睡着」= 累计睡够 5 分钟
_ONSET_MICRO_AWAKE_SECONDS = 60  # 不超过 1 分钟的碎醒不算打断
_ONSET_CAP_MINUTES = 180

_STAGE_TYPE_MAP = {
  "sleep_stage_deep": "deep",
  "sleep_stage_rem": "rem",
  "sleep_stage_light": "core",
  "sleep_stage_unspecified": "core",
  "sleep_stage_awake": "awake",
}

# 合成行标记：与客户端（设备）直接上报的 SleepResult 区分；合并/覆盖策略靠它
SOURCE_HEALTHKIT = "healthkit"


class SleepSession:
  """一晚睡眠会话：聚类后的阶段区间 + 落在窗口内的 inBed 区间。"""

  def __init__(self):
    self.intervals: list[tuple[int, int, str]] = []   # (start, end, type)，按 start 升序
    self.in_bed: list[tuple[int, int]] = []           # (start, end)

  @property
  def start(self) -> int:
    return self.intervals[0][0]

  @property
  def end(self) -> int:
    return max(e for _s, e, _t in self.intervals)

  def asleep_seconds(self) -> float:
    return sum(e - s for s, e, t in self.intervals if t != "awake")


def _read_intervals(behaviors: dict, key: str) -> list[tuple[int, float]]:
  """behaviors[key] → [(start, duration_seconds)]，非法/超长条目跳过。"""
  out = []
  for item in behaviors.get(key) or []:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
      continue
    try:
      start, dur = int(item[0]), float(item[1])
    except (TypeError, ValueError):
      continue
    if dur <= 0 or dur > MAX_INTERVAL_SECONDS:
      continue
    out.append((start, dur))
  return out


def derive_sessions(behaviors: dict) -> list[SleepSession]:
  """5 条阶段轨聚类成会话；inBed 区间按时间重叠挂到对应会话（独立轨，不参与聚类）。"""
  intervals: list[tuple[int, int, str]] = []
  for key, stage_type in _STAGE_TYPE_MAP.items():
    for start, dur in _read_intervals(behaviors, key):
      intervals.append((start, int(start + dur), stage_type))
  if not intervals:
    return []
  intervals.sort()

  sessions: list[SleepSession] = []
  cur = SleepSession()
  for iv in intervals:
    if cur.intervals and iv[0] - cur.end > SESSION_GAP_SECONDS:
      sessions.append(cur)
      cur = SleepSession()
    cur.intervals.append(iv)
  sessions.append(cur)

  # inBed 与会话时间重叠即归属（md §4.1：inBed 是独立轨，与阶段轨重叠）
  for start, dur in _read_intervals(behaviors, "sleep_in_bed"):
    end = int(start + dur)
    for session in sessions:
      if start < session.end and end > session.start:
        session.in_bed.append((start, end))
        break

  return [s for s in sessions if s.asleep_seconds() >= MIN_SESSION_ASLEEP_SECONDS]


def _compute_onset(session: SleepSession) -> tuple[Optional[float], int]:
  """返回 (onset_minutes 或 None, lights_out_ts)。口径 md §6.1。"""
  lights_out = session.start
  if session.in_bed:
    first_in_bed = min(s for s, _e in session.in_bed)
    if first_in_bed < session.start:  # 严格早于才采用；等于会话起点的包络写法弃用
      lights_out = first_in_bed

  # 第一段就是睡着：会话起点已经在睡，无法知道何时入睡 → 不可测（不是「秒睡」）
  if session.intervals[0][2] != "awake" and lights_out >= session.start:
    return None, lights_out

  acc = 0.0
  endpoint: Optional[int] = None
  for start, end, stage_type in session.intervals:
    if stage_type == "awake":
      if end - start > _ONSET_MICRO_AWAKE_SECONDS:
        acc = 0.0  # 长时间清醒，重新累计
      continue
    acc += end - start
    if acc >= _ONSET_SUSTAINED_SECONDS:
      endpoint = int(end - (acc - _ONSET_SUSTAINED_SECONDS))
      break
  if endpoint is None or endpoint <= lights_out:
    return None, lights_out
  return min((endpoint - lights_out) / 60.0, _ONSET_CAP_MINUTES), lights_out


def _window_values(behaviors: dict, key: str, start: int, end: int) -> list[float]:
  """数值型序列在窗口 [start, end] 内的取值列表。"""
  values = []
  for item in behaviors.get(key) or []:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
      continue
    try:
      ts, val = int(item[0]), float(item[1])
    except (TypeError, ValueError):
      continue
    if start <= ts <= end:
      values.append(val)
  return values


def _mean(values: list[float]) -> Optional[float]:
  return round(sum(values) / len(values), 1) if values else None


def _hhmm(ts: int, tz: datetime.tzinfo) -> str:
  return datetime.datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def build_sleep_result(session: SleepSession, behaviors: dict, tz: datetime.tzinfo) -> SleepResult:
  """一个会话 → 一条 SleepResult（source=healthkit）。"""
  start, end = session.start, session.end
  elements = [
    SleepElement(start_time=s, duration=round((e - s) / 60.0, 2), sleep_type=t)
    for s, e, t in session.intervals
  ]

  asleep_sec = session.asleep_seconds()
  awake_sec = sum(e - s for s, e, t in session.intervals if t == "awake")
  deep_sec = sum(e - s for s, e, t in session.intervals if t == "deep")
  rem_sec = sum(e - s for s, e, t in session.intervals if t == "rem")
  in_bed_sec = sum(e - s for s, e in session.in_bed) or None

  onset_min, lights_out = _compute_onset(session)

  # ── 派生得分（启发式，无设备算法时让各 score 卡有真实数据支撑）────────────
  # duration：总睡眠对标 8h；structure：deep+rem 占比对标 45%；efficiency：总睡眠/卧床
  duration_score = min(asleep_sec / (8 * 3600), 1.0) * 100
  structure_score = min((deep_sec + rem_sec) / asleep_sec / 0.45, 1.0) * 100 if asleep_sec else None
  efficiency = asleep_sec / in_bed_sec if in_bed_sec else None
  if efficiency is not None:
    quality = 0.5 * duration_score + 0.25 * efficiency * 100 + 0.25 * (structure_score or 0)
  else:
    quality = 0.6 * duration_score + 0.4 * (structure_score or 0)

  # soe（入睡效率分）：onset ≤5 分钟计 100，≥60 分钟计 0，线性
  soe = None
  if onset_min is not None:
    soe = round(max(0.0, 100.0 - max(0.0, onset_min - 5) / 55 * 100), 1)

  # night_var_index（夜间波动分）：觉醒次数与觉醒时长占比罚分
  span_sec = asleep_sec + awake_sec
  awake_count = sum(1 for _s, _e, t in session.intervals if t == "awake")
  awake_share = awake_sec / span_sec if span_sec else 0.0
  night_var = max(0.0, 100.0 - awake_count * 8 - awake_share * 100)

  # ── 窗口内体征 ────────────────────────────────────────────────────────────
  hrv = _mean(_window_values(behaviors, "sleep_heart_rate_variability_sdnn", start, end))
  rr_values = _window_values(behaviors, "sleep_respiratory_rate", start, end)
  avg_rr = _mean(rr_values)
  respiratory_var = None
  if len(rr_values) >= 2 and avg_rr:
    respiratory_var = round((max(rr_values) - min(rr_values)) / avg_rr * 100, 1)
  temp_values = _window_values(behaviors, "sleep_body_temperature", start, end) \
    or _window_values(behaviors, "sleeping_wrist_temperature", start, end)
  avg_temp = _mean(temp_values)

  return SleepResult(
    timestamp=end,  # 醒来时刻；_analysis_needed 以它归日
    sleep_quality=round(quality, 1),
    soe=soe,
    onset=round(onset_min, 1) if onset_min is not None else None,
    sleep_arch_index=round(structure_score, 1) if structure_score is not None else None,
    night_var_index=round(night_var, 1),
    first_sleep_time=None if onset_min is None else _hhmm(int(lights_out + onset_min * 60), tz),
    bed_time=_hhmm(lights_out, tz),
    wake_time=_hhmm(end, tz),
    hrv=hrv,
    respiratory_var=respiratory_var,
    avg_respiratory=avg_rr,
    avg_temperature=avg_temp,
    sleep_status=elements,
    source=SOURCE_HEALTHKIT,
  )


def derive_sleep_results(behaviors: dict, tz: datetime.tzinfo) -> list[tuple[int, int, SleepResult]]:
  """behaviors → [(session_start, session_end, SleepResult)]，按会话起点升序。

  返回窗口供调用方做「同窗口替换/设备行优先」合并。

  午睡过滤：按醒来自然日（tz）分组，同一天只保留睡眠最长的一个会话——
  避免下午小睡（30 分钟以上）混进 sleep_data 拉低周均分、被当成「最新一晚」。
  夜班/作息紊乱者一天只会有一个长睡眠会话，不受影响。
  """
  sessions = derive_sessions(behaviors)
  by_wake_day: dict[datetime.date, SleepSession] = {}
  for session in sessions:
    day = datetime.datetime.fromtimestamp(session.end, tz).date()
    if day not in by_wake_day or session.asleep_seconds() > by_wake_day[day].asleep_seconds():
      by_wake_day[day] = session
  return [
    (s.start, s.end, build_sleep_result(s, behaviors, tz))
    for s in sorted(by_wake_day.values(), key=lambda s: s.start)
  ]
