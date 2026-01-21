import asyncio,json,logging,os,time
from typing import Optional
from dotenv import load_dotenv
import jwt
from pydantic import ValidationError
import websockets
from aiohttp import ClientResponseError, ClientSession, web
import plyvel
from user_profile import UserProfile
from config import Config
from common import util
from user_profile import (
  UserProfile, ProfileRequest, ProfileResponse, ProfileData,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp,BaseResponse
)
from auth import AuthRequest
from uid.uuid import get_or_create_uuid
import logger

logger.init_log("user_server_logs")

# all bloking sync api
class UserProfileServ:
  MAX_BEHAVIOR_LEN = 1024
  def __init__(self):
    # 初始化LevelDB（若路径不存在则自动创建）
    self.db = plyvel.DB(Config.DB_PATH, create_if_missing=True)

  def get_profile(self, uid: str) -> Optional[UserProfile]:
    """从LevelDB读取单个用户的画像"""
    if not uid or not isinstance(uid, str):
      logging.error(f"erro uid : {uid}")
      return None

    data = self.db.get(uid.encode('utf-8'))  # LevelDB键值为bytes类型
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
        old_behaviors[behavior_type] = old_behaviors[behavior_type][len(old_behaviors) - UserServer.MAX_BEHAVIOR_LEN:]

    logging.info(f"after update {old_behaviors}")
    return old_behaviors

    # incr update the behaviors by time, and update long term weight
  def update_profile(self, uid: str, new_profile: UserProfile) -> bool:
    """写入用户行为（仅更新单个用户数据）"""
    if new_profile is None or uid is None or not isinstance(uid, str):
      logging.error(f"invalid new profile {new_profile} or uid {uid}")
      return False

    # 读取或创建用户画像（仅操作单个用户，避免全量加载）
    profile = self.get_profile(uid)
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

load_dotenv()
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

def get_http_status(resp: BaseResponse):
  status = 200
  if resp.code != 0:
    status = resp.code
  return status


async def query_profile(jwt_token: str, server_uri: str) :
  query_endpoint = f"{server_uri}/query_profile"
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
      error_msg = f"查询失败 [HTTP {e.status}]: {e.msg}"
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
      payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=Config.ALGORITHM)
    except jwt.ExpiredSignatureError:
      logging.error("login token expired")
      return None
    except jwt.InvalidTokenError:
      logging.error("login token invalid")
      return None

    logging.info(f"payload: {payload}")
    return payload

  def _parse_for_uid(self, data: ProfileData):
    uid = None
    if data.jwt_token is not None:
      payload = self._check_token(data.jwt_token)
      if payload is None:
        return InvalidOrExpiredTokenResp()
      uid = payload.get("uid")
    elif data.uid is not None and len(data.uid) > 3:
      uid = data.uid

    return uid

  def handle_query_profile(self, request: ProfileRequest) -> BaseResponse:
    """查询用户画像（从LevelDB按需读取）"""
    if request.data is None:
      logging.error("query request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)

    if uid is None:
      return InvalidOrExpiredTokenResp()

    if uid == "active_uid":
      uid = self.active_uid

    profile = self.user_serv.get_profile(uid)
    if profile:
      return ProfileResponse(code=0, msg="succ", request_type=request.request_type, data=ProfileData(user_profile=profile))
    else:
      logging.warning(f"{uid}, {request} not found")
      return BaseResponse(code=0, msg=f"User with uid '{request.uid}' not found")

    # incr update the behaviors by time, and update long term weight
  def handle_update_profile(self, request: ProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    if request.data is None:
      logging.error("update request without any data")
      return InvalidOrExpiredTokenResp()

    uid = self._parse_for_uid(request.data)

    if uid is None:
      return InvalidOrExpiredTokenResp()

    succ = self.user_serv.update_profile(uid, request.data.user_profile)
    if succ:
      return ProfileResponse(code=0, msg=f"update profile for '{request.timestamp}' succ", request_type=request.request_type, data=None)
    else:
      return BaseResponse(code=500, msg=f"update profile failed")

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


  async def handle_profile_request_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      logging.info(f"req {data}")
      req = ProfileRequest.model_validate(data)
      logging.info(f"request {req}")
      if req.request_type == "query_profile":
        response_obj = self.handle_query_profile(req)
      elif req.request_type == "update_profile":
        response_obj = self.handle_update_profile(req)
      else:
        response_obj = BaseResponse(code=400, msg="Invalid request type")

    except (json.JSONDecodeError, TypeError, KeyError, ValidationError) as e:
      response_obj = BaseResponse(code=400, msg=f"Invalid request format: {e}")

    logging.info(f"profile response: {response_obj}")
    return web.json_response(status = get_http_status(response_obj), data=response_obj.model_dump())
      

  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      request = AuthRequest.model_validate(data)
      response_obj = self.handle_login(request)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
      response_obj = InvalidReqFormatResp()

    logging.info(f"login resp: {response_obj}")
    if response_obj.code == 0 and len(Config.RemoteHost) > 10 and self.update_task is None or self.update_task.done():
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