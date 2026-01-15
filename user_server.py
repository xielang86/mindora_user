import asyncio
import json
import logging
import os
from typing import Optional
from dotenv import load_dotenv
import jwt
import websockets
from aiohttp import web
import plyvel
from user_profile import UserProfile
from config import Config
from common import util
from user_profile import (
  UserProfile, QueryProfileRequest, UpdateProfileRequest,
  QueryProfileResponse, UpdateProfileResponse, BaseResponse,
  InvalidOrExpiredTokenResp, InvalidReqFormatResp
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

  def save_profile(self, profile: UserProfile):
    """将单个用户的画像写入LevelDB"""
    uid = profile.uid
    data = json.dumps(profile.model_dump()).encode('utf-8')
    self.db.put(uid.encode('utf-8'), data)

  def _merge_profile(self, old_profile, new_profile):
    return old_profile

  def _merge_behavior(self, old_behaviors, new_behaviors):
    # merge sort, consider the old ones is sorted already
    logging.info(f"merge {old_behaviors} and {new_behaviors}")
    for behavior_type, values in new_behaviors.items():
      if behavior_type in old_behaviors and isinstance(values, list):
        values.sort(key=lambda x:x[0])
        old_behaviors[behavior_type]= util.merge_two_sorted(old_behaviors[behavior_type], values) 
      else:
        old_behaviors[behavior_type] = values

      if len(old_behaviors[behavior_type]) > UserProfileServ.MAX_BEHAVIOR_LEN:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][len(old_behaviors) - UserServer.MAX_BEHAVIOR_LEN:]

    logging.info(f"after update {old_behaviors}")
    return old_behaviors

    # incr update the behaviors by time, and update long term weight
  def update_profile(self, new_profile: UserProfile) -> bool:
    """写入用户行为（仅更新单个用户数据）"""
    if new_profile is None or new_profile.uid is None or not isinstance(new_profile.uid, str):
      logging.error(f"invalid new profile {new_profile}")
      return False

    # 读取或创建用户画像（仅操作单个用户，避免全量加载）
    profile = self.get_profile(new_profile.uid)
    if profile is None:
      self.save_profile(new_profile)
      return True

    # just replace, if need
    if len(new_profile.uid_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
      profile.uid_emb = new_profile.uid_emb

    profile.long_term_profile = self._merge_profile(profile.long_term_profile, new_profile.long_term_profile)
    
    profile.behaviors = self._merge_behavior(profile.behaviors, new_profile.behaviors)
    # 仅保存当前用户的更新（而非全量数据）
    self.save_profile(profile)
    logging.info(f"Behavior data for uid '{new_profile.uid}' updated")
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

class UserServer:
  def __init__(self):
    server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
    self.host = Config.HOST
    self.port = Config.PORT
    self.user_serv = UserProfileServ()
    self.app = web.Application()
    self.active_uid = ""
    self.system_uid = get_or_create_uuid()

    self.setup_routes()

  def close(self):
    self.user_serv.close()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/query_profile', self.handle_query_profile_http)
    self.app.router.add_post('/update_profile', self.handle_update_profile_http)
    self.app.router.add_post('/login', self.handle_login_http)

  def check_token(self, jwt_token: str)-> dict | None:
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
    
  def handle_query_profile(self, request: QueryProfileRequest) -> BaseResponse:
    """查询用户画像（从LevelDB按需读取）"""
    uid = None
    if request.jwt_token is not None:
      payload = self.check_token(request.jwt_token)
      if payload is None:
        return InvalidOrExpiredTokenResp()
      uid = payload.get("uid")
    elif request.uid is not None and len(request.uid) > 3:
      uid = request.uid
    else:
      return InvalidOrExpiredTokenResp()

    profile = self.user_serv.get_profile(uid)
    if profile:
      return QueryProfileResponse(code=0, profile=profile)
    else:
      logging.warning(f"{uid}, {request} not found")
      return BaseResponse(code=0, message=f"User with uid '{request.uid}' not found")

    # incr update the behaviors by time, and update long term weight
  def handle_update_profile(self, request: UpdateProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    uid = None
    if request.jwt_token is not None:
      payload = self.check_token(request.jwt_token)
      if payload is None:
        logging.error("failed to parse token")
        return InvalidOrExpiredTokenResp()
      uid = payload.get("uid")
    elif request.user_profile.uid is None or len(request.user_profile.uid) < 4:
      return InvalidOrExpiredTokenResp()

    if uid is not None and len(uid) > 3:
      request.user_profile.uid = uid

    succ = self.user_serv.update_profile(request.user_profile)
    if succ:
      return UpdateProfileResponse(code=0, message=f"Behavior data for req '{request}' updated")
    else:
      return BaseResponse(code=500, message=f"update profile failed")

  def handle_login(self, request: AuthRequest) -> BaseResponse:
    if request.data is None or request.data.jwt_token is None:
      return InvalidReqFormatResp()

    payload = self.check_token(request.data.jwt_token)
    if payload is None:
      return InvalidOrExpiredTokenResp()

    uid = payload.get("uid")
    self.active_uid = uid
    return BaseResponse(code=0, message="user ativated successufully")

  async def handle_request(self, websocket, path=None):
    try:
      async for message in websocket:
        response_obj: BaseResponse
        try:
          data = json.loads(message)
          action = data.get("action")

          if action == "query_profile":
            req = QueryProfileRequest.model_validate(data)
            response_obj = self.handle_query_profile(req)
          elif action == "update_profile":
            req = UpdateProfileRequest.model_validate(data)
            response_obj = self.handle_update_profile(req)
          else:
            response_obj = BaseResponse(code=400, message="Invalid action")

        except (json.JSONDecodeError, TypeError, KeyError) as e:
          response_obj = BaseResponse(code=400, message=f"Invalid request format: {e}")
        
        await websocket.send(json.dumps(response_obj.model_dump()))
    except websockets.exceptions.ConnectionClosed:
      logging.error("Connection closed.")

  async def handle_query_profile_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      logging.info(f"req {data}")
      req = QueryProfileRequest.model_validate(data)
      response_obj = self.handle_query_profile(req)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
      logging.error(f"excpetion:{e}")
      response_obj = InvalidReqFormatResp()
    logging.info(f"query profile result: {response_obj}")
    return web.json_response(status = get_http_status(response_obj), data=response_obj.model_dump())

  async def handle_update_profile_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      req = UpdateProfileRequest.model_validate(data)
      response_obj = self.handle_update_profile(req)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
      response_obj = InvalidReqFormatResp()
   
    return web.json_response(status = get_http_status(response_obj), data=response_obj.model_dump())

  async def handle_login_http(self, request: web.Request) -> web.Response:
    try:
      data = await request.json()
      response_obj = self.handle_login(data)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
      response_obj = InvalidReqFormatResp()
    
    return web.json_response(status=get_http_status(response_obj), data=response_obj.model_dump())
  


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
    async with websockets.serve(self.handle_request, self.host, self.port):
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