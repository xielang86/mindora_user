class Config:
  HOST="0.0.0.0"
  PORT = 9001
  IS_DEBUG = True
  AUTH_PORT=9103
  # 本分支（嵌入式轻量部署）仅用明文 JSON 文件存储用户画像，无 LevelDB
  USER_PROFILE_JSON_PATH = "data/user_profiles.txt"
  MaxServerConcurrent = 32
  Mode = 0
  RemoteHost="http://121.43.54.25:9001"
  # RemoteHost="http://localhost:9001"
  ALGORITHM="HS256"
  # 边缘端轻量化开关：本分支无 LLM 需求（llm_service 已整体移除），
  # 仅保留推荐引擎开关
  ENABLE_SLEEP_RECO = False
