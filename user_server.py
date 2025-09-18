import asyncio
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import websockets
from aiohttp import web
import plyvel  # LevelDB的Python绑定
from user_profile import UserProfile
from config import Config
from common import util
from user_profile import (
  UserProfile, QueryProfileRequest, UpdateProfileRequest,
  QueryProfileResponse, UpdateProfileResponse, ErrorResponse, BaseResponse
)
import logger

# all bloking sync api
class UserServ:
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
      return UserProfile.from_dict(json.loads(data.decode('utf-8')))
    return None

  def save_profile(self, profile: UserProfile):
    """将单个用户的画像写入LevelDB"""
    uid = profile.uid
    data = json.dumps(profile.to_dict()).encode('utf-8')
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

      if len(old_behaviors[behavior_type]) > UserServ.MAX_BEHAVIOR_LEN:
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

class UserServer:
  def __init__(self):
    server_semaphore = asyncio.Semaphore(Config.MaxServerConcurrent)
    self.host = Config.HOST
    self.port = Config.PORT
    self.user_serv = UserServ()
    self.app = web.Application()
    self.setup_routes()

  def close(self):
    self.user_serv.close()

  def setup_routes(self):
    """设置HTTP路由"""
    self.app.router.add_post('/query_profile', self.handle_query_profile_http)
    self.app.router.add_post('/update_profile', self.handle_update_profile_http)

  def handle_query_profile(self, request: QueryProfileRequest) -> BaseResponse:
    """查询用户画像（从LevelDB按需读取）"""
    uid = request.uid
    profile = self.user_serv.get_profile(uid)
    if profile:
      return QueryProfileResponse(status="success", profile=profile)
    else:
      return ErrorResponse(status="not_found", message=f"User with uid '{request.uid}' not found")

    # incr update the behaviors by time, and update long term weight
  def handle_update_profile(self, request: UpdateProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    succ = self.user_serv.update_profile(request.user_profile)
    if succ:
      return UpdateProfileResponse(status="success", message=f"Behavior data for req '{request}' updated")
    else:
      return ErrorResponse(status="error", message=f"invalid reqest")

  async def handle_request(self, websocket, path=None):
    try:
      async for message in websocket:
        response_obj: BaseResponse
        try:
          data = json.loads(message)
          action = data.get("action")

          if action == "query_profile":
            req = QueryProfileRequest.from_dict(data)
            response_obj = self.handle_query_profile(req)
          elif action == "update_profile":
            req = UpdateProfileRequest.from_dict(data)
            response_obj = self.handle_update_profile(req)
          else:
            response_obj = ErrorResponse(message="Invalid action")

        except (json.JSONDecodeError, TypeError, KeyError) as e:
          response_obj = ErrorResponse(message=f"Invalid request format: {e}")
        
        await websocket.send(json.dumps(response_obj.to_dict()))
    except websockets.exceptions.ConnectionClosed:
      logging.error("Connection closed.")

  async def handle_query_profile_http(self, request: web.Request) -> web.Response:
      try:
          data = await request.json()
          req = QueryProfileRequest.from_dict(data)
          response_obj = self.handle_query_profile(req)
      except (json.JSONDecodeError, TypeError, KeyError) as e:
          response_obj = ErrorResponse(message=f"Invalid request format: {e}")
      
      return web.json_response(response_obj.to_dict())

  async def handle_update_profile_http(self, request: web.Request) -> web.Response:
      try:
          data = await request.json()
          req = UpdateProfileRequest.from_dict(data)
          response_obj = self.handle_update_profile(req)
      except (json.JSONDecodeError, TypeError, KeyError) as e:
          response_obj = ErrorResponse(message=f"Invalid request format: {e}")
      
      return web.json_response(response_obj.to_dict())

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