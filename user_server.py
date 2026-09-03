"""user_server.py — Mindora user server 对外 HTTP API 层。

只保留 API 关注点：路由、鉴权（RS256 公钥验签）、请求 handler、远端同步、进程启动。
业务实现已拆分为：
  - profile_service.py     画像存储 / 行为聚合 / 更新编排（UserProfileServ 门面）
  - analysis_content.py    LLM 分析内容生成与检索
  - analysis_builders.py   /analysis 响应骨架（纯函数）
  - engagement_service.py  弹窗 / 问卷 / 陪伴足迹
  - ops_config.py          弹窗问卷运营配置加载
"""
import asyncio,copy,datetime,hashlib,json,logging,os,re,time
from typing import Any, Optional
from dotenv import load_dotenv
import jwt
from pydantic import BaseModel, ValidationError
from aiohttp import ClientSession, web

import analysis_builders
import profile_query_gate
from analysis_content import AnalysisContentService
from config import Config
from common.jwt_keys import verify_token
from profile_service import UserProfileServ
from user_profile import (
  UserProfile, ProfileRequest, ProfileResponse, ProfileData,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp, BaseResponse,
  AnalysisRequest, AnalysisResponse,
  PopupRequest, SurveyRequest, FootprintRequest,
)
from auth import AuthRequest, AuthData
from ops_config import append_popup, save_survey
from uid.uuid import get_or_create_uuid
from llm import SleepAnalysisLLM, deep_merge
import sleep_plan_service
from user_profile import SleepPlanSyncRequest
import logger
import copy

load_dotenv()
run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))
logger.init_log(f"{run_dir}/user_server_logs")
# JWT 验签改用 RS256 公钥（见 common/jwt_keys.py），不再需要本地保存签名密钥


def get_http_status(resp: BaseResponse):
  status = 200
  if resp.code != 0:
    status = resp.code
  return status


# -------------------- 弹窗主图上传（ops 后台） --------------------
# 运营在发布页可贴 URL 或直接上传图片；上传的文件按内容哈希命名存
# data/popup_images/，经 GET /popup_images/<name> 公开访问（App 按完整 URL 拉取）。
POPUP_IMAGE_MAX_BYTES = 2 * 1024 * 1024  # 弹窗主图约 590×286，2MB 上限足够

# 魔数 → (扩展名, Content-Type)；不信任上传文件名里的扩展名
_POPUP_IMAGE_MAGIC = (
  (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
  (b"\xff\xd8\xff", "jpg", "image/jpeg"),
  (b"RIFF", "webp", "image/webp"),  # 需再确认偏移 8 处为 WEBP
)
_POPUP_IMAGE_NAME_RE = re.compile(r"^[a-f0-9]{16}\.(png|jpg|webp)$")


def _popup_image_dir() -> str:
  d = os.path.join(run_dir, "data", "popup_images")
  os.makedirs(d, exist_ok=True)
  return d


def save_popup_image(data: bytes) -> tuple[Optional[str], Optional[str]]:
  """校验并保存弹窗主图，返回 (公开 URL, 错误信息)。同名内容幂等（哈希命名）。"""
  if not data:
    return None, "空文件"
  if len(data) > POPUP_IMAGE_MAX_BYTES:
    return None, f"图片超过 {POPUP_IMAGE_MAX_BYTES // 1024 // 1024}MB 上限"
  ext = mime = None
  for magic, e, m in _POPUP_IMAGE_MAGIC:
    if data.startswith(magic):
      ext, mime = e, m
      break
  if ext == "webp" and data[8:12] != b"WEBP":
    ext = None
  if ext is None:
    return None, "仅支持 png / jpg / webp 图片"
  name = hashlib.sha1(data).hexdigest()[:16] + "." + ext
  path = os.path.join(_popup_image_dir(), name)
  if not os.path.exists(path):
    with open(path, "wb") as f:
      f.write(data)
  return f"{Config.PUBLIC_API_BASE}/popup_images/{name}", None


class UserServer:
  @staticmethod
  def _request_for_log(req_or_data) -> Any:
    """Return a log-safe copy: jwt_token 打码，behaviors 按条数汇总。"""
    if isinstance(req_or_data, BaseModel):
      data = req_or_data.model_dump(mode="json", exclude_none=True)
    elif isinstance(req_or_data, dict):
      data = copy.deepcopy(req_or_data)
    else:
      return req_or_data

    d = data.get("data")
    if isinstance(d, dict) and d.get("jwt_token"):
      d["jwt_token"] = "***"

    up = ((data.get("data") or {}).get("user_profile") or {})
    if isinstance(up.get("behaviors"), dict):
      up["behaviors"] = {
        k: len(v) if isinstance(v, list) else v for k, v in up["behaviors"].items()
      }
    return data

  @staticmethod
  def _update_summary_for_log(up) -> str:
    """update_profile 请求体的一行结构摘要：只看客户端实际传了哪些键
    （model_fields_set，决定合并语义"键不出现→保持原值"）和数据规模，
    不打原始内容（心率序列、个人资料属隐私）。"""
    if up is None:
      return "user_profile=None"
    parts = [f"keys={sorted(up.model_fields_set)}"]
    behaviors = up.behaviors or {}
    if behaviors:
      parts.append("behaviors={" + ",".join(
        f"{k}:{len(v)}" if isinstance(v, list) else str(k)
        for k, v in behaviors.items()
      ) + "}")
    sd = up.sleep_data or []
    if sd:
      latest = max((int(r.timestamp or 0) for r in sd), default=0)
      parts.append(f"sleep_data={len(sd)}晚 latest_ts={latest}")
    prof = getattr(up, "profile", None)
    if prof is not None:
      parts.append(f"profile_keys={sorted(prof.model_fields_set)}")
    return " ".join(parts)

  def __init__(self):
    self.server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
    self.host = Config.HOST
    self.port = Config.PORT
    self.llm = SleepAnalysisLLM()
    self.user_serv = UserProfileServ(llm=self.llm)
    self.app = web.Application()
    self.system_uid = get_or_create_uuid()
    self.debug_uid_set = {"mindora_test_uid1", "mindora_test_uid2", "mindora_test_uid3", "test_debug_user_001"}
    # Per-user LLM background-update rate limiting and task tracking.
    self._llm_tracker_lock = asyncio.Lock()
    self._llm_update_tracker: dict[str, float] = {}
    self._llm_tasks: set[asyncio.Task] = set()
    self._llm_semaphore = asyncio.Semaphore(Config.MAX_LLM_BACKGROUND_TASKS)
    # 醒后自动触发（方案A）：uid -> 防抖中的调度任务 / 上次触发时最新 sleep_data ts。
    # 判定所需的状态（最新报告日期、最新夜晚 ts）都在画像里，重启后可重建；
    # 这两个内存结构只做"别重复烧 LLM"的节流，丢了最多多扫一次。
    self._llm_pending: dict[str, asyncio.Task] = {}
    self._llm_last_attempt: dict[str, int] = {}
    # 活跃表：uid -> 最近一次活跃信号的时间戳（time.time()）。
    # 收窄后的活跃信号只认两类：/analysis 请求（用户在看法分析页）、
    # plays 事件（设备/场景真实使用）。query_profile 等纯打开不算。
    self._activity: dict[str, float] = {}
    # 睡眠计划：uid -> (effective_user_level, fetched_at)，auth_server 查询短缓存
    self._tier_cache: dict[str, tuple[str, float]] = {}
    self.setup_routes()

  def close(self):
    self.user_serv.close()
    for attr in ("_sweeper_task", "_activity_log_task"):
      task = getattr(self, attr, None)
      if task:
        task.cancel()
    # Cancel pending LLM background tasks.
    for task in list(self._llm_tasks):
      task.cancel()
    self._llm_tasks.clear()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/user_profile', self.handle_profile_request_http)
    self.app.router.add_post('/login', self.handle_login_http)
    self.app.router.add_post('/analysis', self.handle_analysis_http)
    self.app.router.add_post('/popup', self.handle_popup_http)
    self.app.router.add_post('/survey', self.handle_survey_http)
    self.app.router.add_post('/companion_footprint', self.handle_footprint_http)
    self.app.router.add_post('/sleep_plan', self.handle_sleep_plan_http)
    # 运营后台接口（管理员校验：JWT + auth_server 查 ops_role）
    self.app.router.add_post('/ops/push', self.handle_ops_push_http)
    self.app.router.add_post('/ops/survey_records', self.handle_ops_survey_records_http)
    self.app.router.add_post('/ops/publish_logs', self.handle_ops_publish_logs_http)
    self.app.router.add_post('/ops/popup_meta', self.handle_ops_popup_meta_http)
    self.app.router.add_post('/ops/save_survey', self.handle_ops_save_survey_http)
    self.app.router.add_post('/ops/survey_list', self.handle_ops_survey_list_http)
    self.app.router.add_post('/ops/upload_image', self.handle_ops_upload_image_http)
    # 弹窗主图公开访问（文件名是内容哈希，长缓存；路径即 ops/upload_image 返回的 URL）
    self.app.router.add_get('/popup_images/{name}', self.handle_popup_image_http)

  # -------------------- 鉴权 --------------------
  def _check_token(self, jwt_token: str)-> dict | None:
    try:
      payload = verify_token(jwt_token)
    except jwt.ExpiredSignatureError:
      logging.error("login token expired")
      return None
    except jwt.InvalidTokenError:
      logging.error("login token invalid")
      return None


    logging.info(f"payload: {payload}")
    return payload

  def _parse_for_uid(self, data: Any):
    uid = None
    if data.jwt_token is not None:
      payload = self._check_token(data.jwt_token)
      if payload is None:
        return None
      uid = payload.get("uid")
    elif Config.IS_DEBUG and data.uid is not None and len(data.uid) > 3 and data.uid in self.debug_uid_set:
      uid = data.uid

    return uid

  def _parse_uid_email(self, data: Any) -> tuple[Optional[str], Optional[str]]:
    """解析 (uid, email)；email 来自 JWT payload（问卷记录展示用），debug uid 无 email。"""
    if data.jwt_token is not None:
      payload = self._check_token(data.jwt_token)
      if payload is None:
        return None, None
      return payload.get("uid"), payload.get("email")
    if Config.IS_DEBUG and data.uid is not None and len(data.uid) > 3 and data.uid in self.debug_uid_set:
      return data.uid, None
    return None, None

  # -------------------- /user_profile --------------------
  def handle_query_profile(self, request: ProfileRequest) -> BaseResponse:
    logging.info("handle query_profile request=%s", self._request_for_log(request))
    """查询用户画像（从LevelDB按需读取）"""
    if request.data is None:
      logging.error("query request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"get uid: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    # uid 时段黑名单（profile_query_gate）：名单内的 uid 只在允许时段可拉全量画像，
    # 时段外直接 403；update_profile / query_revision 不受影响
    if not profile_query_gate.is_query_allowed(uid):
      logging.warning("query_profile denied by time gate uid=%s", uid)
      return ProfileResponse(
        code=403, msg="query_profile is not allowed for this uid at this time",
        request_type=request.request_type, data=None,
      )

    profile = self.user_serv.get_profile(uid)
    if profile:
      logging.info("profile found uid=%s summary=%s", uid, self.user_serv._profile_for_log(profile))
      profile_dict = profile.model_dump()
      # 按请求开关裁剪体积大头：sleep_data / behaviors（不携带 behaviors 时同时去掉 health_sync_days）
      if not request.data.include_sleep_data:
        profile_dict.pop("sleep_data", None)
      if not request.data.include_behaviors:
        profile_dict.pop("behaviors", None)
        profile_dict.pop("health_sync_days", None)
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data={"user_profile": profile_dict})
    else:
      logging.warning("uid=%s query not found request=%s", uid, self._request_for_log(request))
      return ProfileResponse(code=0, msg=f"User with uid '{uid}' not found", request_type=request.request_type, data=None)

    # incr update the behaviors by time, and update long term weight
  def handle_query_revision(self, request: ProfileRequest) -> BaseResponse:
    """轻量变更探测：只返回画像当前 revision（几十字节）。

    设备端两级刷新：3s 探测本接口，revision 与本地基线一致则不动作，变了才全量
    query_profile + LWW 合并；300s 强制全量兜底。设备自己推云成功后以 update_profile
    响应里的新 revision 为基线（回显过滤）。未知用户返回 revision=0。
    """
    if request.data is None:
      return InvalidOrExpiredTokenResp()
    uid = self._parse_for_uid(request.data)
    if uid is None:
      return InvalidOrExpiredTokenResp()
    profile = self.user_serv.get_profile(uid)
    return ProfileResponse(
      code=0, msg="success", request_type=request.request_type,
      data={"revision": profile.revision if profile else 0},
    )

  def handle_query_health_sync_state(self, request: ProfileRequest) -> BaseResponse:
    """健康数据对账（健康数据同步接口_0814.md §8.4）：返回窗口内已有数据的天+口径版本。"""
    if request.data is None:
      return InvalidOrExpiredTokenResp()
    uid = self._parse_for_uid(request.data)
    if uid is None:
      return InvalidOrExpiredTokenResp()

    d = request.data
    try:
      start = datetime.date.fromisoformat(d.start_date or "")
      end = datetime.date.fromisoformat(d.end_date or "")
    except ValueError:
      return ProfileResponse(
        code=400, msg="start_date/end_date required (yyyy-MM-dd)",
        request_type=request.request_type, data=None,
      )
    if start > end:
      return ProfileResponse(
        code=400, msg="start_date must not be after end_date",
        request_type=request.request_type, data=None,
      )

    days = self.user_serv.query_health_sync_days(uid, d.start_date, d.end_date, d.timezone)
    if days is None:
      return ProfileResponse(
        code=0, msg=f"User with uid '{uid}' not found",
        request_type=request.request_type, data={"days": []},
      )
    return ProfileResponse(
      code=0, msg="success", request_type=request.request_type, data={"days": days},
    )

  async def handle_update_profile(self, request: ProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    if request.data is None:
      logging.error("update request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(
      "update_profile request uid=%s skip_reco=%s skip_analysis=%s hs_ver=%s tz=%s lang=%s | %s",
      uid,
      request.data.skip_sleep_scenarios_reco_update,
      request.data.skip_sleep_analysis_update,
      request.data.health_schema_version,
      request.data.timezone,
      request.data.language,
      self._update_summary_for_log(request.data.user_profile),
    )

    if uid is None:
      return InvalidOrExpiredTokenResp()

    async with self.server_semaphore:
      succ = await asyncio.to_thread(
        self.user_serv.update_profile_basic,
        uid,
        request.data.user_profile,
        request.data.health_schema_version,
        request.data.timezone,
        request.data.language,
      )
    if not succ:
      return ProfileResponse(code=500, msg=f"update profile failed", request_type=request.request_type, data=None)

    # 个人资料同步约定 §2/§3：msg 回显 uid（不用 timestamp）；返回合并后的完整 profile，
    # 客户端用它更新本地快照基准（服务端归一化后两边才对得上）
    merged = await asyncio.to_thread(self.user_serv.get_profile, uid)

    # 本批含 plays（场景/设备真实使用）→ 记活跃信号
    up = request.data.user_profile
    if up is not None and (up.behaviors or {}).get("plays"):
      self._mark_activity(uid)

    # LLM 触发（方案A：醒后每天一次）：客户端不传开关时由服务端按数据自动决策
    await self._maybe_schedule_llm_update(
      uid,
      merged,
      request.data.skip_sleep_scenarios_reco_update,
      request.data.skip_sleep_analysis_update,
    )

    return ProfileResponse(
      code=0,
      msg=f"update profile for '{uid}' succ",
      request_type=request.request_type,
      data={"user_profile": merged.model_dump()} if merged else None,
    )

  # -------------------- 活跃门（醒后预生成的成本闸门） --------------------
  def _mark_activity(self, uid: str) -> None:
    """记录一次活跃信号（/analysis 请求或 plays 批次）。"""
    if uid:
      self._activity[uid] = time.time()

  def _is_active(self, uid: str, profile) -> bool:
    """活跃门：窗口（LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS）内有活跃信号。

    两个来源（只认这两类，query_profile 等纯打开不算）：
      - 内存活跃表：/analysis 请求、含 plays 的 update 批次（实时，重启丢失）
      - 持久化兜底：画像 behaviors.plays 的最新事件时间戳（重启后仍正确）
    """
    now = time.time()
    window = Config.LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS
    if now - self._activity.get(uid, 0) < window:
      return True
    plays = ((profile.behaviors or {}).get("plays")) if profile else None
    if plays:
      try:
        latest = max(int(p[0]) for p in plays if p)
        return now - latest < window
      except (TypeError, ValueError, IndexError):
        return False
    return False

  @staticmethod
  def _newest_sleep_ts(profile) -> Optional[int]:
    """画像中最新一晚 sleep_data 的时间戳（醒来的那一刻）；无数据返回 None。"""
    if not profile or not profile.sleep_data:
      return None
    return max((int(r.timestamp or 0) for r in profile.sleep_data), default=0) or None

  def _analysis_needed(self, profile) -> tuple[bool, Optional[int]]:
    """每日触发门：最新一夜（按画像最近时区归日）是否还没有对应的日级分析报告。

    返回 (是否需要, 最新夜晚 ts)。状态全部来自持久化画像，重启后判定不变：
      - 有夜晚但从未生成过日报告 → 需要
      - 最新夜晚日期 > 最新日报告日期 → 需要（醒来了新的一天）
      - 最新夜晚日期 == 最新日报告日期，但最新报告是零数据兜底（llm_used=False）
        → 需要（第一晚到达同日，兜底应被真分析替换）
      - 最新夜晚日期 == 最新真实日报告日期 → 不需要（今天的已生成，零散包不重复烧 LLM）
    """
    newest_ts = self._newest_sleep_ts(profile)
    if newest_ts is None:
      return False, None
    tz = UserProfileServ._resolve_tz(getattr(profile, "last_request_timezone", None), warn=False)
    newest_night = datetime.datetime.fromtimestamp(newest_ts, tz).date().isoformat()
    day_reports = (profile.analysis_reports or {}).get("analysis_sleep_day") or []
    latest_report = max(
      (r for r in day_reports if r.date),
      key=lambda r: (r.date, r.generated_at or 0),
      default=None,
    )
    if latest_report is None:
      return True, newest_ts
    if newest_night > latest_report.date:
      return True, newest_ts
    if not latest_report.llm_used:
      return True, newest_ts
    return False, newest_ts

  async def _maybe_schedule_llm_update(
    self,
    uid: str,
    profile,
    skip_reco: Optional[bool],
    skip_analysis: Optional[bool],
  ) -> None:
    """三态开关 + 醒后自动触发的决策层。

    skip_* 语义：True=客户端要求跳过；False=客户端要求立即强制（跳过防抖）；
    None=服务端自动——有未分析的新夜晚且活跃门通过（窗口内有 /analysis 请求
    或 plays 事件）才调度，带防抖等零散包落地。纯设备后台同步、app 不活跃的
    用户不做预生成（他们打开分析页时由读路径懒触发兜底）。
    """
    auto_needed, newest_ts = False, None
    if Config.LLM_ANALYSIS_AUTO_TRIGGER:
      auto_needed, newest_ts = self._analysis_needed(profile)
      if auto_needed and self._llm_last_attempt.get(uid) == newest_ts:
        # 已经为这一夜尝试过一次（可能失败了）：同夜的零散包不再触发，
        # 交给每小时兜底扫描重试，避免 LLM 故障时被碎片化上传反复点燃
        auto_needed = False
      if auto_needed and not self._is_active(uid, profile):
        logging.info("skip auto llm for uid=%s: inactive (no /analysis or plays in window)", uid)
        auto_needed = False

    run_reco = skip_reco is False or (auto_needed and skip_reco is not True)
    run_analysis = skip_analysis is False or (auto_needed and skip_analysis is not True)
    if not (run_reco or run_analysis):
      return

    forced = skip_reco is False or skip_analysis is False
    delay = 0 if forced else Config.LLM_ANALYSIS_DEBOUNCE_SECONDS
    if auto_needed and newest_ts is not None:
      self._llm_last_attempt[uid] = newest_ts
    await self._schedule_llm_update_if_needed(uid, not run_reco, not run_analysis, delay=delay)

  async def _schedule_llm_update_if_needed(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
    delay: float = 0,
  ) -> None:
    """Schedule a background LLM update for uid unless one ran recently or is pending."""
    async with self._llm_tracker_lock:
      pending = self._llm_pending.get(uid)
      if pending is not None and not pending.done():
        # 防抖窗口内已有任务：它运行时才读最新画像，后续零散包自动并入
        return
      now = time.monotonic()
      last = self._llm_update_tracker.get(uid)
      if last is not None and now - last < Config.LLM_UPDATE_COOLDOWN_SECONDS:
        logging.info("skip background llm update for uid=%s (last %.1fs ago)", uid, now - last)
        return
      self._llm_update_tracker[uid] = now

      task = asyncio.create_task(
        self._run_llm_update(uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update, delay)
      )
      self._llm_pending[uid] = task
      self._llm_tasks.add(task)
      task.add_done_callback(self._llm_tasks.discard)

  async def _run_llm_update(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
    delay: float = 0,
  ) -> None:
    """Run LLM work in a background task."""
    logging.info(
      "start background llm update for uid=%s (skip_reco=%s skip_analysis=%s delay=%.0fs)",
      uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update, delay,
    )
    try:
      if delay > 0:
        await asyncio.sleep(delay)  # 防抖：等醒后的零散健康数据/修正包落地
      async with self._llm_semaphore:
        await asyncio.to_thread(
          self.user_serv.update_profile_llm,
          uid,
          skip_sleep_scenarios_reco_update,
          skip_sleep_analysis_update,
        )
      logging.info("background llm update done for uid=%s", uid)
    except asyncio.CancelledError:
      logging.info("background llm update cancelled for uid=%s", uid)
      raise
    except Exception as e:
      logging.error("background llm update failed for uid=%s: %s", uid, e)

  async def handle_profile_request_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      req = ProfileRequest.model_validate(data)
      # logging.info("request %s", self._request_for_log(req))

      if req.request_type == "query_profile":
        response_obj = self.handle_query_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "update_profile":
        response_obj = await self.handle_update_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "query_revision":
        response_obj = self.handle_query_revision(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "query_health_sync_state":
        response_obj = self.handle_query_health_sync_state(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      # client_request.md：/user_profile 只承载 query_profile / update_profile /
      # query_revision / query_health_sync_state，分析类请求统一走 /analysis，洞察报告由 analysis_explore 出口
      else:
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)

    except ValidationError as e:
        logging.error(f"Validation error: {e}")
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /login --------------------
  def handle_login(self, request: AuthRequest) -> BaseResponse:
    if request.data is None or request.data.jwt_token is None:
      return InvalidReqFormatResp()

    payload = self._check_token(request.data.jwt_token)
    if payload is None:
      return InvalidOrExpiredTokenResp()

    uid = payload.get("uid")
    return BaseResponse(code=0, msg="user ativated successufully")

  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      request = AuthRequest.model_validate(data)
      response_obj = self.handle_login(request)
    except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as e:
      logging.error(f"login error: {e}")
      response_obj = InvalidReqFormatResp()

    logging.info(f"login resp: {response_obj}")
    return web.json_response(status=get_http_status(response_obj), data=response_obj.model_dump())

  # -------------------- /analysis --------------------
  async def handle_analysis_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = AnalysisRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)
      if isinstance(uid, BaseResponse):
        return web.json_response(uid.model_dump(), status=uid.code)

      # 用户在看法分析页 = 活跃信号（收窄后只认 /analysis 和 plays）
      self._mark_activity(uid)

      # 读路径登记请求环境（fill_only：只补缺，不覆盖 update 路径记下的真实值；
      # 每日触发门的自然日口径与文案语言都以画像上的这个值为准）
      await asyncio.to_thread(
        self.user_serv.note_request_meta, uid, req.data.timezone, req.data.language,
      )
      profile = await asyncio.to_thread(self.user_serv.get_profile, uid)

      # 零睡眠记录用户：读路径即时补齐兜底建议（模板，无 LLM 成本），
      # 首次请求就能见到内容；有真实数据后由后台 LLM 更新自然替换（兜底 llm_used=False）
      if profile is not None and not profile.sleep_data:
        fallback_changed = await asyncio.to_thread(
          self.user_serv.content.ensure_fallback_content, uid, profile,
        )
        if fallback_changed:
          await asyncio.to_thread(self.user_serv.save_profile, uid, profile)

      response_data = self._build_analysis_data(req, profile)

      # 读路径懒触发：有未分析的新夜晚 → 立即后台生成（本次先回骨架，
      # 文案下次刷新自然出现）。兜住活跃门漏判/预生成未覆盖的活跃用户。
      needed, newest_ts = self._analysis_needed(profile)
      if needed and newest_ts is not None:
        self._llm_last_attempt[uid] = newest_ts
        await self._schedule_llm_update_if_needed(uid, delay=0)

      # LLM 文案全部来自 update_profile 时异步生成、按日/周/月序列存储的报告，
      # 请求时纯查库，不再同步调用 LLM；未命中则只回数值骨架（文案为空/默认）
      report = UserProfileServ._find_analysis_report(
        profile, req.request_type, req.data.date, req.data.start_date, req.data.end_date,
      )
      if report is not None and (report.language or "en") != (req.data.language or "en"):
        # 文本返回约定：文案必须按 data.language 返回。语言不匹配的库存报告不合并
        # （回退骨架/客户端本地默认），并按新语言触发后台重生成
        logging.info(
          "skip report merge: language mismatch report=%s req=%s rt=%s uid=%s",
          report.language, req.data.language, req.request_type, uid,
        )
        report = None
        await self._schedule_llm_update_if_needed(uid, delay=0)
      if report:
        # 只合并客户端请求的模块，保持 modules 分字段查询语义不被库存报告击穿
        updates = report.modules
        if req.data.modules:
          updates = {k: v for k, v in updates.items() if k in req.data.modules}
        deep_merge(response_data, updates)

      resp = AnalysisResponse(code=0, msg="success", request_type=req.request_type, data=response_data)
      return web.json_response(resp.model_dump())

    except ValidationError as e:
      logging.error(f"analysis validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.error(f"analysis error: {e}")
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # ---- /analysis 骨架组装：委托给 analysis_builders（纯函数） ----
  def get_overall_score(self, profile: Optional[UserProfile]) -> Optional[float]:
    return analysis_builders.get_overall_score(profile)

  def _filter_modules(self, data: dict, modules: list) -> dict:
    return analysis_builders.filter_modules(data, modules)

  def _build_analysis_data(self, req: AnalysisRequest, profile: Optional[UserProfile]) -> dict:
    d = req.data
    rt = req.request_type
    if rt == "analysis_overview":
      return self._build_overview(d, profile)
    elif rt == "analysis_sleep_day":
      return self._build_sleep_day(d, profile)
    elif rt == "analysis_sleep_week":
      return self._build_sleep_week(d, profile)
    elif rt == "analysis_sleep_month":
      return self._build_sleep_month(d, profile)
    elif rt == "analysis_explore":
      return self._build_explore(d, profile)
    raise ValueError(f"Unknown request_type: {rt}")

  def _build_overview(self, d, profile: Optional[UserProfile]) -> dict:
    return analysis_builders.build_overview(d, profile)

  def _build_sleep_day(self, d, profile: Optional[UserProfile]) -> dict:
    return analysis_builders.build_sleep_day(d, profile)

  def _build_sleep_week(self, d, profile: Optional[UserProfile]) -> dict:
    return analysis_builders.build_sleep_week(d, profile)

  def _build_sleep_month(self, d, profile: Optional[UserProfile]) -> dict:
    return analysis_builders.build_sleep_month(d, profile)

  def _build_explore(self, d, profile: Optional[UserProfile]) -> dict:
    return analysis_builders.build_explore(d, profile)

  # -------------------- /popup（tanchuang_suvey.md） --------------------
  async def handle_popup_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = PopupRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "query_popups":
        # scope=history（popup_survey.md 2.1 历史消息恢复）：不做定向/频控/优先级，
        # 不带 next_query_after（非轮询，客户端仅登录后调一次）
        if req.data.scope == "history":
          messages = await asyncio.to_thread(
            self.user_serv.query_message_history, uid, req.data.language, req.data.popup_ids,
          )
          resp = BaseResponse(code=0, msg="ok")
          return web.json_response({**resp.model_dump(), "data": {"popups": messages}})

        result = await asyncio.to_thread(
          self.user_serv.query_popups, uid, req.data.language, req.data.placement,
        )
        # next_query_after 未配置时不下发该字段，客户端用默认 300s
        data = {"popups": result["popups"]}
        if result.get("next_query_after") is not None:
          data["next_query_after"] = result["next_query_after"]
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": data})

      # report_popup
      event_at = req.data.event_at or req.timestamp
      ok = await asyncio.to_thread(
        self.user_serv.report_popup_event, uid, req.data.popup_id, req.data.event, event_at,
      )
      if not ok:
        return web.json_response(BaseResponse(code=400, msg="unknown popup_id").model_dump(), status=400)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {}})

    except ValidationError as e:
      logging.error(f"popup validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("popup error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /survey（tanchuang_suvey.md） --------------------
  async def handle_survey_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = SurveyRequest.model_validate(body)
      uid, email = self._parse_uid_email(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "query_survey":
        survey = self.user_serv.get_survey(req.data.survey_id, req.data.language)
        if survey is None:
          return web.json_response(BaseResponse(code=404, msg="survey not found").model_dump(), status=404)
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": survey})

      # submit_survey（email 存入全量记录，供运营后台展示）
      data, code = await asyncio.to_thread(self.user_serv.submit_survey, uid, req.data, email)
      if data is None:
        msg = "survey not found" if code == 404 else "invalid survey answers"
        return web.json_response(BaseResponse(code=code, msg=msg).model_dump(), status=code)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})

    except ValidationError as e:
      logging.error(f"survey validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("survey error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /companion_footprint（peibanzuji.md） --------------------
  async def handle_footprint_http(self, request: web.Request) -> web.Response:
    try:
      body = await request.json()
      req = FootprintRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      if req.request_type == "upload_footprint":
        accepted = await asyncio.to_thread(self.user_serv.merge_footprint_days, uid, req.data.days)
        resp = BaseResponse(code=0, msg="ok")
        return web.json_response({**resp.model_dump(), "data": {"accepted_days": accepted}})

      # query_footprint
      data = await asyncio.to_thread(
        self.user_serv.query_footprint,
        uid, req.data.scope, req.data.year, req.data.month, req.data.timezone,
      )
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})

    except ValidationError as e:
      logging.error(f"footprint validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("footprint error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /sleep_plan（睡眠计划同步接口.md） --------------------
  async def _get_effective_level(self, uid: str, jwt_token: Optional[str]) -> str:
    """查询用户当前生效等级（auth_server query_user_rights），60s 内存缓存。

    查询失败按 free 降级且不缓存（下次重试）：只影响「新增计划」的额度判定，
    已有计划的更新/删除不受影响；客户端有乐观更新 + 重传队列，可自愈。
    """
    now = time.time()
    cached = self._tier_cache.get(uid)
    if cached and now - cached[1] < Config.SLEEP_PLAN_TIER_CACHE_SECONDS:
      return cached[0]

    level = "free"
    if jwt_token:
      try:
        req = AuthRequest(
          request_type="query_user_rights",
          timestamp=int(now),
          version="1.0",
          data=AuthData(jwt_token=jwt_token),
        )
        async with ClientSession() as session:
          async with session.post(
            f"{Config.AUTH_SERVER_URL}/auth",
            json=req.model_dump(mode="json"),
            timeout=3,
          ) as resp:
            body = await resp.json()
        level = ((body or {}).get("data") or {}).get("effective_user_level") or "free"
      except Exception as e:
        logging.error("query_user_rights failed for uid=%s: %s", uid, e)
        return "free"
    elif Config.IS_DEBUG and uid in self.debug_uid_set:
      level = "premium"  # 测试 uid 放行全额度

    self._tier_cache[uid] = (level, now)
    return level

  async def handle_sleep_plan_http(self, request: web.Request) -> web.Response:
    """睡眠计划同步：sync_plans（上报变更+拉全量）/ query_plans（仅拉全量）。

    服务端是唯一事实源：合并/额度/唯一开启/完成判定都在 sleep_plan_service；
    客户端乐观更新后以本响应整体覆盖本地。
    """
    try:
      body = await request.json()
      req = SleepPlanSyncRequest.model_validate(body)
      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

      d = req.data
      incoming = d.plans if req.request_type == "sync_plans" else []
      effective_level = await self._get_effective_level(uid, d.jwt_token)

      def _do_sync() -> sleep_plan_service.SyncResult:
        profile = self.user_serv.get_profile(uid)
        stored = list(profile.sleep_plans) if profile else []
        result = sleep_plan_service.sync_plans(
          stored, incoming,
          effective_level=effective_level,
          tz_name=d.timezone,
        )
        # 环境信息随同步请求刷新（触发时刻按最近一次同步的时区换算，§6⑥；
        # 客户端该字段必填，非默认值，可以直接覆盖）
        meta_changed = False
        if profile is not None:
          meta_changed = self.user_serv._note_request_meta(profile, d.timezone, d.language, fill_only=False)
        if result.changed or meta_changed:
          if profile is None:
            profile = UserProfile()
            self.user_serv._note_request_meta(profile, d.timezone, d.language, fill_only=False)
          profile.sleep_plans = result.plans
          profile.sleep_plans_synced_at = int(time.time())
          self.user_serv.save_profile(uid, profile)
        return result

      result = await asyncio.to_thread(_do_sync)

      visible = result.visible_plans
      data = {
        "server_time": int(time.time()),
        "membership_tier": sleep_plan_service.client_tier(effective_level),
        "quota": {"used": len(visible), "limit": sleep_plan_service.plan_quota(effective_level)},
        "plans": [p.model_dump(mode="json") for p in visible],
        "rejected": result.rejected,
      }
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})

    except ValidationError as e:
      logging.error(f"sleep_plan validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("sleep_plan error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- 运营后台接口（/ops/*） --------------------
  # 调用方：ops_admin_server.py。鉴权两步：本地验 JWT → auth_server 查 ops_role，
  # 仅 admin/super 放行（0号管理员 super 由数据库直设，授权走 auth_server grant_ops_role）。

  async def _check_ops_admin(self, jwt_token: str) -> Optional[str]:
    """校验调用者是运营管理员（ops_role=admin/super），返回 uid；否则 None。"""
    if not jwt_token:
      return None
    payload = self._check_token(jwt_token)
    if payload is None:
      return None
    uid = payload.get("uid")
    if not uid:
      return None

    try:
      req = AuthRequest(
        request_type="query_ops_role",
        timestamp=int(time.time()),
        version="1.0",
        data=AuthData(jwt_token=jwt_token),
      )
      async with ClientSession() as session:
        async with session.post(
          f"{Config.AUTH_SERVER_URL}/auth",
          json=req.model_dump(mode="json"),
          timeout=3,
        ) as resp:
          body = await resp.json()
    except Exception as e:
      logging.error("ops role check failed (auth_server unreachable?): %s", e)
      return None

    role = ((body or {}).get("data") or {}).get("ops_role")
    if role in ("admin", "super"):
      return uid
    logging.warning("ops api rejected: uid=%s ops_role=%s", uid, role)
    return None

  async def handle_ops_push_http(self, request: web.Request) -> web.Response:
    """接收运营后台发布的弹窗消息：校验管理员 → 校验消息 → 追加进运营配置（热加载生效，等 App 拉取）。"""
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      popup = body.get("popup")
      ok, msg = append_popup(popup)
      if not ok:
        return web.json_response(BaseResponse(code=400, msg=msg).model_dump(), status=400)

      # 发布审计日志（append-only，ops 后台"发布记录"页可查）
      payload = self._check_token(body.get("jwt_token") or "") or {}
      self.user_serv.record_publish(popup, uid, payload.get("email") or "")

      logging.info("ops push published by uid=%s popup_id=%s", uid, popup.get("popup_id"))
      resp = BaseResponse(code=0, msg=msg)
      return web.json_response({**resp.model_dump(), "data": {"popup_id": popup.get("popup_id")}})
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops push format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops push error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_survey_records_http(self, request: web.Request) -> web.Response:
    """全量问卷提交记录（_meta:survey:*），运营后台表格展示；可按 survey_id 过滤。"""
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      records = await asyncio.to_thread(self.user_serv.list_survey_records, body.get("survey_id"))
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {"records": records, "total": len(records)}})
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops survey_records format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops survey_records error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_publish_logs_http(self, request: web.Request) -> web.Response:
    """消息发布审计日志（_meta:publish:*），运营后台"发布记录"页；按发布时间倒序。"""
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      limit = body.get("limit") or 200
      records = await asyncio.to_thread(self.user_serv.list_publish_records, limit)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {"records": records, "total": len(records)}})
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops publish_logs format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops publish_logs error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_popup_meta_http(self, request: web.Request) -> web.Response:
    """运营发布表单的辅助数据：现有 survey_ids（问卷选择器）+ popup_ids（ID 查重/建议）。"""
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      from ops_config import _load_ops_config
      popups, surveys, _nqa = await asyncio.to_thread(_load_ops_config)
      data = {
        "survey_ids": sorted(surveys.keys()),
        "popup_ids": sorted(p.get("popup_id") for p in popups if p.get("popup_id")),
        # 完整问卷内容：发布页「预览」按钮按 survey_id 本地渲染题目流程
        "surveys": surveys,
      }
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": data})
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops popup_meta format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops popup_meta error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_survey_list_http(self, request: web.Request) -> web.Response:
    """全部问卷定义（含 created_at / i18n 内容），运营后台问卷列表页；按创建时间倒序。"""
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      from ops_config import _load_ops_config
      _popups, surveys, _nqa = await asyncio.to_thread(_load_ops_config)
      items = [
        {"survey_id": sid, "created_at": (s or {}).get("created_at"), "i18n": (s or {}).get("i18n") or {}}
        for sid, s in surveys.items()
      ]
      # 创建时间倒序；老配置缺 created_at 的排最后
      items.sort(key=lambda s: s["created_at"] or 0, reverse=True)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {"surveys": items, "total": len(items)}})
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops survey_list format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops survey_list error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_save_survey_http(self, request: web.Request) -> web.Response:
    """运营保存新问卷（写入运营配置 surveys 字典）；仅 ops admin。

    结构与 query_survey 响应 data 同构的内容按语言收进 i18n（tanchuang_suvey.md 5.），
    校验（survey_id 查重、题目/选项/奖励字段）在 ops_config.save_survey 内完成。
    """
    try:
      body = await request.json()
      uid = await self._check_ops_admin(body.get("jwt_token") or "")
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      survey = body.get("survey")
      if not isinstance(survey, dict):
        return web.json_response(BaseResponse(code=400, msg="survey 必须是 JSON object").model_dump(), status=400)
      ok, msg = await asyncio.to_thread(save_survey, survey)
      if not ok:
        return web.json_response(BaseResponse(code=400, msg=msg).model_dump(), status=400)
      resp = BaseResponse(code=0, msg=msg)
      return web.json_response(resp.model_dump())
    except (json.JSONDecodeError, TypeError) as e:
      logging.error(f"ops save_survey format error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.exception("ops save_survey error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_ops_upload_image_http(self, request: web.Request) -> web.Response:
    """运营上传弹窗主图（multipart，字段名 image），返回公开 URL；仅 ops admin。"""
    try:
      form = await request.post()
      # multipart 里 jwt_token 作为普通表单字段携带；也兼容 header 传法
      token = form.get("jwt_token") or request.headers.get("X-Ops-Token") or ""
      uid = await self._check_ops_admin(token)
      if uid is None:
        return web.json_response(BaseResponse(code=403, msg="not an ops admin").model_dump(), status=403)

      field = form.get("image")
      if field is None or not getattr(field, "filename", None):
        return web.json_response(BaseResponse(code=400, msg="缺少图片文件（字段 image）").model_dump(), status=400)
      data = field.file.read()
      url, err = await asyncio.to_thread(save_popup_image, data)
      if err:
        return web.json_response(BaseResponse(code=400, msg=err).model_dump(), status=400)
      resp = BaseResponse(code=0, msg="ok")
      return web.json_response({**resp.model_dump(), "data": {"url": url}})
    except Exception as e:
      logging.exception("ops upload_image error: %s", e)
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  async def handle_popup_image_http(self, request: web.Request) -> web.Response:
    """弹窗主图公开访问：/popup_images/<hash>.<ext>，内容寻址 → 长缓存。"""
    name = request.match_info.get("name") or ""
    if not _POPUP_IMAGE_NAME_RE.match(name):
      return web.Response(status=404, text="not found")
    path = os.path.join(_popup_image_dir(), name)
    if not os.path.isfile(path):
      return web.Response(status=404, text="not found")
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
    return web.Response(
      body=await asyncio.to_thread(lambda: open(path, "rb").read()),
      content_type=mime,
      headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

  # -------------------- 启动 --------------------
  def _startup_self_check(self) -> None:
    """启动自检：关键运行资源缺失时打 ERROR（不阻断启动），让部署遗漏在启动日志即可见。

    覆盖：
      - 运营配置 popup_survey_config.json（缺失 → /popup 恒空、/survey 恒 404，
        故障特征见 ops_config._load_ops_config 的缓存兜底）
      - 睡眠推荐资源（knowledge_base / topology / reco_candidates，缺失 → 推荐退化为兜底）
      - LLM 可用性（ARK_API_KEY 未配 → 分析文案用默认模板）
    """
    from ops_config import ops_config_status

    st = ops_config_status()
    if not st["exists"]:
      logging.error(
        "[self-check] ops config MISSING: %s — /popup 将恒返回空、/survey 将恒 404。"
        "部署 data/popup_survey_config.json 即可恢复（mtime 热加载，无需重启）",
        st["path"],
      )
    else:
      logging.info(
        "[self-check] ops config OK: %s (popups=%d surveys=%d next_query_after=%s)",
        st["path"], st["popups"], st["surveys"], st["next_query_after"],
      )
      if st["dangling_survey_refs"]:
        logging.error(
          "[self-check] ops config 弹窗引用了不存在的问卷（用户点击后 query_survey 必 404）: %s",
          st["dangling_survey_refs"],
        )

    from llm import reco as llm_reco
    for res in (llm_reco._KNOWLEDGE_BASE_PATH, llm_reco._TOPOLOGY_PATH, llm_reco._SOP_CANDIDATES_PATH):
      if not os.path.exists(res):
        logging.error("[self-check] reco resource MISSING: %s — 睡眠推荐将退化为兜底策略", res)

    if not self.llm.enabled:
      logging.warning("[self-check] LLM disabled (ARK_API_KEY / KIMI_API_KEY 均未配置) — 分析/洞察文案使用默认模板")

  async def _analysis_sweeper(self):
    """每小时兜底扫描：给"有未分析新夜晚但没跑成"的 uid 补触发。

    覆盖的漏网场景：服务器重启丢了内存中的防抖任务、后台 LLM 临时失败、
    更新请求后一直没有新请求进来（自动触发只在 update 路径上做判定）。
    LLM 未启用时直接空转（calc_* 会秒回，扫了也没意义）。
    """
    while True:
      await asyncio.sleep(Config.LLM_ANALYSIS_SWEEP_SECONDS)
      try:
        if not Config.LLM_ANALYSIS_AUTO_TRIGGER or not self.llm or not self.llm.enabled:
          continue
        uids = await asyncio.to_thread(self.user_serv.list_uids)
        for uid in uids:
          profile = await asyncio.to_thread(self.user_serv.get_profile, uid)
          needed, _ = self._analysis_needed(profile)
          # 活跃门：app 不活跃（窗口内无 /analysis、无 plays）的用户不补烧 LLM
          if needed and self._is_active(uid, profile):
            logging.info("analysis sweeper: catch up uid=%s", uid)
            await self._maybe_schedule_llm_update(uid, profile, None, None)
      except asyncio.CancelledError:
        raise
      except Exception as e:
        logging.error("analysis sweeper error: %s", e)

  async def _activity_logger(self):
    """每 5 分钟打印内存活跃表总数；顺手清掉窗口外的过期条目防膨胀。"""
    while True:
      await asyncio.sleep(300)
      try:
        now = time.time()
        window = Config.LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS
        stale = [uid for uid, ts in self._activity.items() if now - ts >= window]
        for uid in stale:
          del self._activity[uid]
        logging.info(
          "activity table: total=%d (window=%ds, pruned=%d)",
          len(self._activity), window, len(stale),
        )
      except asyncio.CancelledError:
        raise
      except Exception as e:
        logging.error("activity logger error: %s", e)

  async def start_http(self):
    """启动HTTP服务器"""
    runner = web.AppRunner(self.app)
    await runner.setup()
    site = web.TCPSite(runner, self.host, self.port)
    await site.start()
    logging.info(f"UserServer (LevelDB) started on http://{self.host}:{self.port}")
    self._startup_self_check()
    self._sweeper_task = asyncio.create_task(self._analysis_sweeper())
    self._activity_log_task = asyncio.create_task(self._activity_logger())
    # 保持服务运行
    await asyncio.Event().wait()


if __name__ == "__main__":
  server = UserServer()
  try:
    asyncio.run(server.start_http())
  except KeyboardInterrupt:
    logging.warning("Shutting down UserServer.")
    server.close()  # 关闭LevelDB连接
