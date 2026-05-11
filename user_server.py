import asyncio,datetime,json,logging,os,time
from typing import Any, Optional, List
from dotenv import load_dotenv
import jwt
from pydantic import ValidationError
import websockets
from aiohttp import ClientResponseError, ClientSession, web
from sleep_reco import RecommendationEngine
import plyvel
from user_profile import UserProfile, SleepScenario
from config import Config
from common import util
from user_profile import (
  UserProfile, ProfileRequest, ProfileResponse, ProfileData,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp, BaseResponse,
  AnalysisRequest, AnalysisResponse,
  SleepAdviceRequest, SleepAdviceResponse, SleepAdviceResult,
)
from auth import AuthRequest
from uid.uuid import get_or_create_uuid
from llm_service import SleepAnalysisLLM, extract_sleep_context, deep_merge
import logger
import copy

load_dotenv()
run_dir = os.getenv("RUN_DIR")
logger.init_log(f"{run_dir}/user_server_logs")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 1024
  def __init__(self):
    # 初始化LevelDB（若路径不存在则自动创建）
    self.db = plyvel.DB(f"{run_dir}/{Config.DB_PATH}", create_if_missing=True)

  def get_profile(self, uid: str) -> Optional[UserProfile]:
    """从LevelDB读取单个用户的画像"""
    if not uid or not isinstance(uid, str):
      logging.error(f"erro uid : {uid}")
      return None

    data = self.db.get(uid.encode('utf-8'))  # LevelDB键值为bytes类型
    logging.info(f"get from leveldb data {data}")
    if data:
      return UserProfile.model_validate(json.loads(data.decode('utf-8')))
    return None

  def save_profile(self, uid: str, profile: UserProfile):
    """将单个用户的画像写入LevelDB"""
    data = json.dumps(profile.model_dump()).encode('utf-8')
    self.db.put(uid.encode('utf-8'), data)

  def _merge_profile(self, old_profile, new_profile):
    return old_profile

  def _merge_behavior(self, old_behaviors, new_behaviors):
    # merge sort, consider the old ones is sorted already
    logging.info(f"merge {old_behaviors} and {new_behaviors}")
    for behavior_type, values in new_behaviors.items():
      values.sort(key=lambda x:x[0])
      if behavior_type in old_behaviors and isinstance(values, list):
        old_behaviors[behavior_type].sort(key=lambda x:x[0])
        old_behaviors[behavior_type]= util.merge_two_sorted_dedup(old_behaviors[behavior_type], values) 
      else:
        old_behaviors[behavior_type] = values

      if len(old_behaviors[behavior_type]) > UserProfileServ.MAX_BEHAVIOR_LEN:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][len(old_behaviors[behavior_type]) - UserProfileServ.MAX_BEHAVIOR_LEN:]

    logging.info(f"after update {old_behaviors}")
    return old_behaviors
  
  def calc_sleep_reco(self, uid: str, new_profile: UserProfile, old_profile: UserProfile) -> List[SleepScenario]:
    # 1. 触发推荐引擎逻辑
    sleep_scenarios = old_profile.sleep_scenarios
    if RecommendationEngine.should_rerun_recommendation(old_profile, new_profile):
      logging.info(f"Rerunning sleep scenario recommendation for {uid}")
      sleep_scenarios = RecommendationEngine.generate(new_profile)

    return sleep_scenarios

  def update_profile(self, uid: str, new_profile: UserProfile) -> bool:
    """写入用户行为（仅更新单个用户数据）"""
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    # 读取或创建用户画像（仅操作单个用户，避免全量加载）
    profile = self.get_profile(uid)
    old_profile = profile
    if profile is None:
      self.save_profile(uid, new_profile)
      return True

    # just replace, if need
    if len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
      profile.uid_emb = new_profile.uid_emb

    profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)
     
    profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)
    # 仅保存当前用户的更新（而非全量数据）
    self.save_profile(uid, profile)
    logging.info(f"Behavior data for uid '{uid}' updated")
    return True
  
  def close(self):
    self.db.close()

def get_http_status(resp: BaseResponse):
  status = 200
  if resp.code != 0:
    status = resp.code
  return status


async def query_profile(jwt_token: str, server_uri: str) :
  query_endpoint = f"{server_uri}/user_profile"
  async with ClientSession() as session:
    try:
      req = ProfileRequest(request_type="query_profile", timestamp=int(time.time()), version="1.0", data=ProfileData(jwt_token = jwt_token))
      # 构造请求数据
      async with session.post(
        query_endpoint,
        json=req.model_dump(),
        timeout=2  # 10秒超时
      ) as response:
        response.raise_for_status()  # 触发HTTP错误（如4xx、5xx）
        data = await response.json()
        return ProfileResponse.model_validate(data)
            
    except ClientResponseError as e:
      # 处理HTTP错误响应
      error_msg = f"查询失败 [HTTP {e.status}]: {e}"
      raise Exception(error_msg) from e
    except Exception as e:
      raise Exception(f"查询用户画像失败: {str(e)}") from e

class UserServer:
  def __init__(self):
    server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
    self.host = Config.HOST
    self.port = Config.PORT
    self.user_serv = UserProfileServ()
    self.update_task = None
    self.app = web.Application()
    self.active_uid = ""
    self.system_uid = get_or_create_uuid()

    self.llm = SleepAnalysisLLM()
    self.setup_routes()

  def close(self):
    self.user_serv.close()
    if self.update_task:
      self.update_task.cancel()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/user_profile', self.handle_profile_request_http)
    self.app.router.add_post('/login', self.handle_login_http)
    self.app.router.add_post('/analysis', self.handle_analysis_http)
    self.app.router.add_post('/sleep_advice', self.handle_sleep_advice_http)

  def _check_token(self, jwt_token: str)-> dict | None:
    logging.info(f"in login: {jwt_token}")
    try:
      payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=[Config.ALGORITHM])
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
        return InvalidOrExpiredTokenResp()
      uid = payload.get("uid")
    elif Config.IS_DEBUG and data.uid is not None and len(data.uid) > 3:
      uid = data.uid

    return uid

  def handle_query_profile(self, request: ProfileRequest) -> BaseResponse:
    logging.info(f"handle: {request}")
    """查询用户画像（从LevelDB按需读取）"""
    if request.data is None:
      logging.error("query request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"get uid: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    if uid == "active_uid":
      uid = self.active_uid

    profile = self.user_serv.get_profile(uid)
    logging.info(f"profile found: {profile}")
    if profile:
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data={"user_profile": profile.model_dump()})
    else:
      logging.warning(f"{uid}, {request} not found")
      return ProfileResponse(code=0, msg=f"User with uid '{request.data}' not found", request_type=request.request_type, data=None)

    # incr update the behaviors by time, and update long term weight
  def handle_update_profile(self, request: ProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    if request.data is None:
      logging.error("update request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)
    logging.info(f"uid for update: {uid}")

    if uid is None:
      return InvalidOrExpiredTokenResp()

    succ = self.user_serv.update_profile(uid, request.data.user_profile)
    if succ:
      return ProfileResponse(code=0, msg=f"update profile for '{request.timestamp}' succ", request_type=request.request_type, data=None)
    else:
      return ProfileResponse(code=500, msg=f"update profile failed", request_type=request.request_type, data=None)

  def handle_login(self, request: AuthRequest) -> BaseResponse:
    if request.data is None or request.data.jwt_token is None:
      return InvalidReqFormatResp()

    payload = self._check_token(request.data.jwt_token)
    if payload is None:
      return InvalidOrExpiredTokenResp()

    uid = payload.get("uid")
    self.active_uid = uid
    self.jwt_token = request.data.jwt_token
    return BaseResponse(code=0, msg="user ativated successufully")

  async def handle_profile_request(self, websocket, path=None):
    try:
      async for msg in websocket:
        response_obj: BaseResponse
        try:
          data = json.loads(msg)
          req = ProfileRequest.model_validate(data)
          if req.request_type == "query_profile":
            response_obj = self.handle_query_profile(req)
          elif req.request_type == "update_profile":
            response_obj = self.handle_update_profile(req)
          else:
            response_obj = BaseResponse(code=400, msg="Invalid request type")

        except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as e:
          response_obj = BaseResponse(code=400, msg=f"Invalid request format: {e}")
        
        await websocket.send(json.dumps(response_obj.model_dump()))
    except websockets.exceptions.ConnectionClosed:
      logging.error("Connection closed.")


  def get_overall_score(self, profile: UserProfile) -> Optional[float]:
    """计算用户最近7天的平均睡眠质量得分（0-100）"""
    if not profile.sleep_data:
      return None
    recent = profile.sleep_data[-7:]
    scores = [s.sleep_quality for s in recent if s.sleep_quality is not None]
    return round(sum(scores) / len(scores), 2) if scores else None

  async def handle_profile_request_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      logging.info(f"req {data}")
      req = ProfileRequest.model_validate(data)
      logging.info(f"request {req}")

      if req.request_type == "query_profile":
        response_obj = self.handle_query_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "update_profile":
        response_obj = self.handle_update_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type in ["analysis_overview", "insight", "daily_report", "weekly_report", "month_report"]:
        uid = self._parse_for_uid(req.data)
        if not uid:
          return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)

        profile = self.user_serv.get_profile(uid)
        if not profile:
          return web.json_response(ProfileResponse(code=404, msg="Profile not found").model_dump(), status=404)

        # Handle different request types
        response_data = {}
        if req.request_type == "analysis_overview":
          response_data = {
            "overall_score": self.get_overall_score(profile),
            "weekly_best": profile.sleep_analysis.get("weekly_best"),
            "sleep_insight": profile.sleep_analysis.get("sleep_insight")
          }
        elif req.request_type == "insight":
          response_data = {"insight": profile.long_term_profile}
        elif req.request_type == "daily_report":
          response_data = {"daily": profile.sleep_data[-1] if profile.sleep_data else None}
        elif req.request_type == "weekly_report":
          response_data = {"weekly": profile.sleep_data[-7:]}
        elif req.request_type == "month_report":
          response_data = {"monthly": profile.sleep_data[-30:]}

        # Filter response based on modules
        if req.modules:
          response_data = {key: value for key, value in response_data.items() if key in req.modules}

        return web.json_response(ProfileResponse(code=0, msg="success", request_type=req.request_type, data=response_data).model_dump())

      else:
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)

    except ValidationError as e:
        logging.error(f"Validation error: {e}")
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)
      
  # -------------------- /analysis endpoint --------------------

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

      if self.llm.enabled:
        ctx = extract_sleep_context(profile, req.data)
        llm_text = await self.llm.generate(req.request_type, ctx, req.data.language, req.data.modules)
        if llm_text:
          deep_merge(response_data, llm_text)

      resp = AnalysisResponse(code=0, msg="success", request_type=req.request_type, data=response_data)
      return web.json_response(resp.model_dump())

    except ValidationError as e:
      logging.error(f"analysis validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.error(f"analysis error: {e}")
      return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)

  # -------------------- /sleep_advice endpoint --------------------

  _DEFAULT_ADVICE_ANALYSIS = (
    "Your sleep data shows a balanced pattern overall. "
    "Deep sleep and REM stages are within a healthy range, "
    "supporting physical recovery and cognitive function."
  )
  _DEFAULT_ADVICE_BULLETS = [
    "Try to maintain a consistent bedtime to reinforce your circadian rhythm.",
    "Limit screen exposure at least 30 minutes before bed.",
    "Consider a light breathing exercise or Mindora scene before sleeping.",
  ]
  _DEFAULT_ADVICE_HIGHLIGHTS = {
    "onset": "Sleep onset appears normal.",
    "deep": "Deep sleep ratio is within the healthy range.",
    "rem": "REM activity supports memory consolidation.",
    "rhythm": "Sleep continuity is stable.",
  }

  async def handle_sleep_advice_http(self, request: web.Request) -> web.Response:
    """POST /sleep_advice — LLM-powered sleep analysis + actionable advice."""
    try:
      body = await request.json()
      req = SleepAdviceRequest.model_validate(body)

      uid = self._parse_for_uid(req.data)
      if uid is None:
        return web.json_response(InvalidOrExpiredTokenResp().model_dump(), status=401)
      if isinstance(uid, BaseResponse):
        return web.json_response(uid.model_dump(), status=uid.code)

      profile = self.user_serv.get_profile(uid)
      date = req.data.date or datetime.date.today().isoformat()
      language = req.data.language or "en"

      # --- try LLM generation -------------------------------------------------
      llm_result = None
      if self.llm.enabled and profile:
        ctx = extract_sleep_context(profile, req.data)
        ctx["focus"] = req.data.focus
        llm_result = await self.llm.generate(
          "sleep_analysis_advice", ctx, language, [],
        )

      # --- assemble response ---------------------------------------------------
      if llm_result:
        result = SleepAdviceResult(
          analysis=llm_result.get("analysis", self._DEFAULT_ADVICE_ANALYSIS),
          advice=llm_result.get("advice", self._DEFAULT_ADVICE_BULLETS),
          highlights=llm_result.get("highlights", self._DEFAULT_ADVICE_HIGHLIGHTS),
          date=date,
          language=language,
          llm_used=True,
        )
      else:
        # Fallback: static defaults when LLM is disabled or fails
        result = SleepAdviceResult(
          analysis=self._DEFAULT_ADVICE_ANALYSIS,
          advice=list(self._DEFAULT_ADVICE_BULLETS),
          highlights=dict(self._DEFAULT_ADVICE_HIGHLIGHTS),
          date=date,
          language=language,
          llm_used=False,
        )

      resp = SleepAdviceResponse(
        code=0, msg="success",
        request_type="sleep_analysis_advice",
        data=result,
      )
      return web.json_response(resp.model_dump())

    except ValidationError as e:
      logging.error(f"sleep_advice validation error: {e}")
      return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
      logging.error(f"sleep_advice error: {e}")
      return web.json_response(
        BaseResponse(code=500, msg="Internal server error").model_dump(), status=500
      )

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

  def _filter_modules(self, data: dict, modules: list) -> dict:
    return {k: v for k, v in data.items() if k in modules} if modules else data

  def _build_overview(self, d, profile: Optional[UserProfile]) -> dict:
    date = d.date or datetime.date.today().isoformat()
    score = self.get_overall_score(profile) if profile else None
    if score is None:
      score = 82

    weekly_best = None
    if profile and profile.mindora_record:
      best = max(profile.mindora_record.items(), key=lambda x: len(x[1]), default=None)
      if best and best[1]:
        weekly_best = {
          "audio_name": best[0].replace("sleep.scene.", "").replace("_", " ").title(),
          "used_times": len(best[1]),
          "score": int(score),
          "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
          "end_date": date,
        }
    if weekly_best is None:
      weekly_best = {
        "audio_name": "Sedona Red Rocks",
        "used_times": 5,
        "score": 92,
        "start_date": (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat(),
        "end_date": date,
      }

    result = {
      "overall_score": {"score": int(score), "date": date},
      "weekly_best": weekly_best,
      "sleep_insight": {
        "title": "Excellent Deep Sleep Performance",
        "description": "Your deep sleep accounts for a healthy proportion of total sleep. Keep maintaining a regular sleep schedule.",
        "date": date,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_day(self, d, profile: Optional[UserProfile]) -> dict:
    date = d.date or datetime.date.today().isoformat()
    latest = profile.sleep_data[-1] if profile and profile.sleep_data else None
    score = int(latest.sleep_quality) if latest and latest.sleep_quality else 70

    result = {
      "score_summary": {"score": score, "date": date},
      "sleep_scenarios": {
        "title": "Sedona Desert Calm",
        "description": "You fell asleep quickly and maintained a stable sleep rhythm after the scenario started.",
        "date": date,
      },
      "stage_insights": {
        "awake": {"description": "A brief awakening was detected and you returned to sleep quickly.", "date": date},
        "rem":   {"description": "REM sleep was sustained and supports emotional processing.", "date": date},
        "core":  {"description": "Core sleep remained stable across most of the night.", "date": date},
        "deep":  {"description": "Deep sleep contributed strongly to physical recovery.", "date": date},
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_week(self, d, profile: Optional[UserProfile]) -> dict:
    today = datetime.date.today()
    start = d.start_date or (today - datetime.timedelta(days=6)).isoformat()
    end   = d.end_date   or today.isoformat()

    score = self.get_overall_score(profile) if profile else None
    score = int(score) if score else 86
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

    result = {
      "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
      "sleep_trends": {
        "body": "Excellent Deep Sleep Performance",
        "description": "Your deep sleep accounted for a healthy proportion of total sleep this week.",
        "start_date": start,
        "end_date": end,
      },
      "onset_efficiency": {
        "scenario_name": "Sedona Desert Calm",
        "used_times": 5,
        "score": score,
        "start_date": start,
        "end_date": end,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_sleep_month(self, d, profile: Optional[UserProfile]) -> dict:
    today = datetime.date.today()
    start = d.start_date or (today - datetime.timedelta(days=29)).isoformat()
    end   = d.end_date   or today.isoformat()

    score = self.get_overall_score(profile) if profile else None
    score = int(score) if score else 89
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair"

    # Build score_series from real data, fall back to mock trend
    score_series: list = []
    if profile and profile.sleep_data:
      for sr in profile.sleep_data[-30:]:
        if sr.sleep_quality is not None:
          score_series.append({
            "date": datetime.date.fromtimestamp(sr.timestamp).isoformat(),
            "score": int(sr.sleep_quality),
          })
    if not score_series:
      cur = datetime.date.fromisoformat(start)
      end_d = datetime.date.fromisoformat(end)
      base = 62
      while cur <= end_d:
        score_series.append({"date": cur.isoformat(), "score": min(100, base)})
        base += 1
        cur += datetime.timedelta(days=1)

    result = {
      "score_summary": {"score": score, "label": label, "start_date": start, "end_date": end},
      "sleep_trends": {
        "body": "This month, you maintained a consistent amount of sleep.",
        "description": "Deep sleep remained above the standard level and your bedtime trended earlier.",
        "score_series": score_series,
        "start_date": start,
        "end_date": end,
      },
      "onset_efficiency": {
        "scenario_list": ["Sedona Desert Calm", "Maldives Drift Sleep", "Canadian Forest Solace"],
        "description": "Sedona Desert Calm was your most frequently used sleep scenario this month and showed the best onset performance.",
        "start_date": start,
        "end_date": end,
      },
    }
    return self._filter_modules(result, d.modules)

  def _build_explore(self, d, profile: Optional[UserProfile]) -> dict:
    date  = d.date or datetime.date.today().isoformat()
    start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=6)).isoformat()

    has_data = profile is not None and bool(profile.sleep_data)
    latest   = profile.sleep_data[-1] if has_data else None
    summaries = latest.sequence_summaries if (latest and latest.sleep_status) else {}

    overall_score    = int(latest.sleep_quality) if latest and latest.sleep_quality else 82
    onset_score      = int(latest.soe)           if latest and latest.soe           else 82
    structure_score  = 49
    fluctuation_score = 34

    tb = summaries.get("time_in_bed") or 1
    rem_pct  = f"{round(summaries.get('rem_sleep_duration',  0) / tb * 100, 1)}%" if summaries else "22%"
    deep_pct = f"{round(summaries.get('deep_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "29.8%"
    core_pct = f"{round(summaries.get('core_sleep_duration', 0) / tb * 100, 1)}%" if summaries else "48.2%"

    hr_mid = int(latest.avg_heart_rate) if latest and latest.avg_heart_rate else 70
    hr_range = f"{hr_mid - 15}-{hr_mid + 15}bpm"
    resp_fluct = f"{int(latest.respiratory_var or 25)}%" if latest else "25%"

    scene_id   = "cocos_island_moonlight"
    scene_name = "Cocos Island Moonlight"
    if profile and profile.mindora_record:
      best = max(profile.mindora_record.items(), key=lambda x: len(x[1]), default=None)
      if best and best[1]:
        scene_id   = best[0].replace("sleep.scene.", "")
        scene_name = scene_id.replace("_", " ").title()

    awake_count = summaries.get("night_awake_count", 2)
    result = {
      "data_ready": has_data,
      "header_summary": {
        "intro_text": "Last night your body entered a stable, relaxed, and highly restorative sleep state.",
        "intro_detail_text": "What happened last night, what helped you most, and how Mindora adjusted for you.",
        "date": date,
      },
      "score_summary": {
        "score": overall_score,
        "title": "Sleep Score",
        "efficiency_score":   onset_score,
        "structure_score":    structure_score,
        "fluctuation_score":  fluctuation_score,
        "date": date,
      },
      "onset_efficiency": {
        "score": onset_score,
        "label": "Healthy Range",
        "onset_minutes": 12,
        "first_sleep_time":           latest.first_sleep_time if latest else "23:45",
        "pre_sleep_heart_rate":       f"{int(latest.hr_before_sleep)}bpm"  if latest and latest.hr_before_sleep  else "68bpm",
        "pre_sleep_respiratory_rate": f"{int(latest.rr_before_sleep)}brpm" if latest and latest.rr_before_sleep else "15brpm",
        "description": "You fell asleep faster than your recent average and your pre-sleep physiology stayed calm.",
        "date": date,
      },
      "sleep_structure": {
        "score": structure_score,
        "label": "Average",
        "continuous_sleep_minutes": int(tb),
        "rem_percent":  rem_pct,
        "deep_percent": deep_pct,
        "core_percent": core_pct,
        "description": "Your sleep structure remained relatively balanced, with deep sleep contributing strongly to recovery.",
        "date": date,
      },
      "night_fluctuation": {
        "score": fluctuation_score,
        "label": "High Fluctuation" if awake_count > 3 else "Normal",
        "intervention": "Rain Wash",
        "awake_count":            awake_count,
        "awake_duration_minutes": int(summaries.get("night_awake_duration", 5)),
        "awake_type":             summaries.get("night_awake_type") or "Brief awakening",
        "heart_rate_range":       hr_range,
        "respiratory_fluctuation": resp_fluct,
        "description": "You had a small number of brief interruptions and the system applied a suitable intervention.",
        "date": date,
      },
      "scene_preference": {
        "scene_id":   scene_id,
        "scene_name": scene_name,
        "scene_type": "Ocean wind with slow percussion",
        "description": "This scene has recently matched your sleep onset rhythm most consistently.",
        "start_date": start,
        "end_date":   date,
      },
      "sleep_advice": {
        "description": "Keep your current bedtime and continue using the same wind-down scene for the next few nights.",
        "date": date,
      },
    }
    return self._filter_modules(result, d.modules)

  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      request = AuthRequest.model_validate(data)
      response_obj = self.handle_login(request)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
      logging.error(f"login error: {e}, request={request}")
      response_obj = InvalidReqFormatResp()

    logging.info(f"login resp: {response_obj}")
    if response_obj.code == 0 and len(Config.RemoteHost) > 10 and (self.update_task is None or self.update_task.done()):
      self.update_task = asyncio.create_task(self.fetch_profile_from_remote(f"{Config.RemoteHost}")) 
    else:
      logging.info("update task has started already")

    return web.json_response(status=get_http_status(response_obj), data=response_obj.model_dump())
  
  async def fetch_profile_from_remote(self, url):
    # check jwt_token util expire and the time(maybe 10h is enough)
    start_min = int(time.time()) / 60
    logging.info(f"begin to loop update for activeuid : {self.active_uid}")
    while True:
      cur_min = int(time.time()) / 60
      if cur_min - start_min > 60:
        logging.info("break because of time")
        break

      await asyncio.sleep(60)

      resp = await query_profile(self.jwt_token, Config.RemoteHost)
      if resp is None:
        logging.warning(f"none resp from remote server: {Config.RemoteHost}")

      succ = self.user_serv.update_profile(resp.profile)
      if not succ:
        logging.warning(f"erro in update profile for {resp.profile}")
      else:
        logging.info(f"succ update profile for {self.active_uid}")

  async def start_http(self):
    """启动HTTP服务器"""
    runner = web.AppRunner(self.app)
    await runner.setup()
    site = web.TCPSite(runner, self.host, self.port)
    await site.start()
    logging.info(f"UserServer (LevelDB) started on http://{self.host}:{self.port}")
    # 保持服务运行
    await asyncio.Event().wait()

  async def start(self):
    async with websockets.serve(self.handle_profile_request, self.host, self.port):
      logging.info(f"UserServer started on ws://{self.host}:{self.port}")
      await asyncio.Future()  # 持续运行


if __name__ == "__main__":
  server = UserServer()
  try:
    # asyncio.run(server.start())
    asyncio.run(server.start_http())
  except KeyboardInterrupt:
    logging.warning("Shutting down UserServer.")
    server.close()  # 关闭LevelDB连接
