import iot_server
from tool.iot_local_user_server_client import build_iot_local_base_url
from tool.user_server_client import UserServerClient

LOCAL_NAME = iot_server.MDNS_LOCAL_NAME


def test_iot_server_exports_user_server_local_name(monkeypatch):
  monkeypatch.setattr(iot_server, "get_lan_ip", lambda: "192.168.1.8")

  api_info = iot_server.build_user_server_info()["user_server"]

  assert api_info["base_url"] == "http://192.168.1.8:9001"
  assert api_info["local_name"] == LOCAL_NAME
  assert api_info["local_base_url"] == f"http://{LOCAL_NAME}:9001"
  assert api_info["local_routes"]["user_profile"] == f"http://{LOCAL_NAME}:9001/user_profile"


def test_user_server_client_calls_api_by_iot_exported_local_name(monkeypatch):
  captured = {}

  class FakeResponse:
    status_code = 200
    ok = True
    text = '{"code":0,"msg":"succ"}'

    def json(self):
      return {"code": 0, "msg": "succ"}

  def fake_post(self, url, json, timeout):
    captured["url"] = url
    captured["json"] = json
    captured["timeout"] = timeout
    return FakeResponse()

  monkeypatch.setattr(iot_server, "get_lan_ip", lambda: "192.168.1.8")
  # UserServerClient 走 session.post，要 patch Session 的方法
  monkeypatch.setattr("tool.user_server_client.requests.Session.post", fake_post)

  base_url = iot_server.build_user_server_info()["user_server"]["local_base_url"]
  client = UserServerClient(base_url=base_url, uid="local-name-user", timeout=3)

  result = client.query_profile()

  assert result["ok"] is True
  assert result["url"] == f"http://{LOCAL_NAME}:9001/user_profile"
  assert captured["url"] == f"http://{LOCAL_NAME}:9001/user_profile"
  assert captured["json"]["request_type"] == "query_profile"
  assert captured["json"]["data"]["uid"] == "local-name-user"
  assert captured["timeout"] == 3


def test_example_client_builds_base_url_from_iot_server_name(monkeypatch):
  monkeypatch.delenv("IOT_DEVICE_LOCAL_NAME", raising=False)
  monkeypatch.delenv("IOT_USER_SERVER_PORT", raising=False)

  assert build_iot_local_base_url() == f"http://{LOCAL_NAME}:9001"
  assert build_iot_local_base_url("BedroomHub.local", 9101) == "http://BedroomHub.local:9101"
