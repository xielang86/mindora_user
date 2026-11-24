#!/usr/bin/env python3
"""存量画像 sleep_data 回填：从 behaviors 合成每晚 SleepResult + 配对当夜心率区间。

背景：iOS 健康同步只传 behaviors、从不上报 sleep_data，分析链路全读 sleep_data。
profile_service._synthesize_sleep_data 会在每次 update_profile 时自动合成，本脚本
用于给"近期不会打开 app"的存量用户一次性补齐（source="healthkit"，device 上报行
优先，同晚重算覆盖，幂等）。

用法（仓库根目录）：
  python tool/backfill_sleep_data.py            # dry-run：只打印会改什么，不写盘
  python tool/backfill_sleep_data.py --apply    # 实际写回

注意：
  - leveldb 模式（默认） plyvel 独占锁：先停 user_server 再跑 --apply；
  - 建议先备份数据目录（Config.USER_PROFILE_DB_PATH / json_path）再 --apply；
  - 幂等：可随时重跑。
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 与 user_server.py 入口一致：加载 .env（systemd EnvironmentFile 的变量已.environ
# 中，load_dotenv 默认不覆盖已有环境变量，优先级一致）
load_dotenv()

from profile_service import UserProfileServ  # noqa: E402


def main() -> int:
  parser = argparse.ArgumentParser(description="backfill sleep_data from stored health behaviors")
  parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
  args = parser.parse_args()
  logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

  serv = UserProfileServ()  # 不需要 LLM：回填只走合成基础路径
  changed = 0
  total = 0
  for uid in serv.list_uids():
    profile = serv.get_profile(uid)
    if profile is None:
      continue
    total += 1
    before = profile.model_dump_json()
    stats = serv.backfill_sleep_data_inplace(profile)
    if profile.model_dump_json() == before:
      continue
    changed += 1
    print(f"{uid}: synthesized={stats['synthesized']} sleep_data={len(profile.sleep_data)}")
    if args.apply:
      serv.save_profile(uid, profile)

  mode = "APPLIED" if args.apply else "DRY-RUN (use --apply to write)"
  print(f"{mode}: {changed}/{total} profiles changed")
  return 0


if __name__ == "__main__":
  sys.exit(main())
