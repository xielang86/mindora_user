"""Old single-key profiles remain readable through the v2 transition."""
import json
from types import SimpleNamespace

import plyvel

from profile_repository import ProfileRepository, V2_PARTS, V3_PARTS, decompose_v4, encode, key
from tool.migrate_profile_v2 import run


def test_legacy_read_migrate_and_partial_update_preserve_data(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {
            "revision": 7,
            "profile": {"nickname": "old", "avatar_base64": "image", "future_field": 9},
            "behaviors": {"plays": [1], "steps": [2]},
            "sleep_data": [],
            "future_top": {"x": 1},
        }
        legacy_bytes = json.dumps(original).encode()
        db.put(b"u1", legacy_bytes)
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))

        assert repo.read("u1") == original
        assert run(repo, "u1", "inspect") == "would_migrate"
        assert db.get(key("u1", "meta")) is None

        assert run(repo, "u1", "migrate") == "migrated"
        assert repo.read("u1") == original
        assert run(repo, "u1", "verify") == "verified_equal"

        details = repo.read("u1", parts={"profile:details"})
        details["profile"]["nickname"] = "new"
        assert repo.save("u1", details, parts={"profile:details"}) == 8
        expected = {**original, "revision": 8,
                    "profile": {**original["profile"], "nickname": "new"}}
        assert repo.read("u1") == expected
        assert db.get(b"u1") == legacy_bytes
        assert run(repo, "u1", "verify") == "verified_v6_legacy_stale"
    finally:
        db.close()


def _put_v2(db, uid, original):
    parts, revision = decompose_v4(original)
    content = parts.pop("analysis:content")
    parts["analysis:advice"] = {k: v for k, v in content.items() if k == "bedtime_advice"}
    parts["analysis:reports"] = {k: v for k, v in content.items() if k == "analysis_reports"}
    details = parts["profile:details"]
    parts["internal:preferences"]["basic_info"] = details.pop("basic_info")
    parts["settings:sleep"] = {
        field: details.pop(field) for field in ("sleep_health", "sleep_mode")
    }
    for part in V2_PARTS:
        db.put(f"p2:{uid}:{part}".encode(), encode(parts[part]))
    db.put(key(uid, "meta"), encode({
        "schema_version": 2, "migration_complete": True,
        "commit_revision": revision,
        "part_revisions": {part: revision for part in V2_PARTS},
    }))


def test_v3_analysis_parts_merge_without_revision_change(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {"revision": 9, "bedtime_advice": {"content": "rest", "generated_at": 1},
                    "analysis_reports": {"analysis_sleep_day": [{"date": "2026-11-01", "modules": {}}]},
                    "sleep_insight": {"date": "2026-11-01"}, "future_top": {"keep": True}}
        parts, revision = decompose_v4(original)
        content = parts.pop("analysis:content")
        parts["analysis:advice"] = {"bedtime_advice": content["bedtime_advice"]}
        parts["analysis:reports"] = {"analysis_reports": content["analysis_reports"]}
        for part in V3_PARTS:
            db.put(f"p2:u1:{part}".encode(), encode(parts[part]))
        db.put(key("u1", "meta"), encode({"schema_version": 3, "migration_complete": True,
                                          "commit_revision": revision,
                                          "part_revisions": {part: revision for part in V3_PARTS}}))
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        assert repo.read("u1") == original
        assert repo.read("u1", {"analysis:text"}) == {
            "revision": 9, "bedtime_advice": original["bedtime_advice"],
            "analysis_reports": original["analysis_reports"],
            "sleep_insight": original["sleep_insight"]}
        assert run(repo, "u1", "inspect") == "would_migrate"
        assert run(repo, "u1", "migrate") == "migrated"
        assert repo.read("u1") == original
        assert json.loads(db.get(key("u1", "meta")))["schema_version"] == 6
        assert json.loads(db.get(key("u1", "analysis:text")))["analysis_reports"] == original["analysis_reports"]
        assert db.get(b"p2:u1:analysis:advice") is None
        assert db.get(b"p2:u1:analysis:reports") is None
    finally:
        db.close()


def test_v2_fields_move_into_details_without_revision_change(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {
            "revision": 7, "uid_emb": [0.1], "long_term_profile": [],
            "basic_info": {"goal": "sleep"},
            "sleep_health": {"improvement_goal": "fall asleep"},
            "sleep_mode": {"start_time": "22:00"},
            "profile": {"nickname": "old", "avatar_base64": "image"},
            "future_top": 9,
        }
        _put_v2(db, "u1", original)
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        assert repo.read("u1") == original
        details = repo.read("u1", parts={"profile:details"})
        assert details["basic_info"] == original["basic_info"]
        assert details["sleep_health"] == original["sleep_health"]
        assert details["sleep_mode"] == original["sleep_mode"]
        assert run(repo, "u1", "inspect") == "would_migrate"
        assert run(repo, "u1", "migrate") == "migrated"
        assert repo.read("u1") == original
        assert json.loads(db.get(key("u1", "meta")))["schema_version"] == 6
        assert db.get(b"p2:u1:settings:sleep") is None
        assert json.loads(db.get(key("u1", "profile:details"))) == {
            "basic_info": original["basic_info"],
            "sleep_health": original["sleep_health"],
            "sleep_mode": original["sleep_mode"],
            "profile": {"nickname": "old"},
        }
        assert run(repo, "u1", "verify") == "verified_v6_legacy_stale"
    finally:
        db.close()


def test_v2_null_profile_keeps_basic_info_during_migration(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {
            "revision": 3,
            "profile": None,
            "basic_info": {"goal": "sleep"},
            "sleep_health": {"stress_type": "high"},
            "sleep_mode": {"start_time": "22:00"},
        }
        _put_v2(db, "u1", original)
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        assert repo.migrate("u1", dry_run=True) == "would_migrate"
        assert repo.migrate("u1") == "migrated"
        assert repo.read("u1") == original
    finally:
        db.close()


def test_first_write_upgrades_v2_and_preserves_unloaded_parts(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {
            "revision": 4, "uid_emb": [0.1], "long_term_profile": [],
            "basic_info": {"goal": "sleep"},
            "sleep_health": {"improvement_goal": "fall asleep"},
            "sleep_mode": {"start_time": "22:00"},
            "profile": {"nickname": "old", "avatar_base64": "image"},
            "future_top": 9,
        }
        _put_v2(db, "u1", original)
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        details = repo.read("u1", parts={"profile:details"})
        details["profile"]["nickname"] = "new"
        assert repo.save("u1", details, parts={"profile:details"}) == 5
        assert repo.read("u1") == {
            **original, "revision": 5,
            "profile": {**original["profile"], "nickname": "new"},
        }
        assert db.get(b"p2:u1:settings:sleep") is None
    finally:
        db.close()
