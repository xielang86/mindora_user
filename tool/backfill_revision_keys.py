"""Backfill compact revision keys for profiles written before the split.

Run while user_server is stopped because LevelDB permits only one process to
open a database directory:

  python tool/backfill_revision_keys.py [--db data/userprofile_level_db]
"""
import argparse
import json
from pathlib import Path
from urllib.parse import quote

import plyvel


PREFIX = b"_meta:revision:"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/userprofile_level_db")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    db = plyvel.DB(str(db_path), create_if_missing=False)
    scanned = written = invalid = 0
    try:
        batch = db.write_batch(transaction=True)
        for key, raw in db.iterator():
            if key.startswith((b"_meta:", b"p2:")):
                continue
            scanned += 1
            try:
                meta_raw = db.get(("p2:" + quote(key.decode(), safe="") + ":meta").encode())
                source = json.loads(meta_raw) if meta_raw is not None else json.loads(raw)
                revision = int(source.get("commit_revision", source.get("revision")) or 0)
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                invalid += 1
                continue
            batch.put(PREFIX + key, str(revision).encode("ascii"))
            written += 1
            if written % 1000 == 0:
                batch.write()
                batch = db.write_batch(transaction=True)
        batch.write()
    finally:
        db.close()
    print(f"db={db_path} scanned={scanned} written={written} invalid={invalid}")


if __name__ == "__main__":
    main()
