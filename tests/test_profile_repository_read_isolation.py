"""A caller cannot mutate a later LevelDB profile read through an earlier result."""
from types import SimpleNamespace

import plyvel

from profile_repository import ProfileRepository


def test_split_read_returns_independent_nested_values(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        original = {
            "profile": {"nickname": "original", "avatar_base64": "image"},
            "basic_info": {"goal": "sleep"},
            "sleep_data": [{"timestamp": 1000, "sleep_status": [{"sleep_type": "deep"}]}],
        }
        repo.save("u1", original)
        first = repo.read("u1", {"profile:details", "sleep:records"})
        first["profile"]["nickname"] = "modified"
        first["basic_info"]["goal"] = "changed"
        first["sleep_data"][0]["sleep_status"][0]["sleep_type"] = "awake"

        second = repo.read("u1", {"profile:details", "sleep:records"})
        assert second["profile"]["nickname"] == "original"
        assert second["basic_info"]["goal"] == "sleep"
        assert second["sleep_data"][0]["sleep_status"][0]["sleep_type"] == "deep"
    finally:
        db.close()
