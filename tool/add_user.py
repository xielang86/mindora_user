import asyncio
import json
import websockets
import sys
from typing import Dict, Any

async def send_profile(uid: str, profile_data: Dict[str, Any], server_uri: str):
    uri = "ws://localhost:9101"
    async with websockets.connect(uri) as websocket:
        
        # --- 示例 1: 写入用户行为 ---
        print("--- 1. Writing behavior for user 'client007' ---")
        write_req = WriteBehaviorRequest(
            uid="client007",
            behaviors={
                "heart_rate": [78, 80],
                "clicks": ["product_page_1", "checkout_button"]
            }
        )
        
        # 发送请求
        await websocket.send(json.dumps(write_req.to_dict()))
        
        # 接收并解析响应
        response_json = await websocket.recv()
        response_data = json.loads(response_json)
        write_res = WriteBehaviorResponse.from_dict(response_data)
        
        print(f"Received Response: {write_res}")
        print("-" * 20)

async def main(local_file: str, server_uri: str):
    """读取本地文件并批量发送用户画像"""
    try:
        # 读取本地用户画像文件
        with open(local_file, "r") as f:
            local_profiles = json.load(f)

        # 逐个发送用户数据
        for uid, profile_data in local_profiles.items():
            print(f"正在发送用户 {uid} 的数据...")
            await send_profile(uid, profile_data, server_uri)

        print("所有用户数据发送完成")

    except FileNotFoundError:
        print(f"错误：本地文件 {local_file} 不存在")
    except json.JSONDecodeError:
        print(f"错误：文件 {local_file} 不是有效的JSON格式")
    except Exception as e:
        print(f"发生错误：{str(e)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法：python read_and_send_profile.py <本地用户画像文件路径>")
        print("示例：python read_and_send_profile.py user_profiles_backup.json")
        sys.exit(1)
    
    # 服务器默认地址为 ws://localhost:8765（与UserServer对应）
    asyncio.run(main(sys.argv[1], "ws://localhost:8765"))
