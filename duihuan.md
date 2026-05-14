# Redemption Code API

## 概述

| 项目 | 说明 |
|---|---|
| 协议 | HTTP/1.1 |
| 请求方式 | POST |
| Content-Type | `application/json` |
| 默认地址 | `http://127.0.0.1:9103/auth` |

### 通用请求结构

所有兑换码与权益接口共享同一顶层请求格式：

```json
{
  "request_type": "<类型标识>",
  "version": "1.0",
  "timestamp": 1711296000,
  "data": { ... }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `request_type` | string | 是 | 请求类型，决定路由到哪个业务逻辑 |
| `version` | string | 是 | 协议版本，当前固定为 `1.0` |
| `timestamp` | int | 是 | 客户端发起时间（Unix 秒级时间戳） |
| `data` | object | 是 | 业务数据容器，各接口字段不同 |

### 鉴权说明

| 场景 | 鉴权方式 |
|---|---|
| 用户兑换码、查询权益 | `data.jwt_token` 必填 |
| 后台生成兑换码 | `data.admin_secret` 必填 |

### 通用响应结构

```json
{
  "request_type": "query_user_rights",
  "code": 0,
  "msg": "success",
  "data": { ... }
}
```

| `code` | 含义 |
|---|---|
| `0` | 成功 |
| `400` | 请求格式错误或兑换规则不允许 |
| `401` | Token 无效或已过期 |
| `403` | 用户状态无效、兑换码已禁用或后台口令错误 |
| `404` | 用户或兑换码不存在 |
| `409` | 兑换码已被使用 |
| `410` | 兑换码已过期 |
| `500` | 服务端内部错误 |

---

## 权益模型

服务端当前内置三档权益等级：

| `user_level` | 说明 |
|---|---|
| `free` | 免费用户 |
| `pro` | 专业版用户 |
| `premium` | 高级版用户 |

### 权益返回字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `stored_user_level` | string | 数据库中记录的等级 |
| `effective_user_level` | string | 当前实际生效的等级，过期后回退到 `free` |
| `level_end_at` | string/null | 会员到期时间，ISO 8601 格式 |
| `membership_active` | bool | 当前会员是否仍有效 |
| `rights` | object | App 可直接使用的权益清单 |
| `rights.llm_models` | string[] | 允许使用的模型列表 |
| `rights.algorithms` | string[] | 允许使用的分析算法列表 |
| `rights.analysis_depth` | string | 分析深度，例如 `basic` / `advanced` / `premium` |
| `rights.max_reports_per_day` | int | 每日可用分析次数上限 |
| `server_time` | string | 服务端当前时间，ISO 8601 格式 |

### 当前默认权益映射

| user_level | llm_models | algorithms |
|---|---|---|
| `free` | `sleep-basic-v1` | `sleep_stage_basic`, `daily_summary_basic` |
| `pro` | `sleep-basic-v1`, `sleep-advanced-v2` | 增加 `sleep_trend_pro`, `insight_engine_pro` |
| `premium` | `sleep-basic-v1`, `sleep-advanced-v2`, `sleep-expert-v3` | 增加 `sleep_coach_premium`, `multi_day_correlation` |

---

## request_type = `generate_redemption_codes`

后台批量生成兑换码。

### 请求体

```json
{
  "request_type": "generate_redemption_codes",
  "version": "1.0",
  "timestamp": 1711296000,
  "data": {
    "admin_secret": "your-admin-secret",
    "batch_id": "spring-sale-2026",
    "target_level": "premium",
    "duration_days": 365,
    "quantity": 3,
    "code_expire_at": "2026-12-31T23:59:59"
  }
}
```

### 参数说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data.admin_secret` | string | 是 | 后台生成兑换码口令，服务端与环境变量 `REDEMPTION_ADMIN_SECRET` 比较 |
| `data.batch_id` | string | 是 | 批次号，用于渠道、活动、订单归档 |
| `data.target_level` | string | 是 | 目标等级，当前支持 `free` / `pro` / `premium` |
| `data.duration_days` | int | 是 | 兑换后有效天数，必须大于 0 |
| `data.quantity` | int | 是 | 本次生成数量，必须大于 0 |
| `data.code_expire_at` | string | 否 | 兑换码自身过期时间，ISO 8601 格式；为空表示不过期 |

### 成功响应

```json
{
  "request_type": "generate_redemption_codes",
  "code": 0,
  "msg": "redemption codes generated",
  "data": {
    "batch_id": "spring-sale-2026",
    "target_level": "premium",
    "duration_days": 365,
    "quantity": 3,
    "expire_at": "2026-12-31T23:59:59",
    "codes": [
      {
        "code": "MDR-ABCD-EFGH-JKLM-NPQR",
        "batch_id": "spring-sale-2026",
        "target_level": "premium",
        "duration_days": 365,
        "expire_at": "2026-12-31T23:59:59"
      },
      {
        "code": "MDR-STUV-WXYZ-2345-6789",
        "batch_id": "spring-sale-2026",
        "target_level": "premium",
        "duration_days": 365,
        "expire_at": "2026-12-31T23:59:59"
      }
    ]
  }
}
```

### 失败响应

| code | 说明 |
|---|---|
| `400` | 参数缺失、`target_level` 非法、`duration_days` 或 `quantity` 小于等于 0 |
| `403` | `admin_secret` 错误 |
| `503` | 服务端未配置 `REDEMPTION_ADMIN_SECRET` |

---

## request_type = `redeem_redemption_code`

用户使用兑换码激活或延长权益。

### 请求体

```json
{
  "request_type": "redeem_redemption_code",
  "version": "1.0",
  "timestamp": 1711296000,
  "data": {
    "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "redemption_code": "MDR-ABCD-EFGH-JKLM-NPQR"
  }
}
```

### 参数说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data.jwt_token` | string | 是 | 当前登录用户 JWT |
| `data.redemption_code` | string | 是 | 用户输入的兑换码 |

### 兑换规则

1. 同等级兑换码：从当前 `level_end_at` 往后顺延。
2. 更高等级兑换码：立即升级，从当前时间开始计算。
3. 当前高等级仍有效时，不允许用更低等级兑换码覆盖。
4. 每个兑换码只能被成功使用一次。

### 成功响应

```json
{
  "request_type": "redeem_redemption_code",
  "code": 0,
  "msg": "redemption success",
  "data": {
    "stored_user_level": "premium",
    "effective_user_level": "premium",
    "level_end_at": "2027-04-27T13:45:21",
    "membership_active": true,
    "rights": {
      "llm_models": ["sleep-basic-v1", "sleep-advanced-v2", "sleep-expert-v3"],
      "algorithms": [
        "sleep_stage_basic",
        "daily_summary_basic",
        "sleep_trend_pro",
        "insight_engine_pro",
        "sleep_coach_premium",
        "multi_day_correlation"
      ],
      "analysis_depth": "premium",
      "max_reports_per_day": 100
    },
    "server_time": "2026-04-27T13:45:21",
    "redeemed_code": {
      "batch_id": "spring-sale-2026",
      "target_level": "premium",
      "duration_days": 365,
      "activated_at": "2026-04-27T13:45:21",
      "action": "activated"
    }
  }
}
```

### 失败响应

| code | 说明 |
|---|---|
| `400` | 请求格式错误，或兑换规则不允许（例如高等级有效期内用低等级码） |
| `401` | `jwt_token` 无效或已过期 |
| `403` | 用户状态异常，或兑换码已禁用 |
| `404` | 用户不存在，或兑换码不存在 |
| `409` | 兑换码已被使用 |
| `410` | 兑换码已过期 |

---

## request_type = `query_user_rights`

查询当前用户的权益状态。App 可在登录后、启动时、或兑换成功后再次调用。

### 请求体

```json
{
  "request_type": "query_user_rights",
  "version": "1.0",
  "timestamp": 1711296000,
  "data": {
    "jwt_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  }
}
```

### 参数说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data.jwt_token` | string | 是 | 当前登录用户 JWT |

### 成功响应

```json
{
  "request_type": "query_user_rights",
  "code": 0,
  "msg": "success",
  "data": {
    "stored_user_level": "pro",
    "effective_user_level": "pro",
    "level_end_at": "2026-10-24T00:00:00",
    "membership_active": true,
    "rights": {
      "llm_models": ["sleep-basic-v1", "sleep-advanced-v2"],
      "algorithms": [
        "sleep_stage_basic",
        "daily_summary_basic",
        "sleep_trend_pro",
        "insight_engine_pro"
      ],
      "analysis_depth": "advanced",
      "max_reports_per_day": 20
    },
    "server_time": "2026-04-27T13:52:10"
  }
}
```

### 失败响应

| code | 说明 |
|---|---|
| `401` | `jwt_token` 无效或已过期 |
| `500` | 服务端内部错误 |

---

## 登录接口的权益返回

除专门的权益接口外，以下登录成功响应也会直接返回权益数据：

| request_type | 说明 |
|---|---|
| `login_with_email_verify_code` | 邮箱验证码登录 |
| `login_with_email_password` | 邮箱密码登录 |
| `login_with_jwt` | JWT 刷新登录 |
| `register_with_email_password` | 邮箱密码注册 |
| `register_with_phone` | 手机号注册 |
| `login_with_phone_sms` | 手机号验证码登录 |
| `wechat_callback` | 微信登录 |

### 登录成功响应示例

```json
{
  "request_type": "login_with_jwt",
  "code": 0,
  "msg": "Token is valid",
  "data": {
    "uid": "da4bc1b6baf0c8ab0be7a7f157144f4ca274359e5836c9941c1cd4122c39b61a",
    "email": "xielangtc@163.com",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expire_days": 7,
    "user_level": "pro",
    "effective_user_level": "pro",
    "level_end_at": "2026-10-24T00:00:00",
    "rights": {
      "llm_models": ["sleep-basic-v1", "sleep-advanced-v2"],
      "algorithms": [
        "sleep_stage_basic",
        "daily_summary_basic",
        "sleep_trend_pro",
        "insight_engine_pro"
      ],
      "analysis_depth": "advanced",
      "max_reports_per_day": 20
    }
  }
}
```

### 返回字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.uid` | string | 用户唯一 ID |
| `data.email` | string | 用户邮箱 |
| `data.token` | string | 新的 JWT |
| `data.expire_days` | int | JWT 过期天数 |
| `data.user_level` | string | 数据库存储等级 |
| `data.effective_user_level` | string | 当前实际生效等级 |
| `data.level_end_at` | string/null | 会员到期时间 |
| `data.rights` | object | 当前等级权益 |
