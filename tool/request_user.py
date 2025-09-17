import asyncio
import json
import websockets
import sys

async def query_profile(uid: str, server_uri: str):
    """查询指定用户的画像并打印"""
    async with websockets.connect(server_uri) as websocket:
        # 构建查询请求
        request = {
            "action": "query_profile",
            "uid": uid
        }

        # 发送请求并等待响应
        await websocket.send(json.dumps(request))
        response = await websocket.recv()
        response_data = json.loads(response)

        # 格式化打印结果
        print("用户画像查询结果：")
        print(json.dumps(response_data, indent=4, ensure_ascii=False))

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：python query_profile.py <用户UID>")
        print("示例：python query_profile.py user_123")
        sys.exit(1)
    
    # 服务器默认地址为 ws://localhost:8765（与UserServer对应）
    asyncio.run(query_profile(sys.argv[1], "ws://localhost:8765"))
