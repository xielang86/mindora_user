import os


def _parse_comma_list(value: str | None) -> list[str]:
  """将逗号分隔的字符串去重、去空、转小写后返回列表。"""
  if not value:
    return []
  return [item.strip().lower() for item in value.split(",") if item.strip()]


class Config:
  HOST="0.0.0.0"
  PORT = 9001
  IS_DEBUG = True
  AUTH_PORT=9103
  DB_PATH = "data/userprofile_level_db"
  USER_PROFILE_STORAGE_MODE = "leveldb"  # "leveldb" | "txt_json"
  USER_PROFILE_JSON_PATH = "data/user_profiles.txt"
  MaxServerConcurrent = 32
  Mode = 0
  RemoteHost="http://121.43.54.25:9001"
  # RemoteHost="http://localhost:9001"
  ALGORITHM="HS256"
  # 边缘端轻量化开关：关闭后 user_server 不会 import/实例化 LLM 与推荐引擎
  ENABLE_LLM = True
  ENABLE_SLEEP_RECO = True

  # 同一用户两次 LLM 后台更新之间的最小间隔（秒）
  LLM_UPDATE_COOLDOWN_SECONDS = 300
  # 全局并发 LLM 后台任务上限
  MAX_LLM_BACKGROUND_TASKS = 8

  # ---------------------------------------------------------------------------
  # 旧版 DELETE_USER 接口访问控制：JWT 直接删除风险较高，仅允许特定 IP/MAC 调用。
  # 支持环境变量 DELETE_USER_ALLOWED_IPS / DELETE_USER_ALLOWED_MACS 覆盖，
  # 多个值用英文逗号分隔；未配置（空列表）时默认拒绝所有旧版删除请求。
  # ---------------------------------------------------------------------------
  DELETE_USER_ALLOWED_IPS: list[str] = _parse_comma_list(
    os.getenv("DELETE_USER_ALLOWED_IPS", "")
  )
  DELETE_USER_ALLOWED_MACS: list[str] = _parse_comma_list(
    os.getenv("DELETE_USER_ALLOWED_MACS", "")
  )
  # 从请求头读取客户端 MAC 地址的 header 名
  DELETE_USER_MAC_HEADER: str = os.getenv("DELETE_USER_MAC_HEADER", "X-Device-Mac")
