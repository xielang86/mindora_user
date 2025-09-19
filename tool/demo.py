# client_example.py
import asyncio
import json
import websockets
from aiohttp import ClientSession, ClientResponseError  
  
# 客户端也需要导入数据模型来构建请求和解析响应
from user_profile import (
    UpdateProfileRequest, UpdateProfileResponse,UserProfile,
    QueryProfileRequest, QueryProfileResponse, ErrorResponse
  )
  
all_profiles = [
  UserProfile (
    uid="client007",
    behaviors={
      "heart_rate": [(1758101400,90),(1758101430, 85),(1758101460, 80)],
      "clicks": [(1758101400, "product_page_1"), (1758101430, "checkout_button")]
    }
  ),
  
  UserProfile (
    uid="client007",
    behaviors={
      "heart_rate": [(1758101415,88), (1758101430, 86), (1758101500, 72)],
      "clicks": [(1758101400, "product_page_fake"), (1758101440, "checkout_button")]
    }
  ),

  UserProfile (
    uid="client001",
    behaviors={
      "heart_rate": [(1758101515,88), (1758101530, 86), (1758101600, 72)],
      "clicks": [(1758101500, "product_page_2"), (1758101530, "checkout_button1")]
    }
  )
]

all_query = ["client007", "client001", "ghost"]

async def websocket_demo():
  uri = "ws://localhost:9101"
  async with websockets.connect(uri) as websocket:
      
    # --- 示例 1: 写入用户行为 ---
    print("--- 1. Writing behavior for user 'client007' ---")
    for user_profile in all_profiles:
      update_req = UpdateProfileRequest
      update_req.user_profile = user_profile
      await websocket.send(json.dumps(update_req.to_dict()))
  
      # 接收并解析响应
      response_json = await websocket.recv()
      response_data = json.loads(response_json)
      write_res = UpdateProfileResponse.from_dict(response_data)
  
      print(f"Received Response: {write_res}")
      print("-" * 20)
  
    # --- 示例 2: 查询刚才写入的用户 ---
    print("--- 2. Querying profile for user 'client007' ---")
    for uid in all_query:
      query_req = QueryProfileRequest(uid=uid)
  
      await websocket.send(json.dumps(query_req.to_dict()))
  
      response_json = await websocket.recv()
      response_data = json.loads(response_json)
  
      if response_data.get("status") == "success":
        query_res = QueryProfileResponse.from_dict(response_data)
        print(f"Received Response: {query_res}")
        # 你现在可以直接访问对象属性
        print(f"User {query_res.profile}")
      else:
        error_res = ErrorResponse.from_dict(response_data)
        print(f"Received Error: {error_res}")
      print("-" * 20)

class UserServerHttpClient:
  def __init__(self, server_url: str):
    self.server_url = server_url.rstrip("/")
    self.query_endpoint = f"{self.server_url}/query_profile"
    self.update_endpoint = f"{self.server_url}/update_profile"

  async def query_user_profile(self, uid: str) -> dict:
    async with ClientSession() as session:
      try:
        # 构造请求数据
        payload = {"uid": uid}
        
        # 发送POST请求
        async with session.post(
            self.query_endpoint,
            json=payload,
            timeout=10  # 10秒超时
        ) as response:
            response.raise_for_status()  # 触发HTTP错误（如4xx、5xx）
            return await response.json()
              
      except ClientResponseError as e:
        # 处理HTTP错误响应
        error_msg = f"查询失败 [HTTP {e.status}]: {await e.response.text()}"
        raise Exception(error_msg) from e
      except Exception as e:
        raise Exception(f"查询用户画像失败: {str(e)}") from e

  async def update_user_profile(
    self,
    user_profile:UserProfile
  ) -> dict:
    async with ClientSession() as session:
      try:
        # 构造请求数据（仅包含非None的字段）
        payload = {"action":"update_profile", "user_profile":user_profile.to_dict()} 
        # 发送POST请求
        async with session.post(
          self.update_endpoint,
          json=payload,
          timeout=10
        ) as response:
          response.raise_for_status()
          return await response.json()
              
      except ClientResponseError as e:
        error_msg = f"更新失败 [HTTP {e.status}]: {await e.response.text()}"
        raise Exception(error_msg) from e
      except Exception as e:
        raise Exception(f"更新用户画像失败: {str(e)}") from e

async def http_demo():
  # client = UserServerHttpClient("http://localhost:9102")
  client = UserServerHttpClient("http://192.168.0.221:9102")
  try:
    for user_profile in all_profiles:
      update_result = await client.update_user_profile(user_profile)
      print("更新结果:", json.dumps(update_result, indent=2, ensure_ascii=False))

      # 2. 查询用户画像
    for uid in all_query:
      query_result = await client.query_user_profile(uid=uid)
      print("查询结果:", json.dumps(query_result, indent=2, ensure_ascii=False))

  except Exception as e:
    print(f"操作失败: {e}")

if __name__ == "__main__":
    # asyncio.run(websocket_demo())
    asyncio.run(http_demo())
