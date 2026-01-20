import asyncio,json,requests
from aiohttp import ClientSession, ClientResponseError  
from user_profile import QueryProfileRequest,QueryProfileResponse
import sys

async def query_profile(uid_or_token: str, server_uri: str):
  """查询指定用户的画像并打印"""
  query_endpoint = f"{server_uri}/query_profile"
  print(f"len: {len(uid_or_token)}")
  async with ClientSession() as session:
    try:
      # 构造请求数据
      if len(uid_or_token) > 64:
        payload = {"jwt_token": uid_or_token, "uid": "1234", "action": "query_profile"}
      else:
        payload = {"uid": uid_or_token, "jwt_token": "12345", "action": "query_profile"}
      
      # 发送POST请求
      async with session.post(
        query_endpoint,
        json=payload,
        timeout=10  # 10秒超时
      ) as response:
        response.raise_for_status()  # 触发HTTP错误（如4xx、5xx）
        print(await response.json())
            
    except ClientResponseError as e:
      # 处理HTTP错误响应
      error_msg = f"查询失败 [HTTP {e.status}]: {e.message}"
      raise Exception(error_msg) from e
    except Exception as e:
      raise Exception(f"查询用户画像失败: {str(e)}") from e

# async def query_profile(jwt_token: str, server_uri: str) :
#   """查询指定用户的画像并打印"""
#   query_endpoint = f"{server_uri}/query_profile"
#   req = QueryProfileRequest(jwt_token = jwt_token)
#   print(f"before send post: {req}")
#   resp_login = requests.post(query_endpoint, data=req.model_dump_json())
#   print(f"响应: {resp_login}")
#   return QueryProfileResponse.model_validate(resp_login.json())

if __name__ == "__main__":
  if len(sys.argv) != 2:
    print("用法：python query_profile.py <用户UID>")
    print("示例：python query_profile.py user_123")
    sys.exit(1)
  # uri = "http://121.43.54.25:9001"
  uri = "http://localhost:9001"
  token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOiJkYTRiYzFiNmJhZjBjOGFiMGJlN2E3ZjE1NzE0NGY0Y2EyNzQzNTllNTgzNmM5OTQxYzFjZDQxMjJjMzliNjFhIiwiZW1haWwiOiJ4aWVsYW5ndGNAMTYzLmNvbSIsImV4cCI6MTc2ODgyNDY3N30.ERMNaNg6K4IAlfgnxLqqE7vc-dEh8FCnTXjNrixmgis"
  asyncio.run(query_profile(token, uri))
  # asyncio.run(query_profile(sys.argv[1], uri))
