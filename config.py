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
  # 公网 API 域名（拼接上传图片等资源的可公开访问 URL，如弹窗主图）
  PUBLIC_API_BASE = os.getenv("PUBLIC_API_BASE", "https://api.mindora316.com")
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
  # kimi 方向（Anthropic Messages 协议）：Kimi 编程订阅端点，key=KIMI_API_KEY（env）
  # base/model 可用 KIMI_API_BASE / KIMI_MODEL（env）覆盖；base 不含 /v1/messages（SDK 自动拼）
  KIMI_API_BASE = "https://api.kimi.com/coding"
  KIMI_MODEL = "kimi-k3"
  # 模型路由：request_type → 方向名，"default" 兜底；
  # 所选方向不可用（缺 key）时自动降级到第一个可用方向（见 llm/router.py）
  LLM_ROUTING = {"default": "kimi"}

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

  # ── 洞察规则引擎阈值（Mindora_App睡眠数据展示与分析对照规范_v3.md §4）────────
  # 规范中标注「建议v1 / 阈值应后台可配置 / 待产品确认」的数值全部集中在这里；
  # 这是内置默认值：运营可在不部署代码的情况下，通过运营平台上传
  # data/insight_rules.json 覆盖任意子项（按 key 深度合并，见 insight_rules_config.py，
  # 可用 INSIGHT_RULES_CONFIG_PATH 环境变量改文件路径），热加载无需重启。
  # AN_SCORE 综合评分 v1 公式不采纳（不动现有 sleep_quality 计算），故无此项。
  INSIGHT_RULES_DEFAULT = {
    # AN_DATA_STATE：数据状态机门槛
    "data_state": {"baseline7_min_nights": 4, "baseline30_min_nights": 15},
    # AN_ONSET：昨晚 SOL vs 近7日基线，|delta| 内视为稳定
    "onset": {"delta_stable_min": 3},
    # AN_STRUCTURE：阶段 Δ% 分层（相对近7日均值）
    "structure": {"minor_delta_pct": 20, "major_delta_pct": 40},
    # AN_FLUCTUATION：觉醒事件门槛与展开上限
    "fluctuation": {"awake_min_minutes": 1, "list_max": 2, "expand_max": 3,
                    "intervention_window_min": 10},
    # AN_SCENE / SC_ASSOC：关联样本门槛（不足只说偏好）
    "scene": {"assoc_min_uses_7d": 2, "assoc_min_uses_30d": 3},
    # AN_ADVICE：每日最多条数、同类建议冷却、连续模式最少晚数、历史上限
    "advice": {"max_per_day": 2, "same_type_cooldown_days": 7,
               "pattern_min_nights": 3, "history_max_entries": 30},
    # AN_HOME_SUMMARY：同主题不连续出现的天数
    "home_summary": {"theme_cooldown_days": 1},
    # AN_TREND_7D/30D：有效夜晚门槛、条数上限、最小可报告变化
    "trend": {"max_items_7d": 2, "max_items_30d": 3,
              "min_valid_7d": 4, "min_valid_30d": 15,
              "min_change": {"tst_min": 20, "sol_min": 5, "waso_min": 10,
                             "awake_count": 1, "clock_min": 15}},
    # AN_ONSET_INDEX / AN_STRUCTURE_INDEX / AN_STABILITY_INDEX（建议v1 权重，
    # 缺失子分按剩余权重归一）；label_bands 为产品状态分层（非医学等级），待确认
    "indices": {
      "onset":     {"weights": {"sol": 0.70, "hr_trend": 0.15, "rr_trend": 0.15}},
      "structure": {"weights": {"duration": 0.45, "continuity": 0.35, "stage_stability": 0.20}},
      "stability": {"weights": {"continuity": 0.50, "hr_stability": 0.25, "rr_stability": 0.25}},
      "label_bands": [[80, "excellent"], [60, "good"], [0, "fair"]],
      # 子分公式常量（建议v1，待产品确认）
      "subscore_params": {"sol_full_min": 10, "sol_penalty_per_min": 2,
                          "trend_dev_full_pct": 50, "stage_delta_full_pct": 100,
                          "hr_range_full_pct": 50, "resp_var_full_pct": 50,
                          "waso_penalty_per_min": 2, "awake_extra_penalty": 10},
    },
    # 洞察文案禁用词（规范生成限制；同时用于 LLM 润色输出校验）
    "forbidden_terms": ["stress", "anxiety", "insomnia", "treatment", "diagnos",
                        "焦虑", "失眠", "诊断", "治疗", "有效", "恢复", "焦虑型",
                        "健康异常", "修复充分"],
    # LLM 润色单字段长度上限（字符）
    "polish_max_chars": 220,
  }

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
