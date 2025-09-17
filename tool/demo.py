# client_example.py
import asyncio
import json
import websockets

# 客户端也需要导入数据模型来构建请求和解析响应
from user_profile import (
  UpdateProfileRequest, UpdateProfileResponse,
  QueryProfileRequest, QueryProfileResponse, ErrorResponse
)

async def main():
  uri = "ws://localhost:9101"
  async with websockets.connect(uri) as websocket:
      
    # --- 示例 1: 写入用户行为 ---
    print("--- 1. Writing behavior for user 'client007' ---")
    update_req1 = UpdateProfileRequest(
      uid="client007",
      behaviors={
        "heart_rate": [(1758101400,90),(1758101430, 85),(1758101460, 80)],
        "clicks": [(1758101400, "product_page_1"), (1758101430, "checkout_button")]
      }
    )

    update_req2 = UpdateProfileRequest(
      uid="client007",
      behaviors={
        "heart_rate": [(1758101415,88), (1758101430, 86), (1758101500, 72)],
        "clicks": [(1758101400, "product_page_fake"), (1758101440, "checkout_button")]
      }
    )

    update_req3 = UpdateProfileRequest(
      uid="client001",
      behaviors={
        "heart_rate": [(1758101515,88), (1758101530, 86), (1758101600, 72)],
        "clicks": [(1758101500, "product_page_2"), (1758101530, "checkout_button1")]
      }
    )
    
    for update_req in [update_req1, update_req2, update_req3]:
      await websocket.send(json.dumps(update_req.to_dict()))
  
      # 接收并解析响应
      response_json = await websocket.recv()
      response_data = json.loads(response_json)
      write_res = UpdateProfileResponse.from_dict(response_data)
  
      print(f"Received Response: {write_res}")
      print("-" * 20)
  
    # --- 示例 2: 查询刚才写入的用户 ---
    print("--- 2. Querying profile for user 'client007' ---")
    for uid in ["client007", "client001"]:
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

    # --- 示例 3: 查询一个不存在的用户 ---
    print("--- 3. Querying profile for non-existent user 'ghost999' ---")
    query_req_fail = QueryProfileRequest(uid="ghost999")

    await websocket.send(json.dumps(query_req_fail.to_dict()))

    response_json = await websocket.recv()
    response_data = json.loads(response_json)

    if response_data.get("status") == "success":
      query_res = QueryProfileResponse.from_dict(response_data)
      print(f"Received Response: {query_res}")
    else:
      # 将错误信息解析为 ErrorResponse 对象
      error_res = ErrorResponse.from_dict(response_data)
      print(f"Received Error as expected: {error_res}")
    print("-" * 20)

if __name__ == "__main__":
    asyncio.run(main())
