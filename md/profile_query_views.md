# `query_profile` 固定视图协议

请求路径仍为 `POST /user_server/user_profile`，`request_type` 仍为 `query_profile`。在 `data` 中增加 `profile_view`：

| 值 | 用途 | 读取的 LevelDB 分片 | 响应 |
| --- | --- | --- | --- |
| 省略或 `compat` | 旧版 iOS 和旧版设备共用 | `device` 的 7 个分片，加 `profile:avatar`（共 8 个） | 设备视图的顶层字段，`profile` 同时包含 App 与设备需要的字段 |
| `app` | iOS 个人资料回填（客户端显式选择后） | `profile:details`、`profile:avatar` | `revision`、`profile` 的资料/地址/头像字段 |
| `device` | 设备画像同步 | `profile:details`、`internal:preferences`、`plans:state`、`sleep:records`、`scene:summary`、`analysis:content`、`recommendation:current` | 当前设备实际使用的配置、最近一晚、日级报告、推荐和版本 |
| `all` | 调试、数据核对和完整导出 | 全部 21 个 v4 分片 | 原完整 `query_profile` 响应；原有 `sleep_data_count`、`include_behaviors`、`analysis_report_count`、`engagement_count`、`include_mindora_record`、SOP 诊断开关继续生效 |

`app` 的 `profile` 只含 `nickname`、`gender`、`age`、`birthday`、`email`、`phone`、`weight`、`height`、`address_list`、`avatar_base64`、`avatar_mime_type`。服务端身高体重可能是数字；iOS 查询解码器同时接受数字和字符串。

`device` 的顶层字段限定为 `revision`、`profile`、`basic_info`、`uid_emb`、`long_term_profile`、`sleep_plan`、`sleep_data`、`sleep_analysis`、`analysis_reports`、`bedtime_advice`、`sleep_scenarios_reco`、`standard_sop_reco`、`sleep_health`、`sleep_mode`。`profile` 只含昵称、性别、年龄、生日、邮箱、身高、体重、宗教和教练偏好；不传头像、地址、电话。`sleep_data` 只返回最新一晚，`analysis_reports` 只返回最新一份日级报告。其他旧裁剪参数在此视图下不改变固定集合。

`compat` 的顶层字段与 `device` 相同，`profile` 字段为 `app` 与 `device` 的并集，包含电话、地址、头像。四个视图均返回 `data.user_profile.revision`，不存在的用户沿用 `code=0, data=null`。认证、时段查询门禁和错误码沿用原 `query_profile`。无效的 `profile_view` 拒绝为 400。服务端仅按固定视图选择分片，不接受客户端任意内部字段名。原有裁剪参数只在 `all` 中使用；其他视图始终返回固定集合。

分片读取收益要求画像已迁移至 schema v4。旧单键画像和 schema v2/v3 仍可兼容读取；v3 查询 `analysis:content` 时会分别读取旧的 `analysis:advice` 与 `analysis:reports`。迁移工具为 `tool/migrate_profile_v2.py`。响应字段精简在兼容路径上依然生效。

部署 v4 前先备份并停止写入 LevelDB 的服务，在数据库副本上依次运行迁移工具的 `inspect`、`migrate`、`verify`，确认零错误和迁移前后画像一致，再对停服后的正式库执行迁移。迁移保留业务 `revision`，原两个分析分片在同一 write batch 中退役。旧版服务不识别 schema v4；若需回滚旧版代码，先恢复迁移前备份或使用明确的反向迁移工具，不能只切换代码。

App 和设备均可暂不发版：现有请求不传 `profile_view`，新服务端默认返回两端字段并集。若直接默认 `device`，App 会失去电话、地址和头像；尤其 App 在读取后可能同步资料，因此不能依赖缺失字段的默认值。设备 OTA 后显式发送 `profile_view="device"`；App 后续可显式发送 `profile_view="app"`。设备先于服务端更新时，旧服务端会忽略该字段并返回原响应，设备仍可解析。依赖完整画像的调试脚本及工具必须显式发送 `profile_view="all"`；仅升级服务端就会改变它们不带视图时的响应。

压测默认组合以无参数兼容视图（`app` 操作）和显式设备视图（`device` 操作）代替完整画像，具体权重按实际流量另行校准。显式 App 视图可用 `app_only` 操作测试。完整画像可通过 `profile=...` 显式运行回归测试，不纳入默认性能对比。
