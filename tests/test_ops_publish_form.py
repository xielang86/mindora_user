"""ops_admin_server 发布表单 builder（_build_popup_from_form）测试。

表单字段 → tanchuang_suvey.md 的 popup 结构；三种类型（survey/mall/ad）的动作与
落消息约定不同；user_server /ops/push 仍会二次校验（popup_id 查重等），这里只测
表单侧组装与基础校验。
"""
import io

import pytest

import ops_admin_server
from ops_admin_server import _build_popup_from_form


def _base_form(**overrides):
  form = {
    "type": "mall",
    "popup_id": "pop_20260827_mall_01",
    "priority": "50",
    "url": "https://example.com/promo",  # mall 固定 url 动作
    "zh_badge": "限时活动", "zh_badge_style": "orange",
    "zh_title": "夏日特惠", "zh_subtitle": "全场八折", "zh_action_text": "前往商城",
    "dismiss_stops": "on",
  }
  form.update(overrides)
  return form


# ── survey 类型 ─────────────────────────────────────────────────────

class TestSurveyType:
  def test_survey_fixed_action_and_forced_push(self):
    popup, err = _build_popup_from_form(_base_form(
      type="survey", survey_id="sv_sleep_2026q3",
    ))
    assert err is None
    assert popup["type"] == "survey"
    assert popup["action_type"] == "survey"
    assert popup["action_payload"] == {"survey_id": "sv_sleep_2026q3"}
    # survey 恒落站内消息，不看表单有没有勾 push_message
    assert popup["push_message"] is True

  def test_survey_requires_survey_id(self):
    popup, err = _build_popup_from_form(_base_form(type="survey", survey_id=""))
    assert popup is None and "survey_id" in err


# ── 动作与类型绑定：mall 固定 url；ad 可选 url/route/dismiss ────────

class TestMallAdAction:
  def test_mall_forces_url_action(self):
    # mall 没有其他动作可选：即使表单带了 route 字段也直接落 url
    popup, err = _build_popup_from_form(_base_form(
      type="mall", action_type="route", route="store", push_message="on",
    ))
    assert err is None
    assert popup["action_type"] == "url"
    assert popup["action_payload"] == {"url": "https://example.com/promo"}
    assert popup["push_message"] is True

  def test_mall_url_missing_rejected(self):
    popup, err = _build_popup_from_form(_base_form(type="mall", url=""))
    assert popup is None and "URL" in err

  def test_ad_route_action(self):
    popup, err = _build_popup_from_form(_base_form(
      type="ad", action_type="route", route=" Store ",
    ))
    assert err is None
    # route 归一化（去空格转小写），与客户端白名单口径一致
    assert popup["action_payload"] == {"route": "store"}

  def test_ad_route_not_in_whitelist_rejected(self):
    popup, err = _build_popup_from_form(_base_form(
      type="ad", action_type="route", route="admin_panel",
    ))
    assert popup is None and "白名单" in err

  def test_ad_url_action(self):
    popup, err = _build_popup_from_form(_base_form(
      type="ad", action_type="url", ad_url="https://example.com/brand",
    ))
    assert err is None
    assert popup["action_payload"] == {"url": "https://example.com/brand"}

  def test_ad_url_missing_rejected(self):
    popup, err = _build_popup_from_form(_base_form(type="ad", action_type="url", ad_url=""))
    assert popup is None and "URL" in err

  def test_dismiss_action_empty_payload(self):
    popup, err = _build_popup_from_form(_base_form(type="ad", action_type="dismiss"))
    assert err is None
    assert popup["action_payload"] == {}
    # ad 未勾 push_message → False（文档建议纯广告不落消息）
    assert popup["push_message"] is False


# ── i18n 文案（中文 / 英文二选一，至少一组） ─────────────────────────

class TestI18n:
  def test_at_least_one_language_required(self):
    popup, err = _build_popup_from_form(_base_form(zh_title="", en_title=""))
    assert popup is None and "至少填一种" in err

  def test_zh_action_text_required_with_zh_title(self):
    popup, err = _build_popup_from_form(_base_form(zh_action_text=""))
    assert popup is None and "中文按钮文案" in err

  def test_en_only_is_accepted(self):
    # 二选一：只填英文也可以（i18n 只有 en 组）
    popup, err = _build_popup_from_form(_base_form(
      zh_title="", en_title="Summer Sale", en_action_text="Go to Store",
    ))
    assert err is None
    assert set(popup["i18n"].keys()) == {"en"}

  def test_en_omitted_when_no_title(self):
    popup, err = _build_popup_from_form(_base_form())
    assert err is None
    assert set(popup["i18n"].keys()) == {"zh-Hans"}

  def test_en_included_when_title_given(self):
    popup, err = _build_popup_from_form(_base_form(
      en_badge="Limited Offer", en_badge_style="orange",
      en_title="Summer Sale", en_subtitle="20% off", en_action_text="Go to Store",
    ))
    assert err is None
    assert popup["i18n"]["en"]["title"] == "Summer Sale"

  def test_en_action_text_required_with_title(self):
    popup, err = _build_popup_from_form(_base_form(en_title="Summer Sale", en_action_text=""))
    assert popup is None and "英文按钮文案" in err


# ── 时间窗 / 优先级 / 展示规则 ──────────────────────────────────────

class TestFields:
  def test_time_window_converted(self):
    popup, err = _build_popup_from_form(_base_form(
      start_local="2026-09-01T10:00", end_local="2026-09-08T22:00",
    ))
    assert err is None
    assert isinstance(popup["start_at"], int) and isinstance(popup["end_at"], int)
    assert popup["start_at"] < popup["end_at"]

  def test_time_window_optional(self):
    popup, err = _build_popup_from_form(_base_form())
    assert err is None
    assert "start_at" not in popup and "end_at" not in popup

  def test_start_must_precede_end(self):
    popup, err = _build_popup_from_form(_base_form(
      start_local="2026-09-08T22:00", end_local="2026-09-01T10:00",
    ))
    assert popup is None and "早于" in err

  def test_priority_invalid(self):
    popup, err = _build_popup_from_form(_base_form(priority="abc"))
    assert popup is None and "整数" in err

  def test_display_rule_defaults_and_overrides(self):
    popup, err = _build_popup_from_form(_base_form())
    assert err is None
    # dismiss_stops 表单勾选 → True；未填的数值项不下发（沿用客户端缺省）
    assert popup["display_rule"] == {"dismiss_stops": True}

    form = _base_form(max_show_count="3", cooldown_hours="24")
    del form["dismiss_stops"]  # 取消勾选 → 显式 False
    popup, err = _build_popup_from_form(form)
    assert err is None
    # 表单按小时填，落地 JSON 换成秒
    assert popup["display_rule"] == {
      "dismiss_stops": False, "max_show_count": 3, "cooldown_seconds": 86400,
    }

  def test_cooldown_hours_fractional_and_invalid(self):
    popup, err = _build_popup_from_form(_base_form(cooldown_hours="0.5"))
    assert err is None
    assert popup["display_rule"]["cooldown_seconds"] == 1800

    popup, err = _build_popup_from_form(_base_form(cooldown_hours="abc"))
    assert popup is None and "小时" in err
    popup, err = _build_popup_from_form(_base_form(cooldown_hours="-1"))
    assert popup is None and "小时" in err

  def test_placement_and_id(self):
    popup, err = _build_popup_from_form(_base_form())
    assert err is None
    assert popup["popup_id"] == "pop_20260827_mall_01"
    assert popup["placement"] == "home"
    assert popup["priority"] == 50


# ── 主图：上传文件优先于手填 URL（handle_publish_post 集成）─────────

class _FileField:
  def __init__(self, filename: str, data: bytes = b"\x89PNG\r\n\x1a\n"):
    self.filename = filename
    self.file = io.BytesIO(data)


class _PostReq:
  def __init__(self, form: dict):
    self._form = form

  async def post(self):
    return self._form


class TestPublishPostImage:
  """handle_publish_post：选了文件就先上传到 user_server，用返回 URL 覆盖 image_url。"""

  @pytest.fixture(autouse=True)
  def _patch(self, monkeypatch):
    monkeypatch.setattr(ops_admin_server, "_get_session",
                        lambda _req: {"jwt": "tok", "email": "ops@x.com", "uid": "u", "role": "admin"})
    monkeypatch.setattr(ops_admin_server, "_require_ops", lambda _s: None)
    async def _form_body(_session, tip="", values=None):
      return tip or "form"
    monkeypatch.setattr(ops_admin_server, "_publish_form_body", _form_body)
    self.uploaded = []
    self.pushed = []
    monkeypatch.setattr(ops_admin_server, "_user_server_upload",
                        lambda fn, data, jwt: self.uploaded.append((fn, data)) or
                        {"code": 0, "data": {"url": "https://api.mindora316.com/popup_images/abc.png"}})
    monkeypatch.setattr(ops_admin_server, "_user_server_call",
                        lambda path, body: self.pushed.append((path, body)) or {"code": 0, "msg": "ok"})

  async def test_file_wins_over_url(self):
    form = _base_form(image_url="https://example.com/manual.png", image_file=_FileField("b.png"))
    resp = await ops_admin_server.handle_publish_post(_PostReq(form))
    assert resp.status == 200
    assert self.uploaded and self.uploaded[0][0] == "b.png"
    popup = self.pushed[0][1]["popup"]
    assert popup["image_url"] == "https://api.mindora316.com/popup_images/abc.png"

  async def test_url_kept_when_no_file(self):
    form = _base_form(image_url="https://example.com/manual.png",
                      image_file=_FileField(""))  # 浏览器未选文件 → 空文件名
    resp = await ops_admin_server.handle_publish_post(_PostReq(form))
    assert resp.status == 200
    assert not self.uploaded
    assert self.pushed[0][1]["popup"]["image_url"] == "https://example.com/manual.png"

  async def test_upload_failure_blocks_publish(self, monkeypatch):
    monkeypatch.setattr(ops_admin_server, "_user_server_upload",
                        lambda fn, data, jwt: {"code": 400, "msg": "仅支持 png / jpg / webp 图片"})
    form = _base_form(image_file=_FileField("b.png"))
    resp = await ops_admin_server.handle_publish_post(_PostReq(form))
    assert resp.status == 400
    assert not self.pushed  # 上传失败不触发 /ops/push
