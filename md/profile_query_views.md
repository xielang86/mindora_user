# `query_profile` 按 `device_type` 选择响应

请求路径仍为 `POST /user_server/user_profile`，`request_type` 仍为 `query_profile`。服务端使用已有的 `data.device_type` 选择响应；旧 `profile_view` 字段不再参与选择：

| `device_type` | 用途 | 读取的 LevelDB 分片 | 响应 |
| --- | --- | --- | --- |
| 省略、`ios` 或 `android` | App 个人资料回填；省略兼容当前 iOS 请求 | `profile:details`、`profile:avatar` | `revision`、`profile` 的资料/地址/头像字段 |
| `mindora` | 设备画像同步 | `profile:details`、`device:derived`、`sleep:records`、`analysis:text` | 当前设备实际使用的配置、最近一晚、日级报告、推荐和版本；睡眠计划从独立接口获取 |
| `pc` | 调试、数据核对和完整导出 | 全部 14 个 v6 业务分片 | 原完整 `query_profile` 响应；原有 `sleep_data_count`、`include_behaviors`、`analysis_report_count`、`engagement_count`、`include_mindora_record`、SOP 诊断开关继续生效 |

App 响应的 `profile` 只含 `nickname`、`gender`、`age`、`birthday`、`email`、`phone`、`weight`、`height`、`address_list`、`avatar_base64`、`avatar_mime_type`。服务端身高体重可能是数字；iOS 查询解码器同时接受数字和字符串。

`mindora` 的顶层字段限定为 `revision`、`profile`、`basic_info`、`uid_emb`、`long_term_profile`、`sleep_data`、`sleep_analysis`、`analysis_reports`、`bedtime_advice`、`sleep_scenarios_reco`、`standard_sop_reco`、`sleep_health`、`sleep_mode`。`sleep_plan` 从独立的 `/sleep_plan` 接口获取，不在该响应返回。`profile` 只含昵称、性别、年龄、生日、邮箱、身高、体重、宗教和教练偏好；不传头像、地址、电话。`sleep_data` 只返回最新一晚，`analysis_reports` 只返回最新一份日级报告。其他旧裁剪参数在此响应下不改变固定集合。

三类响应均返回 `data.user_profile.revision`；完整调试画像由 `device_type="pc"` 获取，其中保留睡眠计划。不存在的用户沿用 `code=0, data=null`。认证、时段查询门禁和错误码沿用原 `query_profile`。其他 `device_type` 拒绝为 400。服务端仅按固定类型选择分片，不接受客户端任意内部字段名。原有裁剪参数只在 `pc` 中使用；其他类型始终返回固定集合。

分片读取收益要求画像已迁移至 schema v6。旧单键画像和历史分片格式仍可由当前代码迁移读取；迁移工具为 `tool/migrate_profile_v2.py`。响应字段精简在迁移前的读取路径上依然生效，但读取次数较多。

部署 v6 前先备份并停止写入 LevelDB 的服务，在数据库副本上依次运行迁移工具的 `inspect`、`migrate`、`verify`，确认零错误和迁移前后画像一致，再对停服后的正式库执行迁移。迁移保留业务 `revision`。旧版服务不识别 schema v6；若需回滚旧版代码，先恢复迁移前备份或重建旧单键画像，不能只切换代码。

现有设备请求已带 `device_type="mindora"`，无需等待设备移除旧 `profile_view` 字段；当前可见的 iOS 查询请求尚未发送 `device_type`，因此缺省按 App 响应处理。iOS/Android 后续可显式发送平台值。依赖完整画像的调试脚本及工具必须发送 `device_type="pc"`；只发送旧 `profile_view="all"` 将得到 App 响应。

压测默认组合以无类型的 App 响应（`app` 操作）和 `mindora` 设备响应（`device` 操作）代替完整画像，具体权重按实际流量另行校准。显式 iOS 响应可用 `app_only` 操作测试。完整画像可通过 `profile=...` 显式运行回归测试，不纳入默认性能对比。
