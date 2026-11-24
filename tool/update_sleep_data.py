"""update_sleep_data.py — 给指定 uid 灌 N 晚逼真的 sleep_data（+ 可选场景播放事件），
用于 app 逐屏核对 /analysis 字段。

现有工具为什么不够用：
  - user_server_client.update_profile 只是通用传输层，不构造 sleep_data payload
  - test_sleep_insight_e2e.py 的 payload 构造绑死本地测试服务器（端口 19001）
  - fake_mindora_record.py 只灌场景播放事件，且写死生产地址

用法（本地测试服务器 + debug uid）：
  python tool/update_sleep_data.py --base-url http://127.0.0.1:19001 --days 7 --with-plays

用法（真实 LLM 睡眠分析/洞察，服务器需带 ARK_API_KEY 启动）：
  python tool/update_sleep_data.py --base-url http://127.0.0.1:19001 --days 7 --with-plays --with-llm

用法（生产真实 uid）：
  JWT_TOKEN=<token> python tool/update_sleep_data.py \
      --base-url https://api.mindora316.com/user_server --days 7 --with-plays

用法（回放真实画像：用 out_analysis/<uid>_<ts>/query_profile.json 作为 update 输入，
把真实用户的 sleep_data/behaviors 灌进测试服务器复现 LLM 分析）：
  python tool/update_sleep_data.py --base-url http://127.0.0.1:19001 \
      --from-profile out_analysis/ab960bd5..._1786696687 --uid mindora_test_uid1 --with-llm

灌完后用 dump_analysis 逐屏核对：
  python tool/dump_analysis.py --base-url <同上> [--jwt-token <token>]

数据设计（与 /analysis builders 的数据依赖一一对应）：
  - sleep_data 每晚：sleep_quality/soe/onset/first_sleep_time/sleep_arch_index/
    night_var_index/hr|rr_before_sleep/hrv/respiratory_var + sleep_status 分段
    + night_events(intervention) → overview/day/week/month/explore 全部数值模块
  - behaviors.sleep_heart_rate_min/max（会话起点配对）→ 服务端 update 时算出
    hr_min/hr_max → explore night_fluctuation.heart_rate_range（v2 路径）
  - --with-plays：behaviors.plays 的 sop_start 事件 → weekly_best /
    onset_efficiency / scene_preference / month scenario_list
  - 默认带 health_schema_version=2 走新口径（会 purge 覆盖天的 v1 旧 key，
    测试 uid 无所谓；--health-schema-version 1 可关掉）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tool.user_server_client import UserServerClient

DAY = 86400


def _night_payload(wake_ts: int, i: int) -> dict:
  """构造 wake_ts 醒来的那一晚 SleepResult。i 用于让每晚数值有确定性的差异。"""
  session_start = wake_ts - 8 * 3600            # 23:0x 入睡
  first_sleep_min = 23 * 60 + 15 + (i % 10)     # "23:15" ~ "23:24"
  segments = [
    # (offset_min from session_start, duration_min, sleep_type)
    (0,   50,  "core"),
    (50,  70,  "deep"),
    (120, 90,  "core"),
    (210, 60,  "rem"),
    (270, 5 + i % 4, "awake"),                   # 夜间觉醒（→ night_fluctuation）
    (275, 105, "core"),
    (380, 60,  "deep"),
    (440, 40,  "rem"),
  ]
  sleep_status = [
    {
      "start_time": session_start + off * 60,
      "duration": dur,
      "sleep_type": st,
    }
    for off, dur, st in segments
  ]
  return {
    "timestamp": wake_ts,
    "sleep_quality": 88.0 - i,                   # 88, 87, 86 ... 逐晚递减
    "soe": 85.0 - i,
    "onset": 10.0 + i,
    "sleep_arch_index": 78.0 - i,
    "night_var_index": 30.0 + i,
    "first_sleep_time": f"{first_sleep_min // 60:02d}:{first_sleep_min % 60:02d}",
    "bed_time": "23:05",
    "wake_time": "07:05",
    "hr_before_sleep": 66,
    "rr_before_sleep": 14,
    "hrv": 42,
    "respiratory_var": 12,
    "avg_heart_rate": 60,
    "avg_respiratory": 14,
    "sleep_status": sleep_status,
    "night_events": [
      {
        "timestamp": session_start + 270 * 60,   # 觉醒时刻的干预
        "event_type": "intervention",
        "detail": "Rain Wash",
      }
    ],
  }


def build_payload(days: int, with_plays: bool) -> dict:
  now = int(time.time())
  today_7am = datetime.datetime.now().replace(hour=7, minute=5, second=0, microsecond=0)

  sleep_data = []
  hr_min_series, hr_max_series = [], []
  for i in range(1, days + 1):
    wake_ts = int(today_7am.timestamp()) - i * DAY
    night = _night_payload(wake_ts, i)
    sleep_data.append(night)
    session_start = wake_ts - 8 * 3600
    hr_min_series.append([session_start, 54.0 + i % 3])   # 54~56
    hr_max_series.append([session_start, 86.0 - i % 4])   # 83~86

  behaviors = {
    "sleep_heart_rate_min": hr_min_series,
    "sleep_heart_rate_max": hr_max_series,
  }
  if with_plays:
    def play(ts, scene, duration=1800):
      return [ts, {"cmd": f"sleep.scene.{scene}", "event": "sop_start", "duration": duration}]
    # 播放时间对齐每晚入睡前 1 小时（入睡窗口前 4h 内的使用会被归因给当夜，
    # 这样才能算出 best_sleep_quality_scene_7d → weekly_best.score / onset_efficiency.score）
    night_anchor = lambda i: int(today_7am.timestamp()) - i * DAY - 9 * 3600  # 当晚 22:0x
    behaviors["plays"] = [
      # 近 7 天 cocos 5 次 → weekly_best / week onset_efficiency / explore scene_preference
      play(night_anchor(1), "cocos_island_moonlight"),
      play(night_anchor(2), "cocos_island_moonlight"),
      play(night_anchor(3), "cocos_island_moonlight"),
      play(night_anchor(4), "cocos_island_moonlight"),
      play(night_anchor(5), "cocos_island_moonlight"),
      # amalfi 3 次、kyoto 2 次 → month scenario_list top3 的 2、3 名
      play(night_anchor(2), "amalfi_breeze"),
      play(night_anchor(6), "amalfi_breeze"),
      play(night_anchor(8), "amalfi_breeze"),
      play(night_anchor(3), "kyoto_forest"),
      play(night_anchor(10), "kyoto_forest"),
    ]

  return {"uid_emb": [], "behaviors": behaviors, "sleep_data": sleep_data}


INSIGHT_MODULE_KEYS = ["greeting", "onset", "architecture", "intervention", "scene_preference", "micro_education"]

# --from-profile 回放时剥离的服务端托管字段（顶层）：
#   - LLM 产物（sleep_insight/analysis_reports/*_reco）：留着会因 7 天新鲜期/同周期
#     复用判定让服务端跳过 LLM，回放就触发不了重新生成
#   - 服务端计算/同步状态（sleep_analysis/mindora_record/health_sync_*）：update 合并
#     逻辑本就不读这些键，剥离只为减小 payload
_REPLAY_STRIP_KEYS = [
  "sleep_insight", "analysis_reports", "sleep_scenarios_reco", "standard_sop_reco",
  "sleep_analysis", "mindora_record", "health_sync_days", "health_sync_state",
]
# profile 容器内剥离的字段：avatar_base64 可能上 MB，与分析回放无关
_REPLAY_STRIP_PROFILE_KEYS = ["avatar_base64", "avatar_mime_type"]


def load_profile_from_file(path_str: str) -> dict:
  """从 query_profile.json 提取 user_profile 作为 update_profile 的输入。

  接受三种形态：query_profile 完整响应（data.user_profile）/ 含 user_profile 键的
  对象 / 裸 user_profile。path 可以是 json 文件或包含 query_profile.json 的目录
  （直接传 out_analysis/<uid>_<ts> 子目录即可）。
  """
  path = Path(path_str)
  if path.is_dir():
    path = path / "query_profile.json"
  if not path.is_file():
    raise SystemExit(f"❌ 找不到文件: {path}")
  obj = json.loads(path.read_text())
  if isinstance(obj.get("data"), dict) and isinstance(obj["data"].get("user_profile"), dict):
    profile = obj["data"]["user_profile"]      # query_profile 响应
  elif isinstance(obj.get("user_profile"), dict):
    profile = obj["user_profile"]
  else:
    profile = obj                              # 假定是裸 user_profile
  if not isinstance(profile, dict) or not profile:
    raise SystemExit(f"❌ {path} 中未找到有效的 user_profile")

  stripped = [k for k in _REPLAY_STRIP_KEYS if profile.pop(k, None) is not None]
  inner = profile.get("profile")
  if isinstance(inner, dict):
    stripped += [f"profile.{k}" for k in _REPLAY_STRIP_PROFILE_KEYS if inner.pop(k, None) is not None]
  print(f"from-profile: {path}")
  print(f"  剥离服务端托管字段: {stripped or '无'}")
  print(f"  保留: sleep_data={len(profile.get('sleep_data') or [])} 晚, "
        f"behaviors={list((profile.get('behaviors') or {}).keys())}, "
        f"profile keys={list((profile.get('profile') or {}).keys()) or '无'}")
  return profile


def wait_and_dump_llm_result(client, uid: str, timeout: int) -> bool:
  """--with-llm：轮询 query_profile 等后台 LLM（sleep_insight + analysis_reports）落库，
  打印摘要并把完整内容写到 out_analysis/<uid>_<ts>/llm_insight.json。

  服务端两个节流点会导致"等了但没有新内容"（属预期复用，不是失败）：
    - LLM_UPDATE_COOLDOWN_SECONDS=300：同一 uid 5 分钟内只跑一次后台 LLM
    - sleep_insight 7 天新鲜期 / analysis_reports 同周期复用：已有未过期内容不会再调 LLM
  想强制重新生成：删掉该 uid 的画像（测试服务器删 /tmp/test_insight_e2e_profiles.txt）再灌。
  """
  print(f"\n[llm] 等待后台 LLM 生成 sleep_insight + analysis_reports（超时 {timeout}s，每 10s 轮询）...")
  t0 = time.time()
  while time.time() - t0 < timeout:
    time.sleep(10)
    resp = client.query_profile()
    prof = ((resp.get("response") or {}).get("data") or {}).get("user_profile") or {}
    insight = prof.get("sleep_insight")
    reports = prof.get("analysis_reports") or {}
    ready = bool(insight and (insight.get("greeting") or {}).get("title")) and any(reports.values())
    elapsed = int(time.time() - t0)
    if not ready:
      counts = {k: len(v) for k, v in reports.items() if v}
      print(f"[llm] 等待中... ({elapsed}s) insight={'有' if insight else '无'} reports={counts}")
      continue

    print(f"[llm] {elapsed}s 后落库完成:")
    for key in INSIGHT_MODULE_KEYS:
      m = (insight.get(key) or {})
      print(f"  模块{m.get('module_id')} [{key}] {m.get('title')!r} visible={m.get('visible', True)}")
    print(f"  sleep_insight: date={insight.get('date')} generated_at={insight.get('generated_at')} llm_used={insight.get('llm_used')}")
    for rt, rs in reports.items():
      if rs:
        print(f"  analysis_reports[{rt}]: {len(rs)} 条, 最新 modules={list((rs[-1].get('modules') or {}).keys())}")

    out_dir = Path("out_analysis") / f"{uid}_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "llm_insight.json"
    out_file.write_text(json.dumps(
      {"sleep_insight": insight, "analysis_reports": reports,
       "sleep_scenarios_reco": prof.get("sleep_scenarios_reco"),
       "standard_sop_reco": prof.get("standard_sop_reco")},
      ensure_ascii=False, indent=2))
    print(f"[llm] 完整内容已写入 {out_file}")
    return True

  print(f"[llm] ❌ 超时 {timeout}s 未落库。常见原因："
        "服务器未带 ARK_API_KEY/KIMI_API_KEY 启动（LLM 未启用）/ "
        "300s 冷却期 / 已有 7 天内新鲜 insight（属复用，不是失败）")
  return False


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--base-url", default=os.getenv("APP_SERVER_URL", "http://127.0.0.1:9001"))
  parser.add_argument("--host", default=None, help="user_server IP；传入后覆盖 --base-url")
  parser.add_argument("--port", type=int, default=9001)
  parser.add_argument("--jwt-token", default=os.getenv("JWT_TOKEN", ""))
  parser.add_argument("--uid", default="mindora_test_uid1")
  parser.add_argument("--from-profile", default=None, metavar="PATH",
                      help="不回放构造数据，改用 out_analysis/<uid>_<ts>/query_profile.json "
                           "作为 update 输入（PATH 可为该 json 或其所在目录）；"
                           "自动剥离 LLM 产物字段以触发重新生成，--days/--with-plays 失效")
  parser.add_argument("--days", type=int, default=7, help="灌几晚（默认 7，≤30）")
  parser.add_argument("--with-plays", action="store_true", help="同时灌场景播放事件")
  parser.add_argument("--health-schema-version", type=int, default=2)
  parser.add_argument("--with-llm", action="store_true",
                      help="显式强制服务端立即跑真实 LLM（skip 开关置 False，无防抖），"
                           "并轮询等结果落库后打印摘要、写 llm_insight.json")
  parser.add_argument("--skip-llm", action="store_true",
                      help="显式要求服务端跳过 LLM（skip 开关置 True）；"
                           "与 --with-llm 都不带时不传开关，走服务端自动决策（模拟 app 现状）")
  parser.add_argument("--llm-timeout", type=int, default=420,
                      help="--with-llm 时等待后台 LLM 落库的超时秒数（默认 420）")
  args = parser.parse_args()

  base_url = f"http://{args.host}:{args.port}" if args.host else args.base_url
  client = UserServerClient(base_url=base_url, jwt_token=args.jwt_token, uid=args.uid)

  if args.from_profile:
    payload = load_profile_from_file(args.from_profile)
    print(f"target: {base_url}  uid={args.uid}  模式=回放真实画像")
  else:
    payload = build_payload(min(args.days, 30), args.with_plays)
    print(f"target: {base_url}  uid={args.uid}  nights={len(payload['sleep_data'])}  plays={args.with_plays}")

  # 三态开关：--with-llm=显式强制(False)，--skip-llm=显式跳过(True)，都不带=不传(服务端自动决策，模拟 app)
  skip_flags = None
  if args.with_llm:
    skip_flags = False
  elif args.skip_llm:
    skip_flags = True

  resp = client.update_profile(
    user_profile=payload,
    skip_sleep_scenarios_reco_update=skip_flags,
    skip_sleep_analysis_update=skip_flags,
    health_schema_version=args.health_schema_version,
  )
  body = resp.get("response") or {}
  print(f"update_profile: code={body.get('code')} msg={body.get('msg')}")
  if body.get("code") != 0:
    print(json.dumps(body, ensure_ascii=False)[:400])
    sys.exit(1)

  merged = ((body.get("data") or {}).get("user_profile")) or {}
  sd = merged.get("sleep_data") or []
  latest = sd[-1] if sd else {}
  print(f"落库确认: sleep_data={len(sd)} 条, "
        f"最新一晚 quality={latest.get('sleep_quality')} hr_range={latest.get('hr_min')}-{latest.get('hr_max')}")
  sa = merged.get("sleep_analysis") or {}
  print(f"most_used_scene_7d={json.dumps(sa.get('most_used_scene_7d'), ensure_ascii=False)}")
  print(f"health_sync_days 条数={len(merged.get('health_sync_days') or {})}")

  if args.with_llm:
    ok = wait_and_dump_llm_result(client, args.uid, args.llm_timeout)
    if not ok:
      sys.exit(2)

  print("\n下一步: python tool/dump_analysis.py --base-url <同地址> 逐屏核对")


if __name__ == "__main__":
  main()
