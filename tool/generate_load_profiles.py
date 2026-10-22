#!/usr/bin/env python3
"""Generate deterministic, realistic user profiles for local load tests."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
SCENES = ("cocos_island_moonlight", "amalfi_breeze", "kyoto_forest", "ocean_wave")


def _night(rng: random.Random, day: dt.date, index: int) -> dict:
  bedtime = dt.datetime.combine(day, dt.time(22, 30), tzinfo=TZ)
  bedtime += dt.timedelta(minutes=rng.randint(-25, 35))
  onset = rng.randint(8, 28)
  awake1 = rng.randint(3, 12)
  awake2 = rng.randint(0, 8)
  core1, deep, core2, rem = (
    rng.randint(95, 135), rng.randint(55, 90), rng.randint(100, 145), rng.randint(60, 95)
  )
  rows = []
  cursor = int(bedtime.timestamp())

  def add(minutes: int, kind: str) -> None:
    nonlocal cursor
    if minutes > 0:
      rows.append({"start_time": cursor, "duration": minutes, "sleep_type": kind})
      cursor += minutes * 60

  add(onset, "awake")
  add(core1, "core")
  add(awake1, "awake")
  add(deep, "deep")
  add(core2, "core")
  add(awake2, "awake")
  add(rem, "rem")
  wake = dt.datetime.fromtimestamp(cursor, TZ)
  return {
    "timestamp": cursor + rng.randint(10, 25) * 60,
    "first_sleep_time": dt.datetime.fromtimestamp(rows[1]["start_time"], TZ).strftime("%H:%M"),
    "bed_time": bedtime.strftime("%H:%M"),
    "wake_time": wake.strftime("%H:%M"),
    "hr_before_sleep": round(rng.uniform(58, 72), 1),
    "rr_before_sleep": round(rng.uniform(13, 18), 1),
    "hr_min": round(rng.uniform(47, 55), 1),
    "hr_max": round(rng.uniform(68, 82), 1),
    "respiratory_var": round(rng.uniform(0.03, 0.10), 3),
    "sleep_status": rows,
    "in_bed_intervals": [[int(bedtime.timestamp()), cursor]],
    "self_rating": rng.randint(3, 5),
    "source": None,
  }


def build_profile(uid: str, number: int, rng: random.Random, nights: int) -> dict:
  today = dt.datetime.now(TZ).date()
  sleep_data = [_night(rng, today - dt.timedelta(days=offset), offset)
                for offset in range(nights, 0, -1)]
  plays = []
  for offset in range(nights, 0, -1):
    used_at = dt.datetime.combine(today - dt.timedelta(days=offset), dt.time(22, 5), tzinfo=TZ)
    used_at += dt.timedelta(minutes=rng.randint(-15, 15))
    scene = SCENES[(number + offset) % len(SCENES)]
    plays.append([int(used_at.timestamp()), {
      "cmd": f"sleep.scene.{scene}", "event": "sop_start", "duration": rng.randint(1200, 2400),
    }])
  return {
    "uid": uid,
    "user_profile": {
      "profile": {
        "nickname": f"Load User {number:03d}",
        "gender": "Unknown",
        "age": str(20 + number % 35),
        "email": f"load-user-{number:03d}@example.invalid",
      },
      "sleep_data": sleep_data,
      "behaviors": {
        "plays": plays,
        "clicks": [[int(dt.datetime.now(TZ).timestamp()) - number, {"event": "load_seed"}]],
      },
    },
  }


def main() -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--count", type=int, default=100)
  parser.add_argument("--nights", type=int, default=7)
  parser.add_argument("--seed", type=int, default=316)
  parser.add_argument("--output", default="tool/load_profiles_100.json")
  args = parser.parse_args()
  if args.count <= 0 or args.nights <= 0:
    parser.error("--count and --nights must be positive")
  rng = random.Random(args.seed)
  profiles = [build_profile(f"load_test_user_{i:03d}", i, rng, args.nights)
              for i in range(1, args.count + 1)]
  output = Path(args.output)
  output.parent.mkdir(parents=True, exist_ok=True)
  output.write_text(json.dumps({
    "schema_version": 1,
    "timezone": "Asia/Shanghai",
    "count": len(profiles),
    "profiles": profiles,
  }, ensure_ascii=False, indent=2), encoding="utf-8")
  print(f"wrote {len(profiles)} profiles to {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
  main()
