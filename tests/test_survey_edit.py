"""问卷编辑链路测试（tanchuang_suvey.md 5. query_survey 结构）。

覆盖：
  - ops_config.validate_new_survey / save_survey：结构校验 + 写入配置 + 热加载
  - user_server /ops/save_survey：admin 门禁、成功、校验失败 400
  - ops_admin_server handle_survey_edit_post：转发保存、失败重提页
"""
import json

import pytest

import ops_config as ops_config_module
import ops_admin_server
import user_server
from ops_config import save_survey, validate_new_survey
from popup_config_fixture import SURVEY_ID, write_fixture_config
from user_server import UserServer


@pytest.fixture(autouse=True)
def ops_config(tmp_path, monkeypatch):
  """独立的自包含夹具配置（save_survey 会真实写入该文件）。"""
  dst = tmp_path / "popup_survey_config.json"
  write_fixture_config(dst)
  monkeypatch.setenv("POPUP_SURVEY_CONFIG_PATH", str(dst))
  monkeypatch.setattr(
    ops_config_module, "_ops_config_cache",
    {"path": None, "mtime": None, "popups": [], "surveys": {}},
  )
  return dst


def _choice_q(qid="q1", index=1, title="整体满意度如何？"):
  return {
    "question_id": qid, "index": index, "type": "single_choice", "title": title,
    "options": [{"option_id": "o1", "text": "满意"}, {"option_id": "o2", "text": "不满意"}],
  }


def _survey(**overrides):
  survey = {
    "survey_id": "sv_new_01",
    "i18n": {
      "zh-Hans": {
        "title": "睡眠体验问卷",
        "questions": [
          _choice_q(),
          {"question_id": "q2", "index": 2, "type": "text", "title": "改进意见？", "required": False},
        ],
        "reward": {"gift_type": "virtual", "gift_name": "会员积分"},
      },
    },
  }
  survey.update(overrides)
  return survey


# -------------------- validate_new_survey --------------------

class TestValidateNewSurvey:
  def test_valid_survey_passes(self):
    assert validate_new_survey(_survey(), set()) is None

  def test_multi_language_passes(self):
    survey = _survey()
    survey["i18n"]["en"] = {
      "title": "Sleep Survey",
      "questions": [_choice_q(title="How satisfied are you?")],
    }
    assert validate_new_survey(survey, set()) is None

  def test_duplicate_id_rejected(self):
    err = validate_new_survey(_survey(), {"sv_new_01"})
    assert "已存在" in err

  def test_i18n_required(self):
    assert "i18n" in validate_new_survey({"survey_id": "sv_x"}, set())
    assert "i18n" in validate_new_survey(_survey(i18n={}), set())

  def test_title_and_questions_required(self):
    survey = _survey()
    survey["i18n"]["zh-Hans"]["title"] = ""
    assert "title" in validate_new_survey(survey, set())

    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"] = []
    assert "至少" in validate_new_survey(survey, set())

  def test_bad_question_type_rejected(self):
    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"][0]["type"] = "ranking"
    assert "type" in validate_new_survey(survey, set())

  def test_duplicate_question_and_option_ids_rejected(self):
    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"].append(_choice_q(qid="q1", index=3))
    assert "重复" in validate_new_survey(survey, set())

    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"][0]["options"].append({"option_id": "o1", "text": "x"})
    assert "重复" in validate_new_survey(survey, set())

  def test_choice_needs_two_options(self):
    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"][0]["options"] = [{"option_id": "o1", "text": "唯一"}]
    assert "2 个选项" in validate_new_survey(survey, set())

  def test_text_question_needs_no_options(self):
    survey = _survey()
    survey["i18n"]["zh-Hans"]["questions"] = [
      {"question_id": "q1", "index": 1, "type": "text", "title": "建议？"},
    ]
    assert validate_new_survey(survey, set()) is None

  def test_reward_rules(self):
    # physical 必须有 contact_email + gift_name
    survey = _survey()
    survey["i18n"]["zh-Hans"]["reward"] = {"gift_type": "physical", "gift_name": "好礼"}
    assert "contact_email" in validate_new_survey(survey, set())

    survey["i18n"]["zh-Hans"]["reward"]["contact_email"] = "support@mindora316.com"
    assert validate_new_survey(survey, set()) is None

    # none / 缺省 reward 合法
    survey["i18n"]["zh-Hans"]["reward"] = {"gift_type": "none"}
    assert validate_new_survey(survey, set()) is None
    del survey["i18n"]["zh-Hans"]["reward"]
    assert validate_new_survey(survey, set()) is None


# -------------------- save_survey（写配置 + 热加载） --------------------

class TestSaveSurvey:
  def test_save_writes_config_and_reload_picks_up(self, ops_config):
    ok, msg = save_survey(_survey())
    assert ok, msg

    raw = json.loads(ops_config.read_text(encoding="utf-8"))
    assert "sv_new_01" in raw["surveys"]
    # 热加载后 query_survey 路径（get_survey 的 _i18n 口径）能拿到中文内容
    _, surveys, _ = ops_config_module._load_ops_config()
    stored = surveys["sv_new_01"]
    assert stored["i18n"]["zh-Hans"]["questions"][0]["type"] == "single_choice"
    # 既有问卷不受影响
    assert SURVEY_ID in surveys

  def test_save_duplicate_rejected(self, ops_config):
    ok, msg = save_survey(_survey(survey_id=SURVEY_ID))
    assert not ok and "已存在" in msg

  def test_save_invalid_writes_nothing(self, ops_config):
    before = ops_config.read_text(encoding="utf-8")
    ok, msg = save_survey({"survey_id": "sv_bad", "i18n": {}})
    assert not ok
    assert ops_config.read_text(encoding="utf-8") == before

  def test_save_stamps_created_at(self, ops_config, monkeypatch):
    import time as time_mod
    monkeypatch.setattr(ops_config_module.time, "time", lambda: 1790000000)
    ok, _ = save_survey(_survey())
    assert ok
    raw = json.loads(ops_config.read_text(encoding="utf-8"))
    assert raw["surveys"]["sv_new_01"]["created_at"] == 1790000000

  def test_save_preserves_existing_created_at(self, ops_config):
    ok, _ = save_survey(_survey(created_at=1234567890))
    assert ok
    raw = json.loads(ops_config.read_text(encoding="utf-8"))
    assert raw["surveys"]["sv_new_01"]["created_at"] == 1234567890


# -------------------- user_server /ops/survey_list --------------------

class TestSurveyListHandler:
  async def test_returns_surveys_sorted_by_created_at_desc(self, ops_config):
    save_survey(_survey(survey_id="sv_old", created_at=1000))
    save_survey(_survey(survey_id="sv_new", created_at=3000))
    # 夹具里的 sv_sleep_2026q3 没有 created_at → 排最后

    server = _server(True)
    resp = await UserServer.handle_ops_survey_list_http(server, _JsonReq({"jwt_token": "tok"}))
    assert resp.status == 200
    data = json.loads(resp.text)["data"]
    assert data["total"] == 3
    assert [s["survey_id"] for s in data["surveys"]] == ["sv_new", "sv_old", SURVEY_ID]
    assert data["surveys"][0]["created_at"] == 3000
    assert data["surveys"][2]["created_at"] is None
    assert data["surveys"][0]["i18n"]["zh-Hans"]["questions"]

  async def test_forbidden_for_non_admin(self):
    server = _server(False)
    resp = await UserServer.handle_ops_survey_list_http(server, _JsonReq({"jwt_token": "tok"}))
    assert resp.status == 403


# -------------------- ops 后台 /survey_list 页面 --------------------

class TestSurveyListPage:
  @pytest.fixture(autouse=True)
  def _patch(self, monkeypatch):
    monkeypatch.setattr(ops_admin_server, "_get_session",
                        lambda _req: {"jwt": "tok", "email": "ops@x.com", "uid": "u", "role": "admin"})
    monkeypatch.setattr(ops_admin_server, "_require_ops", lambda _s: None)
    self.surveys = [
      {"survey_id": "sv_jan", "created_at": 1767225600,  # 2026-01-01
       "i18n": {"zh-Hans": {"title": "一月问卷", "questions": [_choice_q()]}}},
      {"survey_id": "sv_aug", "created_at": 1786000000,  # 2026-08
       "i18n": {"zh-Hans": {"title": "八月问卷",
                            "questions": [_choice_q(), {"question_id": "q2", "index": 2, "type": "text",
                                                        "title": "建议？", "placeholder": "请输入", "required": False}],
                            "reward": {"gift_type": "physical", "gift_name": "好礼",
                                       "contact_email": "support@mindora316.com"}},
                "en": {"title": "August Survey", "questions": [_choice_q(title="Satisfied?")]}}},
      {"survey_id": "sv_legacy", "created_at": None,  # 老配置无创建时间
       "i18n": {"zh-Hans": {"title": "旧问卷", "questions": [_choice_q()]}}},
    ]
    monkeypatch.setattr(
      ops_admin_server, "_user_server_call",
      lambda path, body: {"code": 0, "data": {"surveys": self.surveys, "total": len(self.surveys)}},
    )

  class _GetReq:
    def __init__(self, query: dict):
      self.query = query

  async def test_list_all_without_filter(self):
    resp = await ops_admin_server.handle_survey_list(self._GetReq({}))
    assert resp.status == 200
    for sid in ("sv_jan", "sv_aug", "sv_legacy"):
      assert sid in resp.text

  async def test_filter_by_created_at(self):
    import time as time_mod
    start = time_mod.strftime("%Y-%m-%dT%H:%M", time_mod.localtime(1780000000))
    resp = await ops_admin_server.handle_survey_list(self._GetReq({"start_local": start}))
    assert "sv_aug" in resp.text
    assert "sv_jan" not in resp.text
    # 无 created_at 的老问卷在时间筛选时不显示
    assert "sv_legacy" not in resp.text

  async def test_show_renders_survey_detail_below_list(self):
    resp = await ops_admin_server.handle_survey_list(self._GetReq({"show": "sv_aug"}))
    assert resp.status == 200
    # 列表下方展开：双语标题、题干、选项、文本题占位/选答、奖励与客服邮箱
    assert "八月问卷" in resp.text and "August Survey" in resp.text
    assert "建议？" in resp.text and "请输入" in resp.text and "选答" in resp.text
    assert "好礼" in resp.text and "support@mindora316.com" in resp.text

  async def test_show_survey_filtered_out_gives_hint(self):
    import time as time_mod
    start = time_mod.strftime("%Y-%m-%dT%H:%M", time_mod.localtime(1780000000))
    resp = await ops_admin_server.handle_survey_list(
      self._GetReq({"show": "sv_jan", "start_local": start}),
    )
    assert resp.status == 200
    assert "不在当前筛选结果里" in resp.text

  async def test_backend_failure_renders_error_not_500(self, monkeypatch):
    """user_server 非 JSON 响应（未重启等）经 _user_server_call 降级为错误提示页。"""
    monkeypatch.setattr(ops_admin_server, "_user_server_call",
                        lambda path, body: {"code": 404, "msg": "user_server 响应异常（HTTP 404）"})
    resp = await ops_admin_server.handle_survey_list(self._GetReq({}))
    assert resp.status == 200
    assert "拉取问卷列表失败" in resp.text and "HTTP 404" in resp.text


class TestUserServerCallHardening:
  def test_non_json_response_returns_error_dict(self, monkeypatch):
    """线上 404 纯文本（"404: Not Found"）不再让 resp.json() 抛出。"""
    class _Resp:
      status_code = 404
      text = "404: Not Found"

      def json(self):
        raise ValueError("Extra data: line 1 column 4 (char 3)")

    monkeypatch.setattr(ops_admin_server.requests, "post", lambda *a, **k: _Resp())
    result = ops_admin_server._user_server_call("/ops/survey_list", {})
    assert result["code"] == 404
    assert "重启" in result["msg"]

# -------------------- user_server /ops/save_survey --------------------

def _server(is_admin: bool):
  server = UserServer.__new__(UserServer)

  async def _check(_tok):
    return "ops-uid" if is_admin else None

  server._check_ops_admin = _check
  return server


class _JsonReq:
  def __init__(self, body: dict):
    self._body = body

  async def json(self):
    return self._body


class TestSaveSurveyHandler:
  async def test_admin_save_success(self, ops_config):
    server = _server(True)
    resp = await UserServer.handle_ops_save_survey_http(
      server, _JsonReq({"jwt_token": "tok", "survey": _survey()}),
    )
    assert resp.status == 200
    _, surveys, _ = ops_config_module._load_ops_config()
    assert "sv_new_01" in surveys

  async def test_forbidden_for_non_admin(self):
    server = _server(False)
    resp = await UserServer.handle_ops_save_survey_http(
      server, _JsonReq({"jwt_token": "tok", "survey": _survey()}),
    )
    assert resp.status == 403

  async def test_validation_failure_400(self, ops_config):
    server = _server(True)
    resp = await UserServer.handle_ops_save_survey_http(
      server, _JsonReq({"jwt_token": "tok", "survey": {"survey_id": "sv_bad"}}),
    )
    assert resp.status == 400
    resp = await UserServer.handle_ops_save_survey_http(
      server, _JsonReq({"jwt_token": "tok", "survey": "not-a-dict"}),
    )
    assert resp.status == 400


# -------------------- ops 后台 /survey_edit POST --------------------

class TestSurveyEditPost:
  @pytest.fixture(autouse=True)
  def _patch(self, monkeypatch):
    monkeypatch.setattr(ops_admin_server, "_get_session",
                        lambda _req: {"jwt": "tok", "email": "ops@x.com", "uid": "u", "role": "admin"})
    monkeypatch.setattr(ops_admin_server, "_require_ops", lambda _s: None)
    self.calls = []
    monkeypatch.setattr(ops_admin_server, "_user_server_call",
                        lambda path, body: self.calls.append((path, body)) or {"code": 0, "msg": "saved sv_new_01"})

  class _PostReq:
    def __init__(self, form: dict):
      self._form = form

    async def post(self):
      return self._form

  async def test_success_forwards_survey(self):
    raw = json.dumps(_survey(), ensure_ascii=False)
    resp = await ops_admin_server.handle_survey_edit_post(self._PostReq({"survey_json": raw}))
    assert resp.status == 200
    path, body = self.calls[0]
    assert path == "/ops/save_survey"
    assert body["survey"]["survey_id"] == "sv_new_01"
    assert body["survey"]["i18n"]["zh-Hans"]["questions"][0]["options"][0]["text"] == "满意"

  async def test_bad_json_400_no_call(self):
    resp = await ops_admin_server.handle_survey_edit_post(self._PostReq({"survey_json": "{oops"}))
    assert resp.status == 400
    assert not self.calls

  async def test_server_rejection_rerenders_with_json(self, monkeypatch):
    monkeypatch.setattr(ops_admin_server, "_user_server_call",
                        lambda path, body: {"code": 400, "msg": "survey_id 已存在: sv_new_01"})
    resp = await ops_admin_server.handle_survey_edit_post(
      self._PostReq({"survey_json": json.dumps(_survey())}),
    )
    assert resp.status == 400
    assert "已存在" in resp.text and "survey_json" in resp.text  # 可编辑 JSON 重提
