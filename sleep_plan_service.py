"""sleep_plan_service.py — 睡眠计划同步的服务端合并/校验引擎（睡眠计划同步接口.md §5/§6）。

服务端是唯一事实源：
  ① 冲突解决：updated_at 后写胜出，完全相等服务端已有记录为准；墓碑参与比较
  ② 额度校验：free=0 / basic(=服务端 pro)=1 / premium=5，未删除的全部状态都占额度
  ③ 唯一开启：同一 uid 同时至多 1 条 active；status != active 时 activated_at 必须为 null
  ④ 官方预设去重：同一 uid 同一 kind（非 custom）只允许一条未删除记录
  ⑤ 官方计划周期完成：按设备当前时区的「日历日」加 days_count（不是 ×86400，跨夏令时差 1 小时）
  ⑤b 自定义 repeats_weekly=false：从 activated_at 当天（含）起 7 天窗口内每个所选星期执行一次，
     最后执行日的 sleep_time + target_minutes 到达后自动 completed

存储：UserProfile.sleep_plans 全量（含墓碑）。墓碑暂不物理清理——清理后老设备重传同 plan_id
会复活计划，且每用户最多 5 条量级，膨胀可忽略。
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from user_profile import SyncedSleepPlan

# ── 枚举与常量（协议一律下划线形式，客户端驼峰→下划线的转换在客户端同步层做）──
PLAN_KINDS = ("custom", "habit_formation", "jet_lag", "anxiety_relief")
OFFICIAL_KINDS = ("habit_formation", "jet_lag", "anxiety_relief")
OFFICIAL_DAYS_COUNT = {"habit_formation": 21, "jet_lag": 7, "anxiety_relief": 14}
PLAN_STATUSES = ("not_enabled", "active", "completed", "stopped")
REMINDER_MINUTES_CHOICES = (15, 30, 60, 90)
ALARM_SOUND_IDS = ("temple_bell", "sunlit_coast", "rainforest_mist")
SLEEP_AID_CATEGORIES = ("smart", "preference", "trending")
MIN_TARGET_MINUTES = 60
MAX_CUSTOM_NAME_LEN = 10

# 额度表（§6②）：键为服务端等级（common/user_rights.py 的 free/pro/premium），
# 文档里的 "basic（普通会员）" 对应服务端 pro。
PLAN_QUOTA_BY_LEVEL = {"free": 0, "pro": 1, "premium": 5}

# 响应 membership_tier 用文档口径（free/basic/premium）
_LEVEL_TO_CLIENT_TIER = {"free": "free", "pro": "basic", "premium": "premium"}


def client_tier(effective_level: Optional[str]) -> str:
  return _LEVEL_TO_CLIENT_TIER.get((effective_level or "free").strip().lower(), "free")


def plan_quota(effective_level: Optional[str]) -> int:
  return PLAN_QUOTA_BY_LEVEL.get((effective_level or "free").strip().lower(), 0)


def _resolve_tz(tz_name: Optional[str]) -> timezone:
  if tz_name:
    try:
      return ZoneInfo(tz_name)
    except Exception:
      logging.warning("sleep_plan: invalid timezone %r, fallback to UTC", tz_name)
  return timezone.utc


def _add_calendar_days(ts: int, days: int, tz) -> int:
  """从 ts 起加 days 个「日历日」（保持当地墙钟时刻不变），返回秒级时间戳。

  ⚠️ 不是 ts + days*86400：跨夏令时的时区里那一天是 23/25 小时（§6⑤）。
  """
  dt = datetime.fromtimestamp(ts, tz)
  target_date = dt.date() + timedelta(days=days)
  return int(datetime.combine(target_date, dt.timetz()).timestamp())


def _official_completion_ts(plan: SyncedSleepPlan, tz) -> Optional[int]:
  """fixed_daily 官方预设的完成时刻：activated_at + days_count 个日历日。"""
  if plan.activated_at is None or not plan.cycle or plan.cycle.days_count is None:
    return None
  return _add_calendar_days(plan.activated_at, plan.cycle.days_count, tz)


def _one_round_completion_ts(plan: SyncedSleepPlan, tz) -> Optional[int]:
  """自定义 repeats_weekly=false 的完成时刻（§6⑤b）。

  一轮 = 从 activated_at 当天（含）起 7 天窗口内，cycle.weekdays 里每个星期各执行一次；
  完成时刻 = 该轮最后一个执行日的 sleep_time + target_minutes。
  """
  if plan.activated_at is None or not plan.cycle or not plan.cycle.weekdays:
    return None
  if plan.sleep_time is None or plan.target_minutes is None:
    return None
  act_date = datetime.fromtimestamp(plan.activated_at, tz).date()
  weekdays = set(plan.cycle.weekdays)
  exec_days = [
    act_date + timedelta(days=i)
    for i in range(7)
    if (act_date + timedelta(days=i)).isoweekday() in weekdays
  ]
  if not exec_days:
    return None
  last_day = max(exec_days)
  sleep_dt = datetime.combine(last_day, datetime.min.time(), tzinfo=tz) + timedelta(
    minutes=plan.sleep_time + plan.target_minutes,
  )
  return int(sleep_dt.timestamp())


def completion_ts(plan: SyncedSleepPlan, tz) -> Optional[int]:
  """到达该时刻计划应自动 completed；不适用（永不自动完成）返回 None。"""
  if plan.kind != "custom":
    return _official_completion_ts(plan, tz)
  if plan.cycle and plan.cycle.repeats_weekly is False:
    return _one_round_completion_ts(plan, tz)
  return None  # repeats_weekly=true 的自定义计划没有终点


def has_3_consecutive_weekdays(weekdays) -> bool:
  """weekdays（1=周一…7=周日）须构成至少 3 天连续；周六/周日/周一按实际日期视为连续（环形）。"""
  s = set(weekdays)
  return any({d, d % 7 + 1, (d + 1) % 7 + 1} <= s for d in range(1, 8))


def validate_full_plan(plan: SyncedSleepPlan) -> Optional[str]:
  """校验一条完整上报（非墓碑），返回 rejected reason 或 None。"""
  if plan.kind not in PLAN_KINDS:
    return "invalid_plan"
  if plan.status not in PLAN_STATUSES:
    return "invalid_plan"
  if plan.target_minutes is None or plan.target_minutes < MIN_TARGET_MINUTES:
    return "invalid_duration"
  if plan.sleep_time is None or not (0 <= plan.sleep_time <= 1439):
    return "invalid_plan"
  if plan.wake_time is None or not (0 <= plan.wake_time <= 1439):
    return "invalid_plan"
  if plan.created_at is None:
    return "invalid_plan"
  if plan.reminder_enabled is None:
    return "invalid_plan"
  if plan.reminder_minutes_before not in REMINDER_MINUTES_CHOICES:
    return "invalid_plan"
  if plan.alarm_sound_id not in ALARM_SOUND_IDS:
    return "invalid_plan"
  if plan.sleep_aid_category not in SLEEP_AID_CATEGORIES:
    return "invalid_plan"
  if plan.cycle is None or plan.cycle.type not in ("fixed_daily", "weekdays"):
    return "invalid_cycle"

  if plan.kind == "custom":
    if len(plan.custom_name or "") > MAX_CUSTOM_NAME_LEN:
      return "invalid_name"  # 服务端拒绝，不要静默截断（否则两端名称不一致）
    if plan.cycle.type != "weekdays":
      return "invalid_cycle"
    if not plan.cycle.weekdays or plan.cycle.repeats_weekly is None:
      return "invalid_cycle"
    if not all(1 <= d <= 7 for d in plan.cycle.weekdays):
      return "invalid_cycle"
    if not has_3_consecutive_weekdays(plan.cycle.weekdays):
      return "invalid_cycle"
  else:
    # 官方预设：fixed_daily + 固定总天数
    if plan.cycle.type != "fixed_daily" or plan.cycle.days_count != OFFICIAL_DAYS_COUNT[plan.kind]:
      return "invalid_cycle"
  return None


class SyncResult:
  def __init__(self):
    self.plans: list[SyncedSleepPlan] = []   # 合并后的全量（含墓碑），用于落库
    self.rejected: list[dict] = []           # [{plan_id, reason}]
    self.changed: bool = False               # 合并后相对存储是否有变化（决定是否落库）

  @property
  def visible_plans(self) -> list[SyncedSleepPlan]:
    """下发给客户端的全量未删除计划。"""
    return [p for p in self.plans if not p.deleted]


def _normalize(plan: SyncedSleepPlan) -> None:
  """不变式归一：status != active 时 activated_at 必须为 null（§6③）。"""
  if plan.status != "active":
    plan.activated_at = None


def _apply_completion(plans: list[SyncedSleepPlan], tz, now: int) -> bool:
  """把已到达完成时刻的 active 计划置为 completed（§6⑤/⑤b）。返回是否有改动。"""
  changed = False
  for plan in plans:
    if plan.deleted or plan.status != "active":
      continue
    done_at = completion_ts(plan, tz)
    if done_at is not None and now >= done_at:
      plan.status = "completed"
      plan.activated_at = None
      changed = True
      logging.info("sleep_plan auto completed: plan_id=%s kind=%s", plan.plan_id, plan.kind)
  return changed


def _enforce_single_active(plans: list[SyncedSleepPlan]) -> bool:
  """同一 uid 至多 1 条 active：保留 activated_at 最大者，其余降为 not_enabled（§6③）。"""
  active = [p for p in plans if not p.deleted and p.status == "active"]
  if len(active) <= 1:
    return False
  active.sort(key=lambda p: (p.activated_at or 0, p.updated_at), reverse=True)
  for plan in active[1:]:
    plan.status = "not_enabled"
    plan.activated_at = None
    logging.info("sleep_plan demoted to not_enabled (single-active): plan_id=%s", plan.plan_id)
  return True


def sync_plans(
  stored: list[SyncedSleepPlan],
  incoming: list[SyncedSleepPlan],
  *,
  effective_level: Optional[str],
  tz_name: Optional[str],
  now: Optional[int] = None,
) -> SyncResult:
  """sync_plans 主流程：合并 incoming 到 stored，返回全量 + rejected。

  stored/incoming 均含墓碑；返回的 SyncResult.plans 为新的全量（含墓碑）。
  """
  now = now or int(time.time())
  tz = _resolve_tz(tz_name)
  quota = plan_quota(effective_level)
  result = SyncResult()

  # 深拷贝存储侧，避免原地改到画像对象
  by_id: dict[str, SyncedSleepPlan] = {p.plan_id: p.model_copy(deep=True) for p in stored}

  for plan in incoming:
    plan = plan.model_copy(deep=True)
    existing = by_id.get(plan.plan_id)

    if plan.deleted:
      # 墓碑：只需 plan_id/deleted/deleted_at/updated_at；后写胜出
      if plan.deleted_at is None:
        result.rejected.append({"plan_id": plan.plan_id, "reason": "invalid_plan"})
        continue
      if existing is None or plan.updated_at > existing.updated_at:
        by_id[plan.plan_id] = plan
        result.changed = True
      continue

    # 完整计划：先校验字段
    reason = validate_full_plan(plan)
    if reason:
      result.rejected.append({"plan_id": plan.plan_id, "reason": reason})
      continue

    # 官方预设去重（§6④）：同一 uid 同一 kind 只允许一条未删除记录
    if plan.kind != "custom":
      dup = next(
        (p for p in by_id.values()
         if not p.deleted and p.kind == plan.kind and p.plan_id != plan.plan_id),
        None,
      )
      if dup is not None:
        # 已有记录若更旧，用上报内容刷新它（合并成同一条，不新建）
        if existing is None and plan.updated_at > dup.updated_at:
          merged = plan.model_copy(deep=True)
          merged.plan_id = dup.plan_id
          merged.created_at = dup.created_at or plan.created_at
          by_id[dup.plan_id] = merged
          result.changed = True
        result.rejected.append({"plan_id": plan.plan_id, "reason": "duplicate_official_kind"})
        continue

    # 冲突解决（§6①）：updated_at 后写胜出，完全相等服务端为准
    if existing is not None and plan.updated_at <= existing.updated_at:
      continue  # 服务端已有更新或相同，忽略上报（但仍会出现在全量下发里）

    # 额度校验（§6②）：仅对「新增」计划计数；更新已有计划不占新额度
    if existing is None or existing.deleted:
      used = sum(1 for p in by_id.values() if not p.deleted)
      if quota <= 0:
        result.rejected.append({"plan_id": plan.plan_id, "reason": "tier_not_allowed"})
        continue
      if used >= quota:
        result.rejected.append({"plan_id": plan.plan_id, "reason": "quota_exceeded"})
        continue

    _normalize(plan)
    by_id[plan.plan_id] = plan
    result.changed = True

  plans = list(by_id.values())

  # 完成判定（§6⑤/⑤b）先于唯一开启：completed 的计划不再占 active 名额
  if _apply_completion(plans, tz, now):
    result.changed = True
  if _enforce_single_active(plans):
    result.changed = True
  # 存储侧也可能存在历史脏数据（activated_at 未置空），统一归一
  for plan in plans:
    before = plan.activated_at
    _normalize(plan)
    if plan.activated_at != before:
      result.changed = True

  plans.sort(key=lambda p: (p.created_at or 0, p.plan_id))
  result.plans = plans
  return result
