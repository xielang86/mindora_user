"""user_server.py — Mindora user server 对外 HTTP API 层。

只保留 API 关注点：路由、鉴权（RS256 公钥验签）、请求 handler、远端同步、进程启动。
业务实现已拆分为：
  - profile_service.py     画像存储 / 行为聚合 / 更新编排（UserProfileServ 门面）
  - analysis_content.py    LLM 分析内容生成与检索
  - analysis_builders.py   /analysis 响应骨架（纯函数）
  - engagement_service.py  弹窗 / 问卷 / 陪伴足迹
  - ops_config.py          弹窗问卷运营配置加载
"""
import asyncio,copy,datetime,json,logging,os,time
from typing import Any, Optional
from dotenv import load_dotenv
import jwt
from pydantic import BaseModel, ValidationError
from aiohttp import ClientSession, web

import analysis_builders
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
from ops_config import append_popup
from uid.uuid import get_or_create_uuid
from llm import SleepAnalysisLLM, deep_merge
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
    self.setup_routes()

  def close(self):
    self.user_serv.close()
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
    # 运营后台接口（管理员校验：JWT + auth_server 查 ops_role）
    self.app.router.add_post('/ops/push', self.handle_ops_push_http)
    self.app.router.add_post('/ops/survey_records', self.handle_ops_survey_records_http)

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

    profile = self.user_serv.get_profile(uid)
    if profile:
      logging.info("profile found uid=%s summary=%s", uid, self.user_serv._profile_for_log(profile))
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data={"user_profile": profile.model_dump()})
    else:
      logging.warning("uid=%s query not found request=%s", uid, self._request_for_log(request))
      return ProfileResponse(code=0, msg=f"User with uid '{uid}' not found", request_type=request.request_type, data=None)

    # incr update the behaviors by time, and update long term weight
  async def handle_update_profile(self, request: ProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    if request.data is None:
      logging.error("update request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"uid for update: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    async with self.server_semaphore:
      succ = await asyncio.to_thread(
        self.user_serv.update_profile_basic,
        uid,
        request.data.user_profile,
      )
    if not succ:
      return ProfileResponse(code=500, msg=f"update profile failed", request_type=request.request_type, data=None)

    skip_reco = request.data.skip_sleep_scenarios_reco_update
    skip_analysis = request.data.skip_sleep_analysis_update
    if not (skip_reco and skip_analysis):
      await self._schedule_llm_update_if_needed(uid, skip_reco, skip_analysis)

    # 个人资料同步约定 §2/§3：msg 回显 uid（不用 timestamp）；返回合并后的完整 profile，
    # 客户端用它更新本地快照基准（服务端归一化后两边才对得上）
    merged = await asyncio.to_thread(self.user_serv.get_profile, uid)
    return ProfileResponse(
      code=0,
      msg=f"update profile for '{uid}' succ",
      request_type=request.request_type,
      data={"user_profile": merged.model_dump()} if merged else None,
    )

  async def _schedule_llm_update_if_needed(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> None:
    """Schedule a background LLM update for uid unless one ran recently."""
    async with self._llm_tracker_lock:
      now = time.monotonic()
      last = self._llm_update_tracker.get(uid)
      if last is not None and now - last < Config.LLM_UPDATE_COOLDOWN_SECONDS:
        logging.info("skip background llm update for uid=%s (last %.1fs ago)", uid, now - last)
        return
      self._llm_update_tracker[uid] = now

    task = asyncio.create_task(
      self._run_llm_update(uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update)
    )
    self._llm_tasks.add(task)
    task.add_done_callback(self._llm_tasks.discard)

  async def _run_llm_update(
    self,
    uid: str,
    skip_sleep_scenarios_reco_update: bool = False,
    skip_sleep_analysis_update: bool = False,
  ) -> None:
    """Run LLM work in a background task."""
    logging.info(
      "start background llm update for uid=%s (skip_reco=%s skip_analysis=%s)",
      uid, skip_sleep_scenarios_reco_update, skip_sleep_analysis_update,
    )
    try:
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

      # client_request.md：/user_profile 只承载 query_profile / update_profile，
      # 分析类请求统一走 /analysis，洞察报告由 analysis_explore 出口
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

      profile = self.user_serv.get_profile(uid)
      response_data = self._build_analysis_data(req, profile)

      # LLM 文案全部来自 update_profile 时异步生成、按日/周/月序列存储的报告，
      # 请求时纯查库，不再同步调用 LLM；未命中则只回数值骨架（文案为空/默认）
      report = UserProfileServ._find_analysis_report(
        profile, req.request_type, req.data.date, req.data.start_date, req.data.end_date,
      )
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

  async def start_http(self):
    """启动HTTP服务器"""
    runner = web.AppRunner(self.app)
    await runner.setup()
    site = web.TCPSite(runner, self.host, self.port)
    await site.start()
    logging.info(f"UserServer (LevelDB) started on http://{self.host}:{self.port}")
    self._startup_self_check()
    # 保持服务运行
    await asyncio.Event().wait()


if __name__ == "__main__":
  server = UserServer()
  try:
    asyncio.run(server.start_http())
  except KeyboardInterrupt:
    logging.warning("Shutting down UserServer.")
    server.close()  # 关闭LevelDB连接
