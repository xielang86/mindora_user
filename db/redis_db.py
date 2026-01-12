import redis
from redis.exceptions import RedisError
from dotenv import load_dotenv
import os, logging, time

# 加载配置文件
load_dotenv()

class RedisDB:
  """Redis操作封装（连接池+通用读写）"""
  _pool = None

  def __init__(self):
    self.host = os.getenv("REDIS_HOST")
    self.port = int(os.getenv("REDIS_PORT"))
    self.db = int(os.getenv("REDIS_DB"))
    self.password = os.getenv("REDIS_PASSWORD") or None
    self._init_pool()

  def _init_pool(self):
    """初始化Redis连接池"""
    if not RedisDB._pool:
      RedisDB._pool = redis.ConnectionPool(
        host=self.host,
        port=self.port,
        db=self.db,
        password=self.password,
        decode_responses=True  # 自动将bytes转为str
      )

  def get_client(self):
    """获取Redis客户端（从连接池）"""
    try:
      return redis.Redis(connection_pool=RedisDB._pool)
    except RedisError as e:
      raise Exception(f"Redis连接失败：{str(e)}")

  def get(self, key: str) -> str | None:
    """读取Redis值"""
    client = self.get_client()
    try:
      return client.get(key)
    except RedisError as e:
      raise Exception(f"Redis读取失败：{str(e)}")

  def set(self, key: str, value: str, expire_seconds: int = None) -> bool:
    """
    写入Redis值（支持过期时间）
    :param key: 键
    :param value: 值
    :param expire_seconds: 过期时间（秒），None则永久
    :return: 是否成功
    """
    client = self.get_client()
    try:
      if expire_seconds:
        client.setex(key, expire_seconds, value)
      else:
        client.set(key, value)
      return True
    except RedisError as e:
      raise Exception(f"Redis写入失败：{str(e)}")

# 初始化Redis实例（全局单例）
redis_db = RedisDB()

# ------------------- 业务封装：验证码/JWT操作 -------------------
def get_verify_code(email: str, device_id: str) -> str | None:
  """获取验证码（key格式：verify_code:{email}:{device_id}）"""
  key = f"verify_code:{email}:{device_id}"
  return redis_db.get(key)

def set_verify_code(email: str, device_id: str, code: str, expire_seconds: int ) -> int:
  key = f"verify_code:{email}:{device_id}"
  return redis_db.set(key, code, expire_seconds)

def set_jwt_token(uid: str, device_id: str, token: str, expire_seconds: int):
  """存储JWT Token（key格式：jwt_token:{uid}:{device_id}）"""
  key = f"jwt_token:{uid}:{device_id}"
  redis_db.set(key, token, expire_seconds)

def get_jwt_token(uid: str, device_id: str):
  """存储JWT Token（key格式：jwt_token:{uid}:{device_id}）"""
  key = f"jwt_token:{uid}:{device_id}"
  return redis_db.get(key)


if __name__ == "__main__":
  import logger
  email = "zhouzhao@example.com"
  did = "xielang"
  verify_code = "1234"
  expire_seconds = 1
  set_verify_code(email, did, verify_code, expire_seconds)
  read_code = get_verify_code(email, did)
  logging.info(f"write vcode={verify_code}, and read for {read_code}")
  time.sleep(expire_seconds + 1)
  read_code = get_verify_code(email, did)
  logging.info(f"after sleep expire_seconds, write vcode={verify_code}, and read for {read_code}")

  uid = "mindora"
    
  jwt_token = "jflasfjdlsakjf"
  set_jwt_token(uid=uid, device_id = did, token = jwt_token, expire_seconds=expire_seconds)
  read_token = get_jwt_token(uid=uid, device_id=did)
  logging.info(f"write jwt_token={jwt_token}, and read for {read_token}")
  time.sleep(expire_seconds + 1)
  read_token = get_jwt_token(uid=uid, device_id=did)
  logging.info(f"after sleep expire_seconds, read for {read_token}")
