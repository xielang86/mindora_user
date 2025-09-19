import asyncio
import json
from aiohttp import ClientSession, ClientResponseError  
import sys

async def query_profile(uid: str, server_uri: str):
  """查询指定用户的画像并打印"""
  query_endpoint = f"{server_uri}/query_profile"
  async with ClientSession() as session:
    try:
      # 构造请求数据
      payload = {"uid": uid}
      
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
      error_msg = f"查询失败 [HTTP {e.status}]: {await e.response.text()}"
      raise Exception(error_msg) from e
    except Exception as e:
      raise Exception(f"查询用户画像失败: {str(e)}") from e

if __name__ == "__main__":
  if len(sys.argv) != 2:
    print("用法：python query_profile.py <用户UID>")
    print("示例：python query_profile.py user_123")
    sys.exit(1)
  
  asyncio.run(query_profile(sys.argv[1], "http://localhost:9102"))
