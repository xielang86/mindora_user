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
  # auth_server 地址（user_server 校验运营角色 / 运营后台登录都走它）
  AUTH_SERVER_URL = os.getenv("AUTH_SERVER_URL", f"http://127.0.0.1:{AUTH_PORT}")
  # 运营后台服务端口（ops_admin_server.py）
  OPS_ADMIN_PORT = int(os.getenv("OPS_ADMIN_PORT", "9200"))
  DB_PATH = "data/userprofile_level_db"
  USER_PROFILE_STORAGE_MODE = "leveldb"  # "leveldb" | "txt_json"
  USER_PROFILE_JSON_PATH = "data/user_profiles.txt"
  # 弹窗/问卷运营配置（JSON，运营后台线上写入；user_server 按 mtime 检查更新，
  # 可用 POPUP_SURVEY_CONFIG_PATH 环境变量覆盖为绝对路径）
  POPUP_SURVEY_CONFIG_PATH = "data/popup_survey_config.json"
  MaxServerConcurrent = 32
  Mode = 0
  ALGORITHM="RS256"
  # 验签兼容：旧的 HS256 token（JWT_SECRET_KEY 环境变量）在过期前仍可验
  LEGACY_ALGORITHM="HS256"
  # JWT 密钥文件默认路径（可用 JWT_PRIVATE_KEY_PATH/JWT_PUBLIC_KEY_PATH 环境变量覆盖，
  # 或直接用 JWT_PRIVATE_KEY/JWT_PUBLIC_KEY inline PEM 环境变量）
  JWT_PRIVATE_KEY_PATH = "jwt_private.pem"
  JWT_PUBLIC_KEY_PATH = "jwt_public.pem"
  # 边缘端轻量化开关：关闭后 user_server 不会 import/实例化 LLM 与推荐引擎
  ENABLE_LLM = True
  ENABLE_SLEEP_RECO = True

  # ── 高级会员体验期（高级会员体验期接口.md §4）──────────────────────────────
  # Basic 档（普通会员）内购 product_id 白名单：report_subscription 仅对它们盖第②段体验期；
  # premium.* 已是 Premium，不触发
  BASIC_SUBSCRIPTION_PRODUCT_IDS = frozenset({
    "com.mindora316.monthly",
    "com.mindora316.yearly",
  })

  # ── 睡眠计划同步（睡眠计划同步接口.md）────────────────────────────────────
  # 会员等级查询结果（auth_server query_user_rights）的内存缓存时长；查询失败不缓存、按 free 降级
  SLEEP_PLAN_TIER_CACHE_SECONDS = int(os.getenv("SLEEP_PLAN_TIER_CACHE_SECONDS", "60"))

  # 健康数据口径版本（健康数据同步接口_0814.md §8）：服务端当前认知的最新版本。
  # 上传缺省 health_schema_version 按 1 处理；客户端版本高于此值时告警（不拒绝）。
  HEALTH_SCHEMA_VERSION = int(os.getenv("HEALTH_SCHEMA_VERSION", "2"))

  # ── LLM 请求方向（provider）：api_base/model 放配置文件，api_key 一律走环境变量 ──
  # volc_ark 方向：key=ARK_API_KEY（env），endpoint=ARK_ENDPOINT_ID（env）
  ARK_API_BASE = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
  ARK_MODEL = "doubao-seed-2-0-lite-260215"
  # kimi 方向：key=KIMI_API_KEY（env），model 可用 KIMI_MODEL（env）覆盖
  KIMI_API_BASE = "https://api.moonshot.cn/v1/chat/completions"
  KIMI_MODEL = "moonshot-v1-8k"
  # 模型路由：request_type → 方向名，"default" 兜底；
  # 所选方向不可用（缺 key）时自动降级到第一个可用方向（见 llm/router.py）
  LLM_ROUTING = {"default": "volc_ark"}

  # 同一用户两次 LLM 后台更新之间的最小间隔（秒）
  LLM_UPDATE_COOLDOWN_SECONDS = 300
  # 全局并发 LLM 后台任务上限
  MAX_LLM_BACKGROUND_TASKS = 8

  # ── 醒后自动睡眠分析（方案A：app 零散上传，服务端按数据决定何时跑 LLM）──
  # 总开关：有"比最新日级报告更新"的完整夜晚（sleep_data）时才调度后台 LLM，
  # 否则碎片化上传不触发；False 时退化为仅客户端显式 skip=False 才触发。
  LLM_ANALYSIS_AUTO_TRIGGER = os.getenv("LLM_ANALYSIS_AUTO_TRIGGER", "true").lower() == "true"
  # 防抖：检测到新夜晚后延迟这么久再跑，等醒后的零散健康数据/修正包落地
  LLM_ANALYSIS_DEBOUNCE_SECONDS = int(os.getenv("LLM_ANALYSIS_DEBOUNCE_SECONDS", "600"))
  # 兜底扫描间隔：补触发"重启丢了防抖任务 / LLM 临时失败"漏掉的 uid
  LLM_ANALYSIS_SWEEP_SECONDS = int(os.getenv("LLM_ANALYSIS_SWEEP_SECONDS", "3600"))
  # 活跃门窗口：只有窗口内有活跃信号（/analysis 请求 或 plays 事件）的用户
  # 才做醒后预生成——纯设备后台同步、app 不活跃的用户不烧 LLM
  LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS = int(os.getenv("LLM_ANALYSIS_ACTIVITY_WINDOW_SECONDS", "86400"))

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
