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
