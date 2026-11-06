"""Fixed query_profile views read only their storage parts and return client fields."""
from types import SimpleNamespace

import plyvel
import pytest

import profile_query_gate
from profile_repository import ProfileRepository
from user_profile import ProfileData, ProfileRequest, UserProfile
from user_server import (
    APP_PROFILE_FIELDS, DEVICE_PROFILE_FIELDS, DEVICE_TOP_FIELDS,
    PROFILE_VIEW_PARTS, UserServer,
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_query_gate, "is_query_allowed", lambda uid: True)
    db = plyvel.DB(str(tmp_path / "profiles"), create_if_missing=True)
    repo = ProfileRepository(SimpleNamespace(storage_mode="leveldb", db=db))
    source = UserProfile.model_validate({
        "revision": 0,
        "profile": {
            "nickname": "A", "phone": "123", "avatar_base64": "x" * 4096,
            "address_list": [{"id": "addr1", "is_default": True, "region": "北京",
                              "detail": "一号", "name": "A", "phone": "+8613800000000"}],
            "religion": "none",
        },
        "basic_info": {"goal": "sleep"},
        "sleep_health": {"stress_type": "work"},
        "sleep_mode": {"start_time": "22:00"},
        "uid_emb": [0.1],
        "long_term_profile": [["quiet", 0.5]],
        "sleep_data": [
            {"timestamp": 1000, "sleep_quality": 70},
            {"timestamp": 2000, "sleep_quality": 80},
        ],
        "behaviors": {"heart_rate": [[1000, 60]]},
    })
    repo.save("u1", source.model_dump())
    reads = []

    class Service:
        def get_profile(self, uid, parts=None):
            reads.append(parts)
            data = repo.read(uid, parts)
            return UserProfile.model_validate(data) if data is not None else None

    srv = UserServer.__new__(UserServer)
    srv._parse_for_uid = lambda data: "u1"
    srv.user_serv = Service()
    yield srv, reads
    db.close()


def _query(server, view, **options):
    req = ProfileRequest(
        request_type="query_profile", timestamp=1,
        data=ProfileData(uid="u1", jwt_token="test", profile_view=view, **options),
    )
    resp = server.handle_query_profile(req)
    assert resp.code == 0
    return resp.data["user_profile"]


def test_app_view_reads_two_parts_and_only_personal_fields(server):
    srv, reads = server
    body = _query(srv, "app")
    assert reads == [PROFILE_VIEW_PARTS["app"]]
    assert set(body) == {"revision", "profile"}
    assert set(body["profile"]) == APP_PROFILE_FIELDS
    assert body["profile"]["avatar_base64"] == "x" * 4096
    assert body["profile"]["nickname"] == "A"


def test_device_view_reads_eight_parts_and_omits_avatar_history(server):
    srv, reads = server
    body = _query(srv, "device", sleep_data_count=30, include_behaviors=True)
    assert reads == [PROFILE_VIEW_PARTS["device"]]
    assert set(body) <= DEVICE_TOP_FIELDS
    assert set(body["profile"]) == DEVICE_PROFILE_FIELDS
    assert "avatar_base64" not in body["profile"]
    assert "address_list" not in body["profile"]
    assert "behaviors" not in body
    assert "sleep_plans" not in body
    assert body["basic_info"] == {"goal": "sleep"}
    assert body["sleep_health"]["stress_type"] == "work"
    assert body["sleep_mode"]["start_time"] == "22:00"
    assert [night["timestamp"] for night in body["sleep_data"]] == [2000]
    assert set(body["analysis_reports"]) == {"analysis_sleep_day"}


def test_all_view_keeps_complete_query_and_legacy_flags(server):
    srv, reads = server
    body = _query(srv, "all", sleep_data_count=2, include_behaviors=True)
    assert reads == [None]
    assert "behaviors" in body
    assert "avatar_base64" in body["profile"]
    assert len(body["sleep_data"]) == 2


def test_omitted_view_keeps_app_and_device_fields(server):
    srv, reads = server
    req = ProfileRequest(request_type="query_profile", timestamp=1,
                         data=ProfileData(uid="u1", jwt_token="test",
                                          sleep_data_count=30, include_behaviors=True))
    body = srv.handle_query_profile(req).data["user_profile"]
    assert reads == [PROFILE_VIEW_PARTS["compat"]]
    assert len(reads[0]) == 8
    assert set(body) <= DEVICE_TOP_FIELDS
    assert set(body["profile"]) == APP_PROFILE_FIELDS | DEVICE_PROFILE_FIELDS
    assert body["profile"]["phone"] == "123"
    assert body["profile"]["avatar_base64"] == "x" * 4096
    assert body["profile"]["address_list"][0]["id"] == "addr1"
    assert body["basic_info"] == {"goal": "sleep"}
    assert len(body["sleep_data"]) == 1
    assert "behaviors" not in body


def test_unknown_view_rejected():
    with pytest.raises(ValueError):
        ProfileData(uid="u1", jwt_token="test", profile_view="internal")
