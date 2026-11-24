import sys
from unittest.mock import Mock

import requests

from tool import user_server_client as cli


def test_query_plans_request():
  client = cli.UserServerClient(jwt_token="test-token", timezone="Asia/Shanghai")
  client._post = Mock(return_value={"ok": True})
  assert client.query_plans(device_id="test-device", last_sync_at=0) == {"ok": True}
  path, payload = client._post.call_args.args
  assert path == "/sleep_plan"
  assert payload["request_type"] == "query_plans"
  assert payload["data"] == {
    "jwt_token": "test-token", "language": cli.DEFAULT_LANGUAGE,
    "timezone": "Asia/Shanghai", "device_id": "test-device", "last_sync_at": 0,
  }


def test_query_plans_debug_auth_and_optional_timestamp():
  client = cli.UserServerClient(uid="debug-user")
  client._post = Mock()
  client.query_plans()
  data = client._post.call_args.args[1]["data"]
  assert data["uid"] == "debug-user"
  assert "jwt_token" not in data
  assert "last_sync_at" not in data
  assert "plans" not in data


def test_query_plans_cli(monkeypatch, capsys):
  query = Mock(return_value={"response": {"data": {"plans": []}}})
  monkeypatch.setattr(cli.UserServerClient, "query_plans", query)
  monkeypatch.setattr(sys, "argv", ["client", "query_plans", "--device-id", "device-a", "--last-sync-at", "123"])
  cli.main()
  query.assert_called_once_with(device_id="device-a", last_sync_at=123)
  assert '"plans": []' in capsys.readouterr().out


def test_print_result_redacts_token_without_mutating_request(capsys):
  result = {"request": {"data": {"jwt_token": "secret-token"}}}
  cli.print_result("query_plans", result)
  assert "secret-token" not in capsys.readouterr().out
  assert result["request"]["data"]["jwt_token"] == "secret-token"


def test_query_user_rights_request():
  client = cli.UserServerClient(base_url='http://auth-server:9103', jwt_token='test-token')
  client._post = Mock(return_value={'ok': True})
  assert client.query_user_rights() == {'ok': True}
  path, payload = client._post.call_args.args
  assert path == '/auth'
  assert payload['request_type'] == 'query_user_rights'
  assert payload['data'] == {'jwt_token': 'test-token'}


def test_query_user_rights_requires_token():
  import pytest
  client = cli.UserServerClient(jwt_token='')
  client._post = Mock()
  with pytest.raises(ValueError, match='requires'):
    client.query_user_rights()
  client._post.assert_not_called()


def test_query_user_rights_cli(monkeypatch, capsys):
  query = Mock(return_value={'response': {'data': {'effective_user_level': 'premium'}}})
  monkeypatch.setattr(cli.UserServerClient, 'query_user_rights', query)
  monkeypatch.setattr(sys, 'argv', ['client', 'query_user_rights', '--jwt-token', 'test-token'])
  cli.main()
  query.assert_called_once_with()
  assert 'premium' in capsys.readouterr().out


def test_query_profile_sop_flags():
  client = cli.UserServerClient(uid="test-user")
  client._post = Mock()
  client.query_profile()
  data = client._post.call_args.args[1]["data"]
  assert data["include_sop_tag_profile"] is False
  assert data["include_sop_recommendation_details"] is False
  client.query_profile(include_sop_tag_profile=True, include_sop_recommendation_details=True)
  data = client._post.call_args.args[1]["data"]
  assert data["include_sop_tag_profile"] is True
  assert data["include_sop_recommendation_details"] is True


def test_query_profile_sop_cli(monkeypatch, capsys):
  query = Mock(return_value={"response": {"data": {}}})
  monkeypatch.setattr(cli.UserServerClient, "query_profile", query)
  monkeypatch.setattr(sys, "argv", ["client", "query_profile", "--include-sop-tag-profile",
                                    "--include-sop-recommendation-details"])
  cli.main()
  query.assert_called_once_with(device_type="pc", include_sop_tag_profile=True,
                               include_sop_recommendation_details=True)


def test_connect_timeout_retries_without_repeating_read_timeout(monkeypatch):
  client = cli.UserServerClient(connect_timeout=2, connect_retries=2)
  response = Mock(status_code=200, ok=True)
  response.json.return_value = {"code": 0, "data": {}}
  client.session.post = Mock(side_effect=[requests.ConnectTimeout("connect timed out"), response])
  monkeypatch.setattr(cli.time, "sleep", lambda _: None)
  assert client._post("/analysis", {"request_type": "analysis_overview"})["ok"] is True
  assert client.session.post.call_count == 2
  assert client.session.post.call_args.kwargs["timeout"] == (2, 30)

  client.session.post = Mock(side_effect=requests.ReadTimeout("response timed out"))
  assert client._post("/analysis", {})["ok"] is False
  client.session.post.assert_called_once()
