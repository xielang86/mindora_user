import asyncio
import json
from typing import Dict, Any, Optional
from datetime import datetime
import websockets
import plyvel  # LevelDB的Python绑定
from user_profile import UserProfile
from config import Config
from common import util
from user_profile import (
  UserProfile, QueryProfileRequest, UpdateProfileRequest,
  QueryProfileResponse, UpdateProfileResponse, ErrorResponse, BaseResponse
)

class UserServer:
  MAX_BEHAVIOR_LEN = 1024
  def __init__(self, host: str, port: int, db_path: str = "data/user_profiles_leveldb"):
    self.host = host
    self.port = port
    self.db_path = db_path
    # 初始化LevelDB（若路径不存在则自动创建）
    self.db = plyvel.DB(db_path, create_if_missing=True)

  def get_profile(self, uid: str) -> Optional[UserProfile]:
    """从LevelDB读取单个用户的画像"""
    data = self.db.get(uid.encode('utf-8'))  # LevelDB键值为bytes类型
    if data:
      return UserProfile.from_dict(json.loads(data.decode('utf-8')))
    return None

  def save_profile(self, profile: UserProfile):
    """将单个用户的画像写入LevelDB"""
    uid = profile.uid
    data = json.dumps(profile.to_dict()).encode('utf-8')
    self.db.put(uid.encode('utf-8'), data)

  async def handle_query_profile(self, request: QueryProfileRequest) -> BaseResponse:
    """查询用户画像（从LevelDB按需读取）"""
    uid = request.uid
    if not uid or not isinstance(uid, str):
      return {"status": "error", "message": "Missing or invalid 'uid'"}

    profile = self.get_profile(uid)
    if profile:
      return QueryProfileResponse(status="success", profile=profile)
    else:
      return ErrorResponse(status="not_found", message=f"User with uid '{request.uid}' not found")

  def _merge_profile(self, old_profile, new_profile):
    return old_profile

  def _merge_behavior(self, old_behaviors, new_behaviors):
    # merge sort, consider the old ones is sorted already
    print(f"merge {old_behaviors} and {new_behaviors}")
    for behavior_type, values in new_behaviors.items():
      if behavior_type in old_behaviors and isinstance(values, list):
        values.sort(key=lambda x:x[0])
        print(values)
        old_behaviors[behavior_type]= util.merge_two_sorted(old_behaviors[behavior_type], values) 
      else:
        old_behaviors[behavior_type] = values

      if len(old_behaviors[behavior_type]) > UserServer.MAX_BEHAVIOR_LEN:
        old_behaviors[behavior_type] = old_behaviors[behavior_type][len(old_behaviors) - UserServer.MAX_BEHAVIOR_LEN:]

    print(f"after update {old_behaviors}")
    return old_behaviors

    # incr update the behaviors by time, and update long term weight
  async def handle_update_profile(self, request: UpdateProfileRequest) -> BaseResponse:
    """写入用户行为（仅更新单个用户数据）"""
    uid = request.uid
    if not uid or not isinstance(uid, str):
      return ErrorResponse(status="error", message=f"invalid uid{uid}")

    # 读取或创建用户画像（仅操作单个用户，避免全量加载）
    profile = self.get_profile(uid) or UserProfile(uid)
    if profile.uid is None:
      profile.uid = uid

    # just replace, if need
    new_emb = request.uid_emb
    if len(new_emb) > 16 or profile.uid_emb is None or len(profile.uid_emb) == 0:
      profile.uid_emb = new_emb

    profile.long_term_profile = self._merge_profile(profile.long_term_profile, request.long_term_profile)
    
    profile.behaviors = self._merge_behavior(profile.behaviors, request.behaviors)
    # 仅保存当前用户的更新（而非全量数据）
    self.save_profile(profile)
    return UpdateProfileResponse(status="success", message=f"Behavior data for uid '{request.uid}' updated")

  async def handle_request(self, websocket, path=None):
    try:
      async for message in websocket:
        response_obj: BaseResponse
        try:
          data = json.loads(message)
          action = data.get("action")

          if action == "query_profile":
            req = QueryProfileRequest.from_dict(data)
            response_obj = await self.handle_query_profile(req)
          elif action == "update_profile":
            req = UpdateProfileRequest.from_dict(data)
            response_obj = await self.handle_update_profile(req)
          else:
            response_obj = ErrorResponse(message="Invalid action")

        except (json.JSONDecodeError, TypeError, KeyError) as e:
          response_obj = ErrorResponse(message=f"Invalid request format: {e}")
        
        await websocket.send(json.dumps(response_obj.to_dict()))
    except websockets.exceptions.ConnectionClosed:
      print("Connection closed.")

  async def start(self):
    async with websockets.serve(self.handle_request, self.host, self.port):
      print(f"UserServer (LevelDB) started on ws://{self.host}:{self.port}")
      await asyncio.Future()  # 持续运行

  def close(self):
    """关闭LevelDB连接（程序退出时调用）"""
    self.db.close()

if __name__ == "__main__":
  server = UserServer("localhost", Config.PORT, Config.DB_PATH)
  try:
    asyncio.run(server.start())
  except KeyboardInterrupt:
    print("Shutting down UserServer.")
    server.close()  # 关闭LevelDB连接