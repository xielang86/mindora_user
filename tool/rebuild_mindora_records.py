#!/usr/bin/env python3
"""Rebuild mindora_record from retained behaviors.plays and sleep stages.

Default is a dry run. For --apply, stop user_server and back up its actual
RUN_DIR/data/userprofile_level_db first. Old plays outside the retained behavior
window cannot be recovered; this intentionally removes old mindora_record rows.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from profile_service import UserProfileServ  # noqa: E402
from sleep_score_cache import refresh_score_cache  # noqa: E402
from sop_tag_profile import rebuild_sop_tag_profile  # noqa: E402


def rebuilt_profile(service, profile):
  updated = profile.model_copy(deep=True)
  updated.mindora_record = {}
  service._update_mindora_record(updated)
  service._update_scene_stats(updated)
  refresh_score_cache(updated)
  service._update_best_scene_by_sleep_quality(updated)
  rebuild_sop_tag_profile(updated)
  return updated


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  targets = parser.add_mutually_exclusive_group(required=True)
  targets.add_argument("--uid", action="append", help="one UID; repeat for several")
  targets.add_argument("--all", action="store_true", help="all stored UIDs")
  mode = parser.add_mutually_exclusive_group()
  mode.add_argument("--apply", action="store_true", help="replace derived records and save")
  mode.add_argument("--verify", action="store_true", help="fail if stored records differ from rebuilt records")
  parser.add_argument("--limit", type=int, default=0, help="limit profiles for a trial run")
  args = parser.parse_args()

  service = UserProfileServ()
  uids = service.list_uids() if args.all else list(dict.fromkeys(args.uid))
  checked = changed = errors = 0
  for uid in uids:
    if args.limit and checked >= args.limit:
      break
    profile = service.get_profile(uid)
    if profile is None:
      errors += 1
      print(f"{uid}: not found")
      continue
    checked += 1
    updated = rebuilt_profile(service, profile)
    old_count = sum(len(rows) for rows in profile.mindora_record.values())
    new_count = sum(len(rows) for rows in updated.mindora_record.values())
    existing_nonempty = {cmd: rows for cmd, rows in profile.mindora_record.items() if rows}
    differs = updated.mindora_record != existing_nonempty
    if differs:
      changed += 1
      print(f"{uid}: mindora_record {old_count} -> {new_count}")
      if args.apply:
        service.save_profile(uid, updated)
      elif args.verify:
        errors += 1
  label = "APPLIED" if args.apply else "VERIFY" if args.verify else "DRY-RUN"
  print(f"{label}: profiles={checked} changed={changed} errors={errors}")
  return 1 if errors else 0


if __name__ == "__main__":
  raise SystemExit(main())
