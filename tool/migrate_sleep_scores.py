#!/usr/bin/env python3
"""Backfill versioned sleep scores on a stopped-service or copied profile DB.

Default is a dry run. Use --apply only after backing up the actual service DB;
--verify rereads and recomputes every profile without writing.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from profile_service import UserProfileServ  # noqa: E402
from sleep_score_cache import refresh_score_cache, score_cache_ready  # noqa: E402


def main() -> int:
  parser = argparse.ArgumentParser(description="backfill AN_SCORE and three nightly insight indices")
  mode = parser.add_mutually_exclusive_group()
  mode.add_argument("--apply", action="store_true", help="write changed profiles")
  mode.add_argument("--verify", action="store_true", help="read back and check all cached scores")
  parser.add_argument("--limit", type=int, default=0, help="limit profiles for a trial run")
  args = parser.parse_args()
  serv = UserProfileServ()
  total = changed = errors = 0
  for uid in serv.list_uids():
    if args.limit and total >= args.limit:
      break
    profile = serv.get_profile(uid)
    if profile is None:
      continue
    total += 1
    if args.verify:
      if not score_cache_ready(profile):
        errors += 1
        print(f"{uid}: missing or old score version")
        continue
      expected = profile.model_copy(deep=True)
      if refresh_score_cache(expected):
        errors += 1
        print(f"{uid}: cached scores differ from current formula")
      continue
    if refresh_score_cache(profile):
      changed += 1
      print(f"{uid}: {len(profile.sleep_data)} nights refreshed")
      if args.apply:
        serv.save_profile(uid, profile)
  label = "VERIFY" if args.verify else "APPLIED" if args.apply else "DRY-RUN"
  print(f"{label}: profiles={total} changed={changed} errors={errors}")
  return 1 if errors else 0


if __name__ == "__main__":
  raise SystemExit(main())
