"""Offline profile migration to the current schema (v6). Stop user_server and back up the DB first.

  python3 tool/migrate_profile_v2.py --db /path/to/db --mode inspect
  python3 tool/migrate_profile_v2.py --db /path/to/db --mode migrate
  python3 tool/migrate_profile_v2.py --db /path/to/db --mode verify

Migration accepts single-key legacy or split v2/v3/v4/v5 profiles, preserving values and
business revisions. Once v6 is written, v6 is authoritative; rebuild-legacy exports its latest state for a
rollback, instead of silently reverting to the stale original blob.

rollback-v4 rewrites a stopped v6 DB into the schema v4 layout without changing
the current business revision. Use it before starting an older v4 server.
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from profile_repository import (  # noqa: E402
    ProfileRepository, V4_PARTS, V5_PARTS, PARTS, assemble, decompose, decompose_v4,
    encoded_uid, key,
)


def run(repository, uid, mode):
    if mode in {"inspect", "migrate"}:
        return repository.migrate(uid, dry_run=mode == "inspect")
    current = repository.read(uid)
    if current is None:
        return "not_found"
    with repository.snapshot() as reader:
        meta = repository._meta(reader, uid)
        legacy = repository._get(reader, uid.encode())
    if meta is None:
        raise ValueError("user has not been migrated")
    if mode == "rebuild-legacy":
        repository._batch({uid.encode(): current})
        return "legacy_rebuilt"
    if mode == "rollback-v4":
        if meta["schema_version"] != repository.SCHEMA:
            return "already_older_schema"
        parts, revision = decompose_v4(current)
        if assemble(parts, revision) != current:
            raise ValueError("v4 rollback round-trip mismatch")
        v4_meta = {
            "schema_version": 4, "migration_complete": True,
            "commit_revision": revision,
            "part_revisions": {part: revision for part in V4_PARTS},
        }
        values = {key(uid, part): value for part, value in parts.items()}
        values[key(uid, "meta")] = v4_meta
        repository._batch(values, deletions=[key(uid, part) for part in PARTS - V4_PARTS])
        return "rolled_back_v4"
    if meta["schema_version"] == repository.SCHEMA:
        parts, revision = decompose(current)
        if assemble(parts, revision) != current:
            raise ValueError("v6 profile round-trip mismatch")
        with repository.snapshot() as reader:
            for part in PARTS:
                if repository._get(reader, key(uid, part)) is None:
                    raise ValueError(f"missing v6 profile part: {part}")
            for old_part in (V4_PARTS | V5_PARTS) - PARTS | {"analysis:advice", "analysis:reports", "settings:sleep"}:
                old_key = f"p2:{encoded_uid(uid)}:{old_part}".encode()
                if repository._get(reader, old_key) is not None:
                    raise ValueError(f"stale profile part: {old_part}")
    if legacy is not None and int(legacy.get("revision") or 0) == current["revision"]:
        if {**legacy, "revision": current["revision"]} != current:
            raise ValueError("legacy/v2 mismatch at same revision")
        return "verified_equal"
    # Legacy is deliberately not mirrored on split-layout commits.
    return "verified_v6_legacy_stale" if meta["schema_version"] == repository.SCHEMA else "verified_legacy_stale"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=["inspect", "migrate", "verify", "rebuild-legacy", "rollback-v4"], default="inspect")
    parser.add_argument("--uid", action="append", help="Repeat to select users; otherwise all users")
    parser.add_argument("--limit", type=int, help="Process at most this many users; rerun skips completed migrations")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    import plyvel
    try:
        db = plyvel.DB(str(Path(args.db).resolve()), create_if_missing=False)
    except Exception as exc:
        parser.exit(2, f"Cannot open DB ({type(exc).__name__}). Stop user_server and verify the path.\n")
    counts = {}
    errors = 0
    try:
        repository = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        users = args.uid or repository.list_uids()
        processed = 0
        for uid in users:
            if args.mode == "migrate" and args.limit is not None and not args.uid:
                raw_meta = db.get(key(uid, "meta"))
                if raw_meta is not None and json.loads(raw_meta).get("schema_version") == repository.SCHEMA:
                    continue
            if args.limit is not None and processed >= args.limit:
                break
            processed += 1
            try:
                status = run(repository, uid, args.mode)
                counts[status] = counts.get(status, 0) + 1
            except Exception as exc:
                errors += 1
                # No raw profiles/tokens/answers in logs.
                print(json.dumps({"uid": uid, "error": type(exc).__name__}), file=sys.stderr)
        print(json.dumps({"mode": args.mode, "counts": counts, "errors": errors}))
    finally:
        db.close()
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
