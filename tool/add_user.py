import asyncio
import json
from typing import Dict, Any
import asyncio
import aiohttp
import json
from typing import List, Dict, Any

class UserProfileUpdater:
  def __init__(self, uri: str, filename: str):
    """
    初始化用户画像更新器
    
    Args:
        uri: 服务器更新接口地址 (如: http://localhost:8080/update_profile)
        filename: 包含用户画像数据的JSON文件路径
    """
    self.uri = uri
    self.filename = filename
    self.response_queue = asyncio.Queue()  # 存储响应的队列
    self.max_concurrent = 8  # 最大并发数，可根据服务器承载能力调整
    self.semaphore = asyncio.Semaphore(self.max_concurrent)  # 控制并发量

  def load_user_profiles(self) -> List[Dict[str, Any]]:
    """从JSON文件加载用户画像数据"""
    try:
      with open(self.filename, 'r', encoding='utf-8') as f:
        data = json.load(f)
        # 验证数据格式是否为列表
        if not isinstance(data, list):
          raise ValueError("JSON文件内容必须是包含用户画像的列表")
        return data
    except FileNotFoundError:
      raise Exception(f"文件 {self.filename} 不存在")
    except json.JSONDecodeError:
      raise Exception(f"文件 {self.filename} 不是有效的JSON格式")

  async def _send_single_update(self, session: aiohttp.ClientSession, profile: Dict[str, Any]):
    """发送单个用户画像更新请求"""
    async with self.semaphore:  # 限制并发数量
      try:
        # 构造更新请求数据
        payload = {
          "action": "update_profile",
          "user_profile": profile
        }
        
        # 发送POST请求
        async with session.post(
          self.uri,
          json=payload,
          timeout=15
        ) as response:
          response_data = await response.json()
          # 将响应放入队列
          await self.response_queue.put({
            "success": True,
            "profile_uid": profile.get("uid", "unknown"),
            "data": response_data
          })
            
      except Exception as e:
        # 错误信息也放入队列
        await self.response_queue.put({
          "success": False,
          "profile_uid": profile.get("uid", "unknown"),
          "error": str(e)
        })

  async def _print_responses(self):
    """从队列中读取并打印响应结果（独立协程）"""
    # 持续从队列获取响应直到收到终止信号
    while True:
      response = await self.response_queue.get()
      
      # 检查是否是终止信号
      if response is None:
        break
          
      # 打印响应内容
      if response["success"]:
        print(f"✅ 用户 {response['profile_uid']} 更新成功: {response['data']['message']}")
      else:
        print(f"❌ 用户 {response['profile_uid']} 更新失败: {response['error']}")
          
      # 标记任务完成
      self.response_queue.task_done()

  async def _process_all_profiles(self):
    """处理所有用户画像的主协程"""
    # 加载用户数据
    profiles = self.load_user_profiles()
    print(f"已加载 {len(profiles)} 个用户画像，开始发送更新请求...")
    
    # 创建HTTP会话（复用连接提高效率）
    async with aiohttp.ClientSession() as session:
      # 启动打印响应的协程
      print_task = asyncio.create_task(self._print_responses())
      
      # 创建所有发送任务
      tasks = [
        self._send_single_update(session, profile)
        for profile in profiles
      ]
      
      # 等待所有发送任务完成
      await asyncio.gather(*tasks)
      
      # 发送终止信号给打印协程
      await self.response_queue.put(None)
      # 等待打印协程处理完所有响应并退出
      await print_task

  def run(self):
    """入口函数：启动整个更新流程"""
    try:
      asyncio.run(self._process_all_profiles())
      print("所有用户画像更新请求已处理完毕")
    except Exception as e:
      print(f"执行过程中发生错误: {str(e)}")

# 使用示例
if __name__ == "__main__":
  # 服务器更新接口地址
  server_uri = "http://localhost:9102/update_profile"
  # 包含用户画像的JSON文件
  profiles_file = "data/user_profile.json"
  
  # 创建更新器并运行
  updater = UserProfileUpdater(server_uri, profiles_file)
  updater.run()