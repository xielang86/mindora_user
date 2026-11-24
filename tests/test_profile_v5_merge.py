"""Merged profile keys preserve unrelated fields and the analysis plan projection."""
import json
import time
import types
from types import SimpleNamespace

import plyvel
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import profile_service
from user_profile import Profile, UserProfile
import analysis_builders
from analysis_content import explore_input_fingerprint
from analysis_score import daily_score
from user_server import UserServer

from profile_repository import (
    ANALYSIS_PARTS_BY_TYPE, DEVICE_PARTS, ProfileRepository, V4_PARTS, V5_PARTS,
    decompose_v4, encode, key, split_profile_v5,
)


def test_v4_partial_write_upgrades_and_preserves_merged_fields(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {
            "revision": 7,
            "profile": {"nickname": "Old", "avatar_base64": "image"},
            "sleep_data": [],
            "behaviors": {"plays": [[1, {"cmd": "scene"}]], "sleep_in_bed": [[2, 3]]},
            "mindora_record": {"scene": [[3, 60]]},
            "sleep_analysis": {"insight_memory": {"theme": "a"}, "most_used_scene": "scene"},
            "analysis_reports": {"analysis_sleep_day": []},
            "sleep_insight": {"date": "2026-11-08"},
            "uid_emb": [0.1],
            "sleep_scenarios_reco": ["scene"],
            "future_field": {"keep": True},
        }
        parts, revision = decompose_v4(original)
        for part in V4_PARTS:
            db.put(key("u1", part), encode(parts[part]))
        db.put(key("u1", "meta"), encode({
            "schema_version": 4, "migration_complete": True,
            "commit_revision": revision,
            "part_revisions": {part: revision for part in V4_PARTS},
        }))
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        assert repo.read("u1") == original

        signals = repo.read("u1", {"analysis:context"})
        signals["behaviors"]["plays"].append([4, {"cmd": "new"}])
        assert repo.save("u1", signals, {"analysis:context"}) == 8
        expected = {**original, "revision": 8, "behaviors": {
            "plays": original["behaviors"]["plays"] + [[4, {"cmd": "new"}]],
            "sleep_in_bed": original["behaviors"]["sleep_in_bed"],
        }}
        assert repo.read("u1") == expected
        assert json.loads(db.get(key("u1", "meta")))["schema_version"] == 6
        for part in {"internal:health", "internal:interaction", "internal:scene_history",
                     "analysis:content", "analysis:insight", "analysis:memory",
                     "internal:preferences", "scene:summary", "recommendation:current",
                     "analysis:signals", "request_meta"}:
            assert db.get(key("u1", part)) is None

        details = repo.read("u1", {"profile:details"})
        details["profile"]["nickname"] = "New"
        assert repo.save("u1", details, {"profile:details"}) == 9
        assert repo.read("u1")["sleep_analysis"] == original["sleep_analysis"]
        assert repo.read("u1")["uid_emb"] == original["uid_emb"]
        assert repo.read("u1")["mindora_record"] == original["mindora_record"]
    finally:
        db.close()


def test_analysis_and_device_read_part_counts():
    assert {name: len(parts) for name, parts in ANALYSIS_PARTS_BY_TYPE.items()} == {
        "analysis_overview": 4,
        "analysis_sleep_day": 3,
        "analysis_sleep_week": 3,
        "analysis_sleep_month": 3,
        "analysis_explore": 4,
    }
    assert len(DEVICE_PARTS) == 4


def test_plan_sync_projection_keeps_full_plan_and_partial_analysis_read(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        full = {"sleep_plan": None, "sleep_plans": [
            {"plan_id": "active", "status": "active", "target_minutes": 480,
             "updated_at": 101, "custom_name": "Private label",
             "activated_at": 100},
            {"plan_id": "deleted", "status": "deleted", "deleted": True,
             "updated_at": 102,
             "target_minutes": 600},
        ], "last_request_timezone": "Asia/Shanghai"}
        repo.save("u1", full)
        analysis = repo.read("u1", {"analysis:context"})
        assert [p["plan_id"] for p in analysis["sleep_plans"]] == ["active"]
        assert "custom_name" not in analysis["sleep_plans"][0]
        assert repo.read("u1")["sleep_plans"] == full["sleep_plans"]
        changed = repo.read("u1", {"analysis:context", "plans:state"})
        changed["sleep_plans"][0]["target_minutes"] = 540
        repo.save("u1", changed, {"analysis:context", "plans:state"})
        assert repo.read("u1", {"analysis:context"})["sleep_plans"][0]["target_minutes"] == 540
        assert repo.read("u1")["sleep_plans"][1] == full["sleep_plans"][1]
    finally:
        db.close()


def test_v5_to_v6_migration_preserves_full_profile_and_revision(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        original = {"revision": 12, "sleep_data": [], "last_request_language": "zh-Hans",
                    "sleep_plans": [{"plan_id": "active", "status": "active",
                                     "target_minutes": 480, "updated_at": 100,
                                     "activated_at": 90}],
                    "analysis_reports": {"analysis_sleep_day": []}}
        parts, revision = split_profile_v5(original)
        for part in V5_PARTS:
            db.put(key("u1", part), encode(parts[part]))
        db.put(key("u1", "meta"), encode({"schema_version": 5,
                                          "migration_complete": True,
                                          "commit_revision": revision,
                                          "part_revisions": {part: revision for part in V5_PARTS}}))
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        assert repo.read("u1") == original
        assert repo.migrate("u1") == "migrated"
        assert repo.read("u1") == original
        assert json.loads(db.get(key("u1", "meta")))["schema_version"] == 6
        assert db.get(key("u1", "analysis:signals")) is None
        assert db.get(key("u1", "request_meta")) is None
    finally:
        db.close()


def test_analysis_plan_projection_preserves_score_and_report_fingerprint(tmp_path):
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    try:
        repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
        repo.save("u1", {
            "sleep_plans": [{"plan_id": "active", "kind": "custom", "status": "active",
                             "target_minutes": 540, "sleep_time": 1380, "wake_time": 420,
                             "updated_at": 101, "activated_at": 100}],
            "sleep_data": [{"timestamp": 30000, "first_sleep_time": "23:00",
                            "wake_time": "07:00", "sleep_status": [
                                {"start_time": 1000, "duration": 240, "sleep_type": "core"},
                                {"start_time": 15400, "duration": 120, "sleep_type": "deep"},
                                {"start_time": 22600, "duration": 120, "sleep_type": "rem"},
                            ], "in_bed_intervals": [[700, 29800]]}],
            "last_request_timezone": "Asia/Shanghai", "last_request_language": "zh-Hans",
        })
        full = UserProfile.model_validate(repo.read("u1"))
        partial = UserProfile.model_validate(repo.read("u1", ANALYSIS_PARTS_BY_TYPE["analysis_overview"]))
        assert daily_score(full.sleep_data[0], full) == daily_score(partial.sleep_data[0], partial)
        assert explore_input_fingerprint(full, "zh-Hans") == explore_input_fingerprint(partial, "zh-Hans")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_sleep_plan_sync_updates_analysis_context_atomically(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(profile_service, "run_dir", str(tmp_path))
    service = profile_service.UserProfileServ(llm=SimpleNamespace(enabled=False))
    service.repository.save("u1", UserProfile().model_dump(mode="json"))

    class Server:
        user_serv = service
        _parse_for_uid = staticmethod(lambda data: "u1")

        async def _get_effective_level(self, uid, token):
            return "premium"

        async def _run_update(self, func, *args):
            return func(*args)

    server = Server()
    server.handle_sleep_plan_http = types.MethodType(UserServer.handle_sleep_plan_http, server)
    app = web.Application()
    app.router.add_post("/sleep_plan", server.handle_sleep_plan_http)
    now = int(time.time())
    plan = {
        "plan_id": "PLAN-0001", "kind": "custom", "custom_name": "Sleep",
        "target_minutes": 540, "sleep_time": 1350, "wake_time": 390,
        "cycle": {"type": "weekdays", "weekdays": [1, 2, 3, 4, 5], "repeats_weekly": True},
        "reminder_enabled": True, "reminder_minutes_before": 30,
        "alarm_sound_id": "sunlit_coast", "sleep_aid_category": "smart",
        "status": "active", "timezone": "Asia/Shanghai",
        "created_at": now - 100, "updated_at": now, "activated_at": now - 10,
        "deleted": False,
    }
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/sleep_plan", json={
                "request_type": "sync_plans", "timestamp": now, "version": "1.0",
                "data": {"uid": "u1", "language": "zh-Hans", "timezone": "Asia/Shanghai",
                         "device_id": "dev-1", "plans": [plan]},
            })
            assert response.status == 200, await response.text()
            assert (await response.json())["code"] == 0
        full = service.get_profile("u1")
        analysis = service.get_profile("u1", ANALYSIS_PARTS_BY_TYPE["analysis_sleep_day"])
        assert full.sleep_plans[0].target_minutes == 540
        assert analysis.sleep_plans[0].target_minutes == 540
        assert analysis.last_request_timezone == "Asia/Shanghai"
    finally:
        service.close()


def test_zero_data_fallback_with_narrow_analysis_reads(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(profile_service, "run_dir", str(tmp_path))
    service = profile_service.UserProfileServ(llm=SimpleNamespace(enabled=False))
    try:
        seed = UserProfile(profile=Profile(nickname="New user", language="en"), uid_emb=[])
        service.repository.save("u1", seed.model_dump(mode="json"))
        for request_type, builder_name in (
            ("analysis_overview", "build_overview"),
            ("analysis_sleep_day", "build_sleep_day"),
            ("analysis_sleep_week", "build_sleep_week"),
            ("analysis_sleep_month", "build_sleep_month"),
            ("analysis_explore", "build_explore"),
        ):
            profile = service.get_profile_and_note_request_meta(
                "u1", "Asia/Shanghai", "en", request_type=request_type,
            )
            assert profile is not None and not profile.sleep_data
            profile = service.ensure_fallback_content_for_uid("u1")
            request = SimpleNamespace(
                date="2026-11-08", start_date="2026-11-01", end_date="2026-11-08",
                timezone="Asia/Shanghai", language="en", modules=[],
            )
            result = getattr(analysis_builders, builder_name)(request, profile)
            assert isinstance(result, dict)
        assert service.get_profile("u1").sleep_insight is not None
        assert service.get_profile("u1").analysis_reports
    finally:
        service.close()
