import asyncio,copy,datetime,json,logging,os,threading,time
from typing import Any, Optional, List
from pathlib import Path
from dotenv import load_dotenv
import jwt
from pydantic import BaseModel, ValidationError
from aiohttp import ClientResponseError, ClientSession, web
# 边缘端轻量化：推荐引擎作为可选依赖（本分支无 LLM，llm_service 已移除）
try:
  from sleep_reco import RecommendationEngine
  _HAS_RECO = True
except ImportError:
  RecommendationEngine = None
  _HAS_RECO = False

from user_profile import UserProfile, SleepScenario
from config import Config
from common import util
from common.jwt_keys import verify_token
from user_profile import (
  UserProfile, ProfileRequest, ProfileResponse, ProfileData,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp, BaseResponse,
)
from auth import AuthRequest
from uid.uuid import get_or_create_uuid

import logger
import copy

load_dotenv()
run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))
logger.init_log(f"{run_dir}/user_server_logs")
# 设备端仅用 RS256 公钥验签（见 common/jwt_keys.py），本地不保存任何签名密钥
REMOTE_SYNC_HEADER = "X-Mindora-Remote-Sync"


# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 100
  def __init__(self):
    self.lock = threading.RLock()
    # 本分支（嵌入式轻量部署）仅用明文 JSON 文件存储，无 LevelDB 依赖
    self.json_path = Path(run_dir) / Config.USER_PROFILE_JSON_PATH
    self.text_profiles: dict[str, Any] = self._load_profiles_from_text_unlocked()
    logging.info(f"preloaded {len(self.text_profiles)} user profiles from {self.json_path}")

  def _profile_to_json_data(self, profile: UserProfile) -> dict:
    return profile.model_dump(mode="json")

  def _load_profiles_from_text_unlocked(self) -> dict[str, Any]:
    if not self.json_path.exists():
      return {}

    raw_text = self.json_path.read_text(encoding="utf-8").strip()
    if not raw_text:
      return {}

    profiles = json.loads(raw_text)
    if not isinstance(profiles, dict):
      raise ValueError(f"profile json file should be a dict keyed by uid: {self.json_path}")
    return profiles

  def _save_profiles_to_text_unlocked(self, profiles: dict[str, Any]):
    self.json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = self.json_path.with_suffix(self.json_path.suffix + ".tmp")
    tmp_path.write_text(
      json.dumps(profiles, ensure_ascii=False, indent=2),
      encoding="utf-8",
    )
    tmp_path.replace(self.json_path)

  def _flush_text_profiles_unlocked(self):
    self._save_profiles_to_text_unlocked(self.text_profiles)

  def get_profile(self, uid: str) -> Optional[UserProfile]:
    """读取单个用户画像"""
    if not uid or not isinstance(uid, str):
      logging.error(f"erro uid : {uid}")
      return None

    with self.lock:
      data = self.text_profiles.get(uid)
      logging.info("get from json txt uid=%s found=%s size=%d", uid, data is not None, len(json.dumps(data)) if data else 0)
      if data is not None:
        return UserProfile.model_validate(data)
      return None

  def save_profile(self, uid: str, profile: UserProfile):
    """将单个用户的画像写入持久化存储"""
    with self.lock:
      self.text_profiles[uid] = self._profile_to_json_data(profile)
      self._flush_text_profiles_unlocked()

  def _merge_profile(self, old_profile, new_profile):
    return old_profile

  @staticmethod
  def _behavior_counts(behaviors: dict) -> dict:
    """Return a compact count summary for logging."""
    return {k: len(v) if isinstance(v, list) else v for k, v in behaviors.items()}

  def _merge_behavior(self, old_behaviors, new_behaviors):
    # merge sort, consider the old ones is sorted already
    logging.info(
      "merge behavior counts before=%s new=%s",
      self._behavior_counts(old_behaviors),
      self._behavior_counts(new_behaviors),
    )
    for behavior_type, values in new_behaviors.items():
      if not isinstance(values, list) or not values:
        # Skip empty updates to avoid wiping existing data (e.g. from remote sync
        # or compact query_profile responses). If a caller truly wants to clear a
        # behavior list, it should explicitly send a deletion marker instead.
        continue
      values.sort(key=lambda x:x[0])
      if behavior_type in old_behaviors:
        old_behaviors[behavior_type].sort(key=lambda x:x[0])
        old_behaviors[behavior_type]= util.merge_two_sorted_dedup(old_behaviors[behavior_type], values)
      else:
        old_behaviors[behavior_type] = values

      if len(old_behaviors[behavior_type]) > UserProfileServ.MAX_BEHAVIOR_LEN:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][-UserProfileServ.MAX_BEHAVIOR_LEN:]

    logging.info("after update behavior counts=%s", self._behavior_counts(old_behaviors))
    return old_behaviors

  @staticmethod
  def _extract_sop_start_events(plays: list) -> list[tuple[str, int, dict]]:
    """Extract SOP start events from a plays list.

    Returns tuples of (cmd, timestamp, event_dict).
    """
    events: list[tuple[str, int, dict]] = []
    if not isinstance(plays, list):
      return events
    for item in plays:
      if not isinstance(item, (list, tuple)) or len(item) < 2:
        continue
      ts, event = item
      if not isinstance(event, dict):
        continue
      cmd = event.get("cmd")
      event_type = event.get("event")
      if isinstance(cmd, str) and cmd.startswith("sleep.scene.") and event_type == "sop_start":
        events.append((cmd, int(ts), event))
    return events

  def _update_mindora_record(self, profile: UserProfile, new_profile: UserProfile):
    """Move SOP play counts from behaviors.plays into mindora_record.

    Stores lightweight (timestamp, duration) tuples instead of full event dicts
    to keep storage and logs small.
    """
    plays = new_profile.behaviors.get("plays", [])
    for cmd, ts, event in self._extract_sop_start_events(plays):
      duration = event.get("duration") if isinstance(event, dict) else None
      record = profile.mindora_record.setdefault(cmd, [])
      record.append((ts, duration))
      # keep the list sorted by timestamp and cap the length
      record.sort(key=lambda x: x[0])
      if len(record) > UserProfileServ.MAX_BEHAVIOR_LEN:
        record[:] = record[-UserProfileServ.MAX_BEHAVIOR_LEN:]

  @staticmethod
  def _calc_scene_stats(mindora_record: dict, days: int | None = None) -> dict:
    """Compute usage counts and total duration per scene from mindora_record.

    If ``days`` is given, only entries whose timestamp is within the last
    ``days`` days are counted.
    """
    cutoff_ts = int(time.time()) - days * 86400 if days else 0
    stats: dict[str, dict] = {}
    for scene_id, records in (mindora_record or {}).items():
      if not isinstance(records, list) or not records:
        continue
      total_duration = 0
      count = 0
      for entry in records:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
          try:
            ts = int(entry[0])
          except (TypeError, ValueError):
            continue
          if ts < cutoff_ts:
            continue
          count += 1
          if len(entry) >= 2 and entry[1] is not None:
            try:
              total_duration += float(entry[1])
            except (TypeError, ValueError):
              pass
      if count > 0:
        stats[scene_id] = {"count": count, "total_duration": round(total_duration, 1)}
    return stats

  @staticmethod
  def _pick_most_used_scene(mindora_record: dict, days: int | None = None) -> Optional[tuple[str, dict]]:
    """Return (scene_id, stats) for the scene with the highest usage count."""
    stats = UserProfileServ._calc_scene_stats(mindora_record, days=days)
    if not stats:
      return None
    best_id = max(stats.items(), key=lambda x: x[1]["count"])[0]
    return best_id, stats[best_id]

  def _update_scene_stats(self, profile: UserProfile):
    """Pre-compute most-used scene (all-time and last 7 days) and persist them in sleep_analysis."""
    now = int(time.time())

    # All-time most used scene
    most_used = self._pick_most_used_scene(profile.mindora_record)
    if most_used is None:
      profile.sleep_analysis.pop("most_used_scene", None)
    else:
      scene_id, scene_stats = most_used
      short_id = scene_id.replace("sleep.scene.", "")
      profile.sleep_analysis["most_used_scene"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "count": scene_stats["count"],
        "total_duration": scene_stats["total_duration"],
        "updated_at": now,
      }

    # Most used scene in the last 7 days
    most_used_7d = self._pick_most_used_scene(profile.mindora_record, days=7)
    if most_used_7d is None:
      profile.sleep_analysis.pop("most_used_scene_7d", None)
    else:
      scene_id, scene_stats = most_used_7d
      short_id = scene_id.replace("sleep.scene.", "")
      profile.sleep_analysis["most_used_scene_7d"] = {
        "scene_id": short_id,
        "scene_name": short_id.replace("_", " ").title(),
        "count": scene_stats["count"],
        "total_duration": scene_stats["total_duration"],
        "updated_at": now,
      }

  @staticmethod
  def _pick_best_sleep_quality_scene(profile: UserProfile, days: int = 7) -> Optional[dict]:
    """Return the scene whose usage before sleep onset produced the highest avg sleep_quality.

    For each sleep night in the last ``days`` days:
      - Compute sleep onset datetime from sleep_data.timestamp + first_sleep_time.
      - Look at scene usages in the 4-hour wind-down window before onset.
      - Pick the scene usage closest to onset as the "effective" scene for that night.
      - Attribute that night's sleep_quality to that scene.

    The returned dict contains the scene with the highest average sleep_quality.
    """
    if not profile or not profile.sleep_data or not profile.mindora_record:
      return None

    now_ts = int(time.time())
    cutoff_ts = now_ts - days * 86400

    # Collect all scene usages with metadata.
    usages: list[dict] = []
    for scene_id, records in profile.mindora_record.items():
      if not isinstance(records, list) or not records:
        continue
      short_id = scene_id.replace("sleep.scene.", "")
      scene_name = short_id.replace("_", " ").title()
      for entry in records:
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
          try:
            ts = int(entry[0])
          except (TypeError, ValueError):
            continue
          if ts < cutoff_ts:
            continue
          usages.append({
            "scene_id": short_id,
            "scene_name": scene_name,
            "timestamp": ts,
          })

    if not usages:
      return None

    # Attribute sleep_quality to the effective scene per night.
    scene_qualities: dict[str, list[float]] = {}
    for record in profile.sleep_data[-days:]:
      if record.timestamp < cutoff_ts:
        continue
      if record.sleep_quality is None or not record.first_sleep_time:
        continue

      try:
        sleep_dt = datetime.datetime.fromtimestamp(record.timestamp)
        hour, minute = record.first_sleep_time.split(":")
        onset_dt = sleep_dt.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if onset_dt > sleep_dt:
          onset_dt -= datetime.timedelta(days=1)
      except Exception:
        continue

      onset_ts = int(onset_dt.timestamp())
      window_start_ts = onset_ts - 4 * 3600  # 4-hour wind-down window

      # Pick the scene usage closest to onset but not after it.
      best_usage = None
      best_delta = None
      for usage in usages:
        ts = usage["timestamp"]
        if ts < window_start_ts or ts > onset_ts:
          continue
        delta = onset_ts - ts
        if best_delta is None or delta < best_delta:
          best_delta = delta
          best_usage = usage

      if best_usage is None:
        continue

      scene_qualities.setdefault(best_usage["scene_id"], []).append(record.sleep_quality)

    if not scene_qualities:
      return None

    best_scene_id = max(
      scene_qualities.items(),
      key=lambda item: sum(item[1]) / len(item[1]),
    )[0]
    qualities = scene_qualities[best_scene_id]
    avg_quality = round(sum(qualities) / len(qualities), 1)

    # Find a human-readable name for the best scene.
    scene_name = best_scene_id.replace("_", " ").title()
    for usage in usages:
      if usage["scene_id"] == best_scene_id:
        scene_name = usage["scene_name"]
        break

    return {
      "scene_id": best_scene_id,
      "scene_name": scene_name,
      "avg_sleep_quality": avg_quality,
      "nights": len(qualities),
      "updated_at": now_ts,
    }

  def _update_best_scene_by_sleep_quality(self, profile: UserProfile):
    """Persist the scene with the highest avg sleep_quality in the last 7 days."""
    best = self._pick_best_sleep_quality_scene(profile, days=7)
    if best is None:
      profile.sleep_analysis.pop("best_sleep_quality_scene_7d", None)
      return
    profile.sleep_analysis["best_sleep_quality_scene_7d"] = best

  @staticmethod
  def _profile_for_log(profile: UserProfile) -> dict:
    """Return a compact dict for logging (large fields are summarized)."""
    data = profile.model_dump(mode="json", exclude_none=True)
    if isinstance(data.get("behaviors"), dict):
      data["behaviors"] = {
        k: len(v) if isinstance(v, list) else v for k, v in data["behaviors"].items()
      }
    if isinstance(data.get("sleep_scenarios_reco"), list):
      data["sleep_scenarios_reco"] = [
        {"scenario_id": s.get("scenario_id"), "scenario_name": s.get("scenario_name")}
        for s in data["sleep_scenarios_reco"]
      ]
    if isinstance(data.get("standard_sop_reco"), list):
      data["standard_sop_reco"] = [
        {"scenario_id": s.get("scenario_id"), "scenario_name": s.get("scenario_name")}
        for s in data["standard_sop_reco"]
      ]
    if isinstance(data.get("mindora_record"), dict):
      data["mindora_record"] = {
        k: len(v) if isinstance(v, list) else v for k, v in data["mindora_record"].items()
      }
    if isinstance(data.get("sleep_data"), list):
      data["sleep_data"] = len(data["sleep_data"])
    return data


  def update_profile(self, uid: str, new_profile: UserProfile, skip_sleep_scenarios_reco_update: bool = False) -> bool:
    """写入用户行为（仅更新单个用户数据）"""
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    with self.lock:
      # 读取或创建用户画像（仅操作单个用户，避免全量加载）
      profile = self.get_profile(uid)
      old_profile = profile
      if profile is None:
        self._update_scene_stats(new_profile)
        self._update_best_scene_by_sleep_quality(new_profile)
        if not skip_sleep_scenarios_reco_update and _HAS_RECO and Config.ENABLE_SLEEP_RECO:
          new_profile.sleep_scenarios_reco = RecommendationEngine.generate(new_profile)
          new_profile.standard_sop_reco = RecommendationEngine.generate_sop_reco(
            new_profile,
            [key for key in new_profile.mindora_record.keys() if "sleep.scene." in key],
          )
        self.save_profile(uid, new_profile)
        return True

      # just replace, if need
      if len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
        profile.uid_emb = new_profile.uid_emb

      profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)

      profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)

      # aggregate SOP play events into mindora_record so we can keep behaviors small
      self._update_mindora_record(profile, new_profile)
      self._update_scene_stats(profile)
      self._update_best_scene_by_sleep_quality(profile)

      # 仅保存当前用户的更新（而非全量数据）
      self.save_profile(uid, profile)
      logging.info(
        "Profile updated uid=%s summary=%s",
        uid,
        self._profile_for_log(profile),
      )
      return True

  def close(self):
    # 纯 JSON 文件存储，无外部资源需要关闭
    pass

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
  @staticmethod
  def _request_for_log(req_or_data) -> Any:
    """Return a log-safe copy with behaviors summarized by count."""
    if isinstance(req_or_data, BaseModel):
      data = req_or_data.model_dump(mode="json", exclude_none=True)
    elif isinstance(req_or_data, dict):
      data = copy.deepcopy(req_or_data)
    else:
      return req_or_data

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
    # 本分支（嵌入式轻量部署）无 LLM 需求，不实例化任何 LLM 组件
    self.user_serv = UserProfileServ()
    self.update_task = None
    self.app = web.Application()
    self.active_uid = ""
    self.system_uid = get_or_create_uuid()
    self.debug_uid_set = {"mindora_test_uid1", "mindora_test_uid2", "mindora_test_uid3", "test_debug_user_001"}
    self.setup_routes()

  def close(self):
    self.user_serv.close()
    if self.update_task:
      self.update_task.cancel()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/user_profile', self.handle_profile_request_http)
    self.app.router.add_post('/login', self.handle_login_http)

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
    elif data.uid == "active_uid" and self.active_uid:
      # "active_uid" 别名：直接解析为最近通过 /login 鉴权的真实 uid。
      # 不是调试后门——只有在有用户完成 JWT 登录后才可解析，独立于 debug 白名单。
      # 统一在这里映射，保证 query/update/remote-sync 各路径行为一致。
      uid = self.active_uid
    elif Config.IS_DEBUG and data.uid is not None and len(data.uid) > 3 and data.uid in self.debug_uid_set:
      uid = data.uid

    return uid

  def handle_query_profile(self, request: ProfileRequest) -> BaseResponse:
    logging.info("handle query_profile request=%s", self._request_for_log(request))
    """查询用户画像（从 JSON 文件存储按需读取）"""
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
    if profile:
      logging.info("profile found uid=%s summary=%s", uid, self.user_serv._profile_for_log(profile))
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data={"user_profile": profile.model_dump()})
    else:
      logging.warning("uid=%s query not found request=%s", uid, self._request_for_log(request))
      return ProfileResponse(code=0, msg=f"User with uid '{request.data}' not found", request_type=request.request_type, data=None)

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
        self.user_serv.update_profile,
        uid,
        request.data.user_profile,
        request.data.skip_sleep_scenarios_reco_update,
      )
    if succ:
      return ProfileResponse(code=0, msg=f"update profile for '{request.timestamp}' succ", request_type=request.request_type, data=None)
    else:
      return ProfileResponse(code=500, msg=f"update profile failed", request_type=request.request_type, data=None)

  async def sync_profile_to_remote(self, uid: str, request: ProfileRequest) -> bool:
    if not Config.RemoteHost or len(Config.RemoteHost) < 10:
      return False

    profile = self.user_serv.get_profile(uid)
    if profile is None:
      logging.warning(f"skip remote sync because local profile missing for uid={uid}")
      return False

    remote_endpoint = f"{Config.RemoteHost.rstrip('/')}/user_profile"
    sync_data = {"user_profile": profile.model_dump()}
    if request.data.jwt_token is not None:
      sync_data["jwt_token"] = request.data.jwt_token
    else:
      sync_data["uid"] = uid
    if request.data.skip_sleep_scenarios_reco_update:
      sync_data["skip_sleep_scenarios_reco_update"] = True

    payload = ProfileRequest(
      request_type="update_profile",
      timestamp=int(time.time()),
      version=request.version,
      data=ProfileData.model_validate(sync_data),
    ).model_dump()

    try:
      async with ClientSession() as session:
        async with session.post(
          remote_endpoint,
          json=payload,
          headers={REMOTE_SYNC_HEADER: "1"},
          timeout=10,
        ) as response:
          resp_data = await response.json()
          if response.status >= 400:
            logging.error(f"remote profile sync failed status={response.status}, body={resp_data}")
            return False
          logging.info(f"remote profile sync succ for uid={uid}, body={resp_data}")
          return True
    except Exception as e:
      logging.error(f"remote profile sync error for uid={uid}: {e}")
      return False

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

  async def handle_profile_request_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      logging.info("req %s", self._request_for_log(data))
      req = ProfileRequest.model_validate(data)
      logging.info("request %s", self._request_for_log(req))

      if req.request_type == "query_profile":
        response_obj = self.handle_query_profile(req)
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      elif req.request_type == "update_profile":
        response_obj = await self.handle_update_profile(req)
        if (
          response_obj.code == 0
          and Config.RemoteHost is not None and len(Config.RemoteHost) > 8
          and req.data is not None
        ):
          uid = self._parse_for_uid(req.data)
          if isinstance(uid, str) and uid:
            remote_succ = await self.sync_profile_to_remote(uid, req)
            if not remote_succ:
              response_obj.msg = f"{response_obj.msg}, remote sync failed"
        return web.json_response(response_obj.model_dump(), status=get_http_status(response_obj))

      else:
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)

    except ValidationError as e:
        logging.error(f"Validation error: {e}")
        return web.json_response(InvalidReqFormatResp().model_dump(), status=400)
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        return web.json_response(BaseResponse(code=500, msg="Internal server error").model_dump(), status=500)
      
  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      request = AuthRequest.model_validate(data)
      response_obj = self.handle_login(request)
    except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as e:
      logging.error(f"login error: {e}")
      response_obj = InvalidReqFormatResp()

    logging.info(f"login resp: {response_obj}")
    if response_obj.code == 0 and len(Config.RemoteHost) > 10 and (self.update_task is None or self.update_task.done()):
      self.update_task = asyncio.create_task(self.fetch_profile_from_remote(f"{Config.RemoteHost}")) 
    else:
      logging.info("update task has started already")

    return web.json_response(status=get_http_status(response_obj), data=response_obj.model_dump())
  
  async def fetch_profile_from_remote(self, url):
    """Periodically pull the active user's profile from the remote server.

    Runs in a background task so it must not block the asyncio event loop.
    """
    start_min = int(time.time()) / 60
    logging.info(f"begin to loop update for activeuid : {self.active_uid}")
    while True:
      cur_min = int(time.time()) / 60
      if cur_min - start_min > 60:
        logging.info("break because of time")
        break

      await asyncio.sleep(60)

      resp = await query_profile(self.jwt_token, Config.RemoteHost)
      if resp is None or resp.code != 0 or resp.data is None:
        logging.warning(f"none or invalid resp from remote server: {Config.RemoteHost}")
        continue

      profile_data = resp.data.get("user_profile")
      if profile_data is None:
        logging.warning(f"remote resp missing user_profile for activeuid={self.active_uid}")
        continue

      try:
        new_profile = UserProfile.model_validate(profile_data)
      except ValidationError as e:
        logging.error(f"remote profile validation failed: {e}")
        continue

      # Offload the synchronous update_profile to a thread so the event loop
      # stays responsive for local HTTP requests.
      succ = await asyncio.to_thread(
        self.user_serv.update_profile,
        self.active_uid,
        new_profile,
      )
      if not succ:
        logging.warning(f"erro in update profile for {self.active_uid}")
      else:
        logging.info(f"succ update profile for {self.active_uid}")

  async def start_http(self):
    """启动HTTP服务器"""
    runner = web.AppRunner(self.app)
    await runner.setup()
    site = web.TCPSite(runner, self.host, self.port)
    await site.start()
    logging.info(f"UserServer (JSON storage) started on http://{self.host}:{self.port}")
    # 保持服务运行
    await asyncio.Event().wait()


if __name__ == "__main__":
  server = UserServer()
  try:
    asyncio.run(server.start_http())
  except KeyboardInterrupt:
    logging.warning("Shutting down UserServer.")
    server.close()  # 关闭存储资源
