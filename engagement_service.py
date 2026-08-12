"""engagement_service.py — 弹窗 / 问卷 / 陪伴足迹业务逻辑（popup_survey.md, peibanzuji.md）。

从 user_server.py 拆出（原 UserProfileServ 的职责D）。存储与并发控制通过
构造时传入的画像服务（profile_service.UserProfileServ）复用：
lock / get_profile / save_profile / _get_or_create_profile_unlocked。

站内消息历史恢复（popup_survey.md 2.1）：
  - 全局消息目录存 LevelDB 全局 KV（_meta:msg:<popup_id> 一消息一 key，append-only），
    由配置热加载钩子 _sync_message_catalog 维护，不与用户绑定
  - per-user 只复用 inbox_messages 的落地记录（created_at = 首次下发时间），
    已读/已删/展示次数一律不存（客户端 Keychain 管理）
"""
import datetime
import logging
import time
import uuid
from typing import List, Optional
from zoneinfo import ZoneInfo

from ops_config import _i18n, _load_ops_config, register_reload_hook
from user_profile import (
  UserProfile, PopupState, InboxMessage, SurveySubmission, FootprintDay,
)

# 陪伴足迹里程碑规则（peibanzuji.md ③）：连续 N 天 plan_completed=true → 一条已完成里程碑
FOOTPRINT_MILESTONE_STREAK = 5

# 全局消息目录的 LevelDB key 前缀（一消息一 key，点查友好；_meta: 前缀避免撞 uid）
MESSAGE_CATALOG_PREFIX = "_meta:msg:"

# 全量问卷提交记录的 LevelDB key 前缀（一提交一 key，append-only，运营后台按前缀全量遍历）
SURVEY_RECORD_PREFIX = "_meta:survey:"


class EngagementService:
  def __init__(self, profile_serv):
    self._ps = profile_serv
    # 配置热加载 → 同步消息目录（append-only；从配置消失的标 offline）
    register_reload_hook(self._sync_message_catalog)

  # -------------------- 站内消息目录（popup_survey.md 2.1） --------------------
  def _sync_message_catalog(self, popups: list, surveys: dict) -> None:
    """配置热加载回调：把符合落消息规则的弹窗（survey 恒落 / push_message=true）
    upsert 进全局消息目录；目录中已不在配置里的标 offline=true（不物理删，
    按 ID 精确恢复仍要能取回内容）。
    """
    now = int(time.time())
    active_ids = set()
    for popup in popups:
      if not (popup.get("push_message") or popup.get("type") == "survey"):
        continue
      pid = popup.get("popup_id")
      if not pid:
        continue
      active_ids.add(pid)
      record = {
        "popup_id": pid,
        "type": popup.get("type"),
        "i18n": popup.get("i18n") or {},
        "action_type": popup.get("action_type", "dismiss"),
        "action_payload": popup.get("action_payload") or {},
        "image_url": popup.get("image_url", ""),
        "start_at": popup.get("start_at"),
        "end_at": popup.get("end_at"),
        "priority": popup.get("priority", 0),
        "display_rule": popup.get("display_rule") or {},
      }
      key = MESSAGE_CATALOG_PREFIX + pid
      existing = self._ps.get_global(key)
      if existing and not existing.get("offline") and all(
        existing.get(f) == v for f, v in record.items()
      ):
        continue  # 内容无变化，不重写（保留 first_seen_at）
      record["first_seen_at"] = (existing or {}).get("first_seen_at") or now
      record["offline"] = False
      self._ps.put_global(key, record)

    for key, record in self._ps.iter_global_prefix(MESSAGE_CATALOG_PREFIX):
      if record.get("popup_id") not in active_ids and not record.get("offline"):
        record["offline"] = True
        self._ps.put_global(key, record)
        logging.info("message catalog offline: %s", record.get("popup_id"))

  def query_message_history(
    self, uid: str, language: str, popup_ids: Optional[List[str]]
  ) -> List[dict]:
    """scope=history 历史消息恢复（popup_survey.md 2.1）。

    不做投放定向/频控/优先级裁剪。两种取法：
      - popup_ids 非空：按 ID 精确恢复，含过期/已下线（offline）条目；未知 ID 静默忽略
      - popup_ids 空：全量兜底，该 uid 落地过的 ∩ 仍在有效期内（end_at 未到/无 且未下线）
    delivered_at = 该 uid 首次落地时间（inbox_messages.created_at），无记录退回目录 first_seen_at。
    """
    now = int(time.time())
    with self._ps.lock:
      profile = self._ps.get_profile(uid)
    landed = {m.popup_id: m.created_at for m in (profile.inbox_messages if profile else [])}

    records: list[dict] = []
    if popup_ids:
      for pid in popup_ids:
        rec = self._ps.get_global(MESSAGE_CATALOG_PREFIX + pid)
        if rec is not None:
          records.append(rec)
    else:
      for _key, rec in self._ps.iter_global_prefix(MESSAGE_CATALOG_PREFIX):
        if rec.get("popup_id") not in landed:
          continue
        end_at = rec.get("end_at")
        if end_at is not None and now > end_at:
          continue
        if rec.get("offline"):
          continue
        records.append(rec)

    messages = [
      self._catalog_record_to_message(rec, language, landed)
      for rec in records
    ]
    messages.sort(key=lambda m: m.get("delivered_at") or 0, reverse=True)
    return messages

  @staticmethod
  def _catalog_record_to_message(rec: dict, language: str, landed: dict) -> dict:
    """目录记录 → 与 query_popups 响应 popups[] 同构的消息（+ delivered_at 必填）。"""
    text = _i18n(rec, language)
    pid = rec["popup_id"]
    return {
      "popup_id": pid,
      "type": rec.get("type"),
      "badge": text.get("badge", ""),
      "badge_style": text.get("badge_style", "purple"),
      "title": text.get("title", ""),
      "subtitle": text.get("subtitle", ""),
      "image_url": rec.get("image_url", ""),
      "action_text": text.get("action_text", ""),
      "action_type": rec.get("action_type", "dismiss"),
      "action_payload": rec.get("action_payload") or {},
      "push_message": True,
      "start_at": rec.get("start_at"),
      "end_at": rec.get("end_at"),
      "delivered_at": landed.get(pid) or rec.get("first_seen_at"),
      "priority": rec.get("priority", 0),
      "display_rule": rec.get("display_rule") or {},
    }

  def query_popups(self, uid: str, language: str, placement: str = "home") -> dict:
    """拉取当前应展示的弹窗列表：按时间窗/展示位/用户频控过滤，按 priority 降序。

    返回 {"popups": [...], "next_query_after": Optional[int]}；
    next_query_after 为 None 表示服务端不下发（客户端用默认 300s）。

    同时对时间窗内 push_message=true（survey 类恒落）的弹窗落地站内消息，
    按 popup_id 去重、每条只落一次，不受频控影响。
    """
    now = int(time.time())
    popups_catalog, _, next_query_after = _load_ops_config()
    with self._ps.lock:
      profile = self._ps._get_or_create_profile_unlocked(uid)
      inbox_changed = False
      result: list[dict] = []

      for popup in popups_catalog:
        if popup.get("placement", "home") != placement:
          continue
        start_at = popup.get("start_at")
        end_at = popup.get("end_at")
        in_window = (start_at is None or start_at <= now) and (end_at is None or now <= end_at)
        if not in_window:
          continue

        # 站内消息落地（不看频控/优先级；survey 类恒落）
        if popup.get("push_message") or popup.get("type") == "survey":
          if not any(m.popup_id == popup["popup_id"] for m in profile.inbox_messages):
            text = _i18n(popup, language)
            profile.inbox_messages.append(InboxMessage(
              message_id=f"msg_{uuid.uuid4().hex[:12]}",
              popup_id=popup["popup_id"],
              title=text.get("title", ""),
              subtitle=text.get("subtitle", ""),
              action_type=popup.get("action_type", "dismiss"),
              action_payload=popup.get("action_payload") or {},
              created_at=now,
            ))
            inbox_changed = True

        # 频控过滤（display_rule 是客户端本地频控的权威参数，服务端同样执行）
        rule = popup.get("display_rule") or {}
        state = profile.popup_states.get(popup["popup_id"])
        if state is not None:
          if state.dismissed and rule.get("dismiss_stops", True):
            continue
          max_show = rule.get("max_show_count")
          if max_show is not None and state.show_count >= max_show:
            continue
          cooldown = rule.get("cooldown_seconds")
          if cooldown and state.last_impression_at and now - state.last_impression_at < cooldown:
            continue

        # survey 类弹窗：该问卷已提交则不再下发（与客户端去重口径一致）
        action_payload = popup.get("action_payload") or {}
        if popup.get("type") == "survey" and action_payload.get("survey_id") in profile.survey_submissions:
          continue

        text = _i18n(popup, language)
        # 首次对该 uid 下发时间：消息弹窗取站内信落地时间（上面已落地），纯弹窗为本次
        delivered_at = next(
          (m.created_at for m in profile.inbox_messages if m.popup_id == popup["popup_id"]),
          now,
        )
        result.append({
          "popup_id": popup["popup_id"],
          "type": popup["type"],
          "badge": text.get("badge", ""),
          "badge_style": text.get("badge_style", "purple"),
          "title": text.get("title", ""),
          "subtitle": text.get("subtitle", ""),
          "image_url": popup.get("image_url", ""),
          "action_text": text.get("action_text", ""),
          "action_type": popup.get("action_type", "dismiss"),
          "action_payload": action_payload,
          "push_message": bool(popup.get("push_message")),
          "start_at": start_at,
          "end_at": end_at,
          "delivered_at": delivered_at,
          "priority": popup.get("priority", 0),
          "display_rule": rule,
        })

      if inbox_changed or profile.popup_states:
        self._ps.save_profile(uid, profile)

      result.sort(key=lambda p: p.get("priority", 0), reverse=True)
      return {"popups": result, "next_query_after": next_query_after}

  def report_popup_event(self, uid: str, popup_id: str, event: str, event_at: int) -> bool:
    """回传弹窗曝光/点击/关闭事件，更新该用户的弹窗状态。"""
    popups_catalog, _, _ = _load_ops_config()
    if not any(p["popup_id"] == popup_id for p in popups_catalog):
      logging.warning("report_popup for unknown popup_id=%s uid=%s", popup_id, uid)
      return False
    with self._ps.lock:
      profile = self._ps._get_or_create_profile_unlocked(uid)
      state = profile.popup_states.get(popup_id) or PopupState()
      if event == "impression":
        state.show_count += 1
        state.last_impression_at = event_at
      elif event == "click":
        state.click_count += 1
      elif event == "dismiss":
        state.dismissed = True
      profile.popup_states[popup_id] = state
      self._ps.save_profile(uid, profile)
      return True

  def get_survey(self, survey_id: str, language: str) -> Optional[dict]:
    """拉取问卷题目（按语言）；未知 survey_id 返回 None。"""
    _, surveys, _ = _load_ops_config()
    survey = surveys.get(survey_id)
    if survey is None:
      return None
    text = _i18n(survey, language)
    return {
      "survey_id": survey_id,
      "title": text.get("title", ""),
      "questions": text.get("questions", []),
      "reward": text.get("reward"),
    }

  def submit_survey(self, uid: str, data, email: Optional[str] = None) -> tuple[Optional[dict], int]:
    """提交问卷。返回 (响应 data, code)；同一 uid+survey_id 幂等：
    重复提交返回既有 submission_id 且 reward_granted=False（code=0）。
    email 来自 JWT payload，仅用于运营后台记录展示。"""
    _, surveys, _ = _load_ops_config()
    survey = surveys.get(data.survey_id)
    if survey is None:
      return None, 404
    text = _i18n(survey, data.language)
    questions = text.get("questions", [])

    # 答案必须覆盖全部题目；必答题（缺省选择题 true、文本题 false）须已作答
    answers_by_qid = {a.question_id: a for a in data.answers}
    for q in questions:
      answer = answers_by_qid.get(q["question_id"])
      if answer is None:
        return None, 400
      required = q.get("required", q["type"] != "text")
      if required:
        if q["type"] in ("single_choice", "multi_choice") and not answer.option_ids:
          return None, 400
        if q["type"] == "text" and not answer.text.strip():
          return None, 400

    reward = text.get("reward") or {}
    gift_type = reward.get("gift_type", "none")
    if gift_type in ("physical", "virtual") and data.gift_delivery is None:
      return None, 400

    with self._ps.lock:
      profile = self._ps._get_or_create_profile_unlocked(uid)
      existing = profile.survey_submissions.get(data.survey_id)
      if existing is not None:
        logging.info(
          "survey duplicate submit uid=%s survey_id=%s submission_id=%s",
          uid, data.survey_id, existing.submission_id,
        )
        return {
          "submission_id": existing.submission_id,
          "reward_granted": False,
          "reward_title": text.get("reward_title", ""),
          "reward_desc": reward.get("desc", ""),
        }, 0

      submission = SurveySubmission(
        submission_id=f"sub_{uuid.uuid4().hex[:12]}",
        survey_id=data.survey_id,
        submitted_at=data.submitted_at or int(time.time()),
        duration_seconds=data.duration_seconds,
        answers=data.answers,
        gift_delivery=data.gift_delivery,
        reward_granted=gift_type != "none",
      )
      profile.survey_submissions[data.survey_id] = submission
      self._ps.save_profile(uid, profile)
      # 全量问卷记录（运营后台展示用）：一提交一 key，append-only
      self._ps.put_global(SURVEY_RECORD_PREFIX + submission.submission_id, {
        "submission_id": submission.submission_id,
        "uid": uid,
        "email": email or "",
        "survey_id": submission.survey_id,
        "language": data.language,
        "submitted_at": submission.submitted_at,
        "duration_seconds": submission.duration_seconds,
        "answers": [a.model_dump() for a in submission.answers],
        "gift_delivery": submission.gift_delivery.model_dump(exclude_none=True) if submission.gift_delivery else None,
        "reward_granted": submission.reward_granted,
      })

    logging.info(
      "survey submitted uid=%s survey_id=%s submission_id=%s duration=%ss answers=%s gift=%s",
      uid, data.survey_id, submission.submission_id, data.duration_seconds,
      {a.question_id: (a.option_ids if a.type != "text" else a.text) for a in data.answers},
      submission.gift_delivery.model_dump(exclude_none=True) if submission.gift_delivery else None,
    )
    return {
      "submission_id": submission.submission_id,
      "reward_granted": submission.reward_granted,
      "reward_title": text.get("reward_title", ""),
      "reward_desc": reward.get("desc", ""),
    }, 0

  def list_survey_records(self, survey_id: Optional[str] = None) -> list[dict]:
    """全量问卷提交记录（运营后台用），按提交时间倒序；可按 survey_id 过滤。"""
    records = [rec for _key, rec in self._ps.iter_global_prefix(SURVEY_RECORD_PREFIX)]
    if survey_id:
      records = [r for r in records if r.get("survey_id") == survey_id]
    records.sort(key=lambda r: r.get("submitted_at") or 0, reverse=True)
    return records

  def merge_footprint_days(self, uid: str, days: List[FootprintDay]) -> int:
    """上传陪伴足迹：按 uid+date 幂等合并（布尔取 OR、计数取大、首活跃取小）。返回接受的天数。"""
    with self._ps.lock:
      profile = self._ps._get_or_create_profile_unlocked(uid)
      for day in days:
        existing = profile.footprint_days.get(day.date)
        if existing is None:
          profile.footprint_days[day.date] = day
          continue
        existing.app_active = existing.app_active or day.app_active
        existing.sleep_companion = existing.sleep_companion or day.sleep_companion
        existing.plan_completed = existing.plan_completed or day.plan_completed
        existing.app_open_count = max(existing.app_open_count, day.app_open_count)
        existing.companion_minutes = max(existing.companion_minutes, day.companion_minutes)
        if day.first_active_at is not None:
          if existing.first_active_at is None or day.first_active_at < existing.first_active_at:
            existing.first_active_at = day.first_active_at
      self._ps.save_profile(uid, profile)
    return len(days)

  @staticmethod
  def _footprint_milestones(days_map: dict[str, FootprintDay]) -> list[dict]:
    """按运营规则扫描日记录生成里程碑：连续 N 天 plan_completed=true → 一条已完成里程碑；
    最近一段未达 N 天的连续记录作为进行中里程碑返回。"""
    plan_dates = sorted(
      datetime.date.fromisoformat(d.date)
      for d in days_map.values()
      if d.plan_completed
    )
    if not plan_dates:
      return []

    streaks: list[list[datetime.date]] = [[plan_dates[0]]]
    for d in plan_dates[1:]:
      if (d - streaks[-1][-1]).days == 1:
        streaks[-1].append(d)
      else:
        streaks.append([d])

    def _fmt(d: datetime.date) -> str:
      return f"{d.year}.{d.month}.{d.day}"

    milestones: list[dict] = []
    for streak in streaks:
      if len(streak) >= FOOTPRINT_MILESTONE_STREAK:
        milestones.append({
          "milestone_id": f"ms_plan_{streak[0].isoformat()}",
          "title": "睡眠计划",
          "date_range": f"{_fmt(streak[0])}-{_fmt(streak[-1])}",
          "desc": f"{len(streak)} 日睡眠目标·已完成",
          "completed": True,
        })
    current = streaks[-1]
    if len(current) < FOOTPRINT_MILESTONE_STREAK:
      milestones.append({
        "milestone_id": f"ms_plan_{current[0].isoformat()}",
        "title": "睡眠计划",
        "date_range": f"{_fmt(current[0])}-{_fmt(current[-1])}",
        "desc": f"{FOOTPRINT_MILESTONE_STREAK} 日睡眠目标·已完成 {len(current)}/{FOOTPRINT_MILESTONE_STREAK}",
        "completed": False,
      })
    return milestones

  @staticmethod
  def _footprint_day_entry(day: FootprintDay) -> dict:
    return {
      "day": int(day.date.split("-")[2]),
      "app_active": day.app_active,
      "sleep_companion": day.sleep_companion,
      "plan_completed": day.plan_completed,
    }

  def query_footprint(self, uid: str, scope: str, year: int, month: Optional[int], timezone: str) -> dict:
    """拉取陪伴足迹汇总：计数统计、锚定日期、日记录与里程碑。"""
    try:
      tz = ZoneInfo(timezone)
    except Exception:
      logging.warning("unknown timezone=%s, fallback UTC", timezone)
      tz = ZoneInfo("UTC")
    today = datetime.datetime.now(tz).date()

    with self._ps.lock:
      profile = self._ps.get_profile(uid)
      days_map = dict(profile.footprint_days) if profile else {}

    # 锚定日期：最近一条有数据（≤今天）的日期
    marked = sorted(
      d.date for d in days_map.values()
      if (d.app_active or d.sleep_companion or d.plan_completed)
      and datetime.date.fromisoformat(d.date) <= today
    )
    anchor_date = marked[-1] if marked else today.isoformat()
    weekday = datetime.date.fromisoformat(anchor_date).isoweekday()

    # 连续使用：该自然年内有任意使用记录的天数累计（非连续 streak）
    year_prefix = f"{year:04d}-"
    continuous_use_year = sum(
      1 for d in days_map.values()
      if d.date.startswith(year_prefix) and (d.app_active or d.sleep_companion)
    )

    if scope == "year":
      months = []
      for m in range(1, 13):
        prefix = f"{year:04d}-{m:02d}-"
        month_days = [
          self._footprint_day_entry(d) for d in sorted(days_map.values(), key=lambda x: x.date)
          if d.date.startswith(prefix) and (d.app_active or d.sleep_companion or d.plan_completed)
        ]
        months.append({"month": m, "days": month_days})
      return {
        "year": year,
        "stats": {"continuous_use_year": continuous_use_year},
        "months": months,
      }

    prefix = f"{year:04d}-{month:02d}-"
    month_records = [d for d in days_map.values() if d.date.startswith(prefix)]
    marked_days = [
      self._footprint_day_entry(d) for d in sorted(month_records, key=lambda x: x.date)
      if d.app_active or d.sleep_companion or d.plan_completed
    ]
    return {
      "anchor_date": anchor_date,
      "weekday": weekday,
      "stats": {
        "sleep_companion_month": sum(1 for d in month_records if d.sleep_companion),
        "app_active_month": sum(1 for d in month_records if d.app_active),
        "continuous_use_year": continuous_use_year,
      },
      "year": year,
      "month": month,
      "days": marked_days,
      "milestones": self._footprint_milestones(days_map),
    }
