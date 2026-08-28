"""ops_admin_server.py — Mindora 运营后台（简单版）。

功能：
  - 邮箱验证码登录（复用 auth_server /auth 的 send_verify_code + login_with_email_verify_code，
    与 App 同一套账号体系；登录后查 ops_role，仅 admin/super 可进入后台）
  - /surveys   问卷提交记录表格（一行一条记录，列为问卷字段；数据来自 user_server
               /ops/survey_records，底层是 leveldb 的 _meta:survey: 全量记录）
  - /publish   表单化创建弹窗消息（survey/mall/ad 三类型，动作/落消息按类型约束，
               高级模式保留原始 JSON 编辑），点击发布 → user_server /ops/push（user_server
               校验管理员后写入运营配置，热加载生效，等 App 拉取）
  - /publish_logs 发布记录（审计日志，user_server /ops/publish_logs，leveldb _meta:publish:*）
  - /admins    管理员授权页（仅 0号管理员 super 可见；super 只能数据库 SQL 直设：
               UPDATE user_auth SET ops_role='super' WHERE email='...'）

权限模型（user_auth.ops_role）：
  none   普通用户，登录后提示无权限
  admin  运营，可查看问卷记录 / 发布消息
  super  0号管理员，额外可授权他人为 admin/none（代码不提供设置 super 的入口）

启动：
  python ops_admin_server.py        # 默认 0.0.0.0:9200（OPS_ADMIN_PORT 可覆盖）
依赖：auth_server（登录/角色）与 user_server（问卷记录/消息发布）已启动。
"""

import asyncio
import html
import json
import logging
import os
import secrets
import time
from typing import Any, Optional

import requests
from aiohttp import web

from config import Config
from ops_config import POPUP_ROUTE_WHITELIST
import logger

run_dir = os.getenv("RUN_DIR") or os.path.dirname(os.path.abspath(__file__))
logger.init_log(f"{run_dir}/ops_admin_logs")

AUTH_URL = f"{Config.AUTH_SERVER_URL}/auth"
USER_SERVER = os.getenv("USER_SERVER_URL", f"http://127.0.0.1:{Config.PORT}")

# send_verify_code 需要 device_id；运营后台共用一个固定 UUID（与 App 设备区分）
OPS_DEVICE_ID = "0a9d8c7b-6a5b-4c3d-2e1f-0a9b8c7d6e5f"

SESSION_COOKIE = "ops_session"
SESSION_TTL_SECONDS = 12 * 3600

# 内存会话（单进程简单后台够用；重启即要求重新登录）
_sessions: dict[str, dict[str, Any]] = {}


# -------------------- auth_server / user_server HTTP 封装 --------------------

def _auth_call(request_type: str, data: dict) -> dict:
  payload = {"request_type": request_type, "timestamp": int(time.time()), "version": "1.0", "data": data}
  resp = requests.post(AUTH_URL, json=payload, timeout=10)
  return resp.json()


def _user_server_call(path: str, body: dict) -> dict:
  resp = requests.post(f"{USER_SERVER}{path}", json=body, timeout=10)
  return resp.json()


def _user_server_upload(filename: str, data: bytes, jwt_token: str) -> dict:
  """multipart 上传弹窗主图到 user_server，返回 {"code":0, "data":{"url":...}}。"""
  resp = requests.post(
    f"{USER_SERVER}/ops/upload_image",
    data={"jwt_token": jwt_token},
    files={"image": (filename or "image", data)},
    timeout=30,
  )
  return resp.json()


# -------------------- 会话 --------------------

def _new_session(jwt_token: str, email: str, uid: str, role: str) -> str:
  sid = secrets.token_hex(16)
  _sessions[sid] = {
    "jwt": jwt_token, "email": email, "uid": uid, "role": role,
    "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
  }
  return sid


def _get_session(request: web.Request) -> Optional[dict]:
  sid = request.cookies.get(SESSION_COOKIE)
  sess = _sessions.get(sid or "")
  if not sess:
    return None
  if sess["expires_at"] < int(time.time()):
    _sessions.pop(sid, None)
    return None
  return sess


# -------------------- 页面模板 --------------------

def _page(title: str, body: str, session: Optional[dict] = None) -> str:
  nav = ""
  if session:
    admin_link = ' | <a href="/admins">管理员授权</a>' if session["role"] == "super" else ""
    nav = (
      f'<p><a href="/surveys">问卷记录</a> | <a href="/answers">问卷作答</a> | <a href="/publish">消息发布</a> | <a href="/publish_logs">发布记录</a>{admin_link}'
      f' | <a href="/logout">退出</a>'
      f'&nbsp;&nbsp;<small>{html.escape(session["email"])}（{session["role"]}）</small></p><hr>'
    )
  return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(title)} - Mindora 运营后台</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; font-size: 13px; vertical-align: top; }}
th {{ background: #f3f0fa; }}
textarea {{ width: 100%; font-family: monospace; font-size: 13px; }}
input, select {{ padding: 6px; font-size: 14px; }}
button {{ padding: 8px 20px; font-size: 14px; }}
.msg {{ padding: 10px; background: #eef7ee; border: 1px solid #b6d7b6; margin: 12px 0; }}
.err {{ padding: 10px; background: #fdecec; border: 1px solid #e0b4b4; margin: 12px 0; }}
</style></head><body>
<h2>{html.escape(title)}</h2>
{nav}
{body}
</body></html>"""


def _html_response(title: str, body: str, session: Optional[dict] = None, status: int = 200) -> web.Response:
  return web.Response(text=_page(title, body, session), content_type="text/html", status=status)


def _esc(value: Any) -> str:
  return html.escape("" if value is None else str(value))


def _fmt_ts(ts: Any) -> str:
  try:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
  except (TypeError, ValueError, OSError):
    return _esc(ts)


# -------------------- 登录 --------------------

def _login_body(email: str = "") -> str:
  return f"""
<form method="post">
  <p>邮箱：<input type="email" name="email" required size="30" value="{html.escape(email, quote=True)}"></p>
  <p>验证码：<input type="text" name="verify_code" size="10" autofocus>
  <small>先点「发送验证码」收邮件，再填码登录</small></p>
  <p>
    <button type="submit" formaction="/send_code" formnovalidate>发送验证码</button>
    <button type="submit" formaction="/login">登录</button>
  </p>
</form>
"""


async def handle_index(request: web.Request) -> web.Response:
  session = _get_session(request)
  if session:
    if session["role"] in ("admin", "super"):
      raise web.HTTPFound("/surveys")
    return _html_response("无运营权限", '<div class="err">当前账号无运营权限（ops_role=none），请联系0号管理员授权。</div>', session, status=403)
  return _html_response("登录", _login_body())


async def handle_send_code(request: web.Request) -> web.Response:
  form = await request.post()
  email = (form.get("email") or "").strip()
  result = await asyncio.to_thread(_auth_call, "send_verify_code", {"email": email, "device_id": OPS_DEVICE_ID})
  if result.get("code") == 0:
    tip = f'<div class="msg">验证码已发送至 {_esc(email)}，请查收邮件。</div>'
  else:
    tip = f'<div class="err">发送失败：{_esc(result.get("msg"))}</div>'
  return _html_response("登录", tip + _login_body(email))


async def handle_login(request: web.Request) -> web.Response:
  form = await request.post()
  email = (form.get("email") or "").strip()
  code = (form.get("verify_code") or "").strip()
  if not code:
    return _html_response("登录", '<div class="err">请填写验证码（先点「发送验证码」收邮件）。</div>' + _login_body(email), status=400)
  result = await asyncio.to_thread(
    _auth_call, "login_with_email_verify_code",
    {"email": email, "device_id": OPS_DEVICE_ID, "verify_code": code},
  )
  token_data = (result.get("data") or {}) if result.get("code") == 0 else None
  if not token_data or not token_data.get("token"):
    return _html_response("登录", f'<div class="err">登录失败：{_esc(result.get("msg"))}</div>' + _login_body(email), status=401)

  jwt_token = token_data["token"]
  role_resp = await asyncio.to_thread(_auth_call, "query_ops_role", {"jwt_token": jwt_token})
  role = ((role_resp.get("data") or {}).get("ops_role")) or "none"

  sid = _new_session(jwt_token, email, token_data.get("uid", ""), role)
  resp = web.HTTPFound("/surveys" if role in ("admin", "super") else "/")
  resp.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL_SECONDS, httponly=True)
  logging.info("ops admin login: email=%s role=%s", email, role)
  raise resp


async def handle_logout(request: web.Request) -> web.Response:
  sid = request.cookies.get(SESSION_COOKIE)
  _sessions.pop(sid or "", None)
  resp = web.HTTPFound("/")
  resp.del_cookie(SESSION_COOKIE)
  raise resp


def _require_ops(session: Optional[dict], allow_roles=("admin", "super")) -> Optional[web.Response]:
  """返回 None 表示放行；否则返回重定向/错误响应。"""
  if session is None:
    return web.HTTPFound("/")
  if session["role"] not in allow_roles:
    return _html_response("无权限", '<div class="err">当前账号无此操作权限。</div>', session, status=403)
  return None


# -------------------- 问卷记录 --------------------

async def handle_surveys(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  survey_id = request.query.get("survey_id") or None
  result = await asyncio.to_thread(
    _user_server_call, "/ops/survey_records",
    {"jwt_token": session["jwt"], **({"survey_id": survey_id} if survey_id else {})},
  )
  if result.get("code") != 0:
    return _html_response("问卷记录", f'<div class="err">拉取失败：{_esc(result.get("msg"))}</div>', session)

  records = (result.get("data") or {}).get("records") or []

  # 动态列：基础列 + 出现过的全部 question_id（按 q 序排列）
  question_ids = sorted({
    a.get("question_id") for r in records for a in (r.get("answers") or []) if a.get("question_id")
  })
  base_cols = ["submission_id", "uid", "survey_id", "submitted_at", "duration_seconds"]
  header = "".join(f"<th>{_esc(c)}</th>" for c in base_cols)
  header += "".join(f"<th>{_esc(q)}</th>" for q in question_ids)
  header += "<th>gift_delivery</th>"

  rows = []
  for r in records:
    answers = {a.get("question_id"): a for a in (r.get("answers") or [])}
    cells = [
      _esc(r.get("submission_id")), _esc(r.get("uid")), _esc(r.get("survey_id")),
      _fmt_ts(r.get("submitted_at")), _esc(r.get("duration_seconds")),
    ]
    for q in question_ids:
      a = answers.get(q) or {}
      value = ", ".join(a.get("option_ids") or []) if a.get("type") != "text" else a.get("text", "")
      cells.append(_esc(value))
    gift = r.get("gift_delivery") or {}
    cells.append(_esc(" / ".join(str(gift.get(k, "")) for k in ("type", "name", "phone") if gift.get(k))))
    rows.append("<tr><td>" + "</td><td>".join(cells) + "</td></tr>")

  body = f"""
<form method="get" action="/surveys">
  <p>survey_id 过滤：<input type="text" name="survey_id" value="{_esc(survey_id or '')}" size="30">
  <button type="submit">查询</button></p>
</form>
<p>共 {len(records)} 条记录</p>
<table><tr>{header}</tr>{''.join(rows)}</table>
"""
  return _html_response("问卷提交记录", body, session)


# -------------------- 问卷作答明细 --------------------

def _answer_text(answer: Optional[dict], question: dict) -> str:
  """把一条作答渲染成可读文本：选择题 option_id → 选项文本，文本题原文。"""
  if not answer:
    return ""
  if question.get("type") == "text":
    return answer.get("text", "")
  option_map = {o.get("option_id"): o.get("text", "") for o in (question.get("options") or [])}
  return ", ".join(option_map.get(oid, oid) for oid in (answer.get("option_ids") or []))


async def handle_answers(request: web.Request) -> web.Response:
  """问卷作答明细页：一行一条提交记录，列为 uid / email / 每题题干，表格值是可读答案。"""
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  survey_id = (request.query.get("survey_id") or "").strip()

  # 未选问卷：拉全量记录，列出所有出现过的 survey_id 供选择
  if not survey_id:
    result = await asyncio.to_thread(_user_server_call, "/ops/survey_records", {"jwt_token": session["jwt"]})
    records = (result.get("data") or {}).get("records") or [] if result.get("code") == 0 else []
    ids = sorted({r.get("survey_id") for r in records if r.get("survey_id")})
    links = "".join(
      f'<li><a href="/answers?survey_id={_esc(sid)}">{_esc(sid)}</a></li>' for sid in ids
    )
    body = f"<p>选择要查看的问卷：</p><ul>{links or '<li>暂无提交记录</li>'}</ul>"
    return _html_response("问卷作答", body, session)

  # 拉问卷定义（用现有 query_survey 接口）与提交记录
  survey_resp = await asyncio.to_thread(
    _user_server_call, "/survey",
    {
      "request_type": "query_survey",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {"jwt_token": session["jwt"], "language": "zh-Hans", "survey_id": survey_id},
    },
  )
  records_resp = await asyncio.to_thread(
    _user_server_call, "/ops/survey_records",
    {"jwt_token": session["jwt"], "survey_id": survey_id},
  )

  questions = ((survey_resp.get("data") or {}).get("questions") or []) if survey_resp.get("code") == 0 else []
  records = ((records_resp.get("data") or {}).get("records") or []) if records_resp.get("code") == 0 else []
  if not questions:
    return _html_response("问卷作答", f'<div class="err">问卷 {_esc(survey_id)} 的定义不存在或已下线。</div>', session, status=404)

  header = "<th>uid</th><th>email</th>" + "".join(
    f'<th title="{_esc(q.get("question_id"))}">{_esc(q.get("title") or q.get("question_id"))}</th>'
    for q in questions
  )

  rows = []
  for r in records:
    answers = {a.get("question_id"): a for a in (r.get("answers") or [])}
    cells = [_esc(r.get("uid")), _esc(r.get("email") or "")]
    cells += [_esc(_answer_text(answers.get(q.get("question_id")), q)) for q in questions]
    rows.append("<tr><td>" + "</td><td>".join(cells) + "</td></tr>")

  body = f"""
<p>问卷：{_esc(survey_id)}　共 {len(records)} 条提交　<small>（列悬停可看 question_id；<a href="/answers">换一份问卷</a>）</small></p>
<table><tr>{header}</tr>{''.join(rows)}</table>
"""
  return _html_response("问卷作答", body, session)


# -------------------- 消息发布 --------------------

# 三种类型的投放约定（tanchuang_suvey.md popups[] 字段说明）：
#   survey 参与调研：action_type 固定 survey，恒落站内消息（不看 push_message）
#   mall   商城活动：动作 url/route/dismiss，建议落站内消息
#   ad     纯广告/品牌推荐：动作 url/route/dismiss，建议不落站内消息
_TYPE_META = {
  "survey": {"label": "参与调研", "badge": "参与调研", "badge_style": "purple", "push_default": True},
  "mall":   {"label": "商城活动", "badge": "限时活动", "badge_style": "orange", "push_default": True},
  "ad":     {"label": "纯广告/品牌推荐", "badge": "品牌推荐", "badge_style": "blue", "push_default": False},
}

_ROUTE_LABELS = {
  "home": "首页 Tab", "sleep": "睡眠/健康 Tab", "explore": "探索 Tab", "store": "商城 Tab",
  "subscription": "订阅/会员页", "redeem": "兑换码页", "footprint": "陪伴足迹",
  "device": "设备管理", "profile": "个人资料", "settings": "设置",
  "faq": "常见问题", "notifications": "消息通知",
}


def _int_field(form, name: str):
  """可选整数表单字段：空 → None；非数字 → 'invalid'。"""
  raw = (form.get(name) or "").strip()
  if not raw:
    return None
  try:
    return int(raw)
  except ValueError:
    return "invalid"


def _ts_field(form, name: str):
  """datetime-local 表单字段 → epoch 秒（按本服务器本地时区解释）；空 → None。"""
  raw = (form.get(name) or "").strip()
  if not raw:
    return None
  try:
    return int(time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M")))
  except ValueError:
    return "invalid"


def _build_popup_from_form(form) -> tuple[Optional[dict], Optional[str]]:
  """把发布表单的字段组装成弹窗 popup dict（tanchuang_suvey.md 结构）。

  返回 (popup, 错误信息)；服务端 /ops/push 还会再完整校验一遍（popup_id 查重等）。
  """
  ptype = (form.get("type") or "").strip()
  if ptype not in _TYPE_META:
    return None, "类型必须是 survey / mall / ad"
  popup_id = (form.get("popup_id") or "").strip()
  if not popup_id:
    return None, "popup_id 必填"

  # 动作：survey 固定打开问卷；mall/ad 可选 url / route / dismiss
  if ptype == "survey":
    action_type = "survey"
    survey_id = (form.get("survey_id") or "").strip()
    if not survey_id:
      return None, "survey 类型必须选择问卷 survey_id"
    action_payload = {"survey_id": survey_id}
  else:
    action_type = (form.get("action_type") or "dismiss").strip()
    if action_type not in ("url", "route", "dismiss"):
      return None, "mall / ad 的动作必须是 url / route / dismiss"
    if action_type == "url":
      url = (form.get("url") or "").strip()
      if not url:
        return None, "动作为「打开网页」时链接 URL 必填"
      action_payload = {"url": url}
    elif action_type == "route":
      route = (form.get("route") or "").strip().lower()
      if route not in POPUP_ROUTE_WHITELIST:
        return None, f"route 不在客户端白名单: {route!r}"
      action_payload = {"route": route}
    else:
      action_payload = {}

  # i18n：中文必填；英文标题填了才下发 en 组（此时按钮文案也必填）
  def _lang(prefix: str) -> Optional[dict]:
    title = (form.get(f"{prefix}_title") or "").strip()
    if not title:
      return None
    return {
      "badge": (form.get(f"{prefix}_badge") or "").strip(),
      "badge_style": (form.get(f"{prefix}_badge_style") or "purple").strip(),
      "title": title,
      "subtitle": (form.get(f"{prefix}_subtitle") or "").strip(),
      "action_text": (form.get(f"{prefix}_action_text") or "").strip(),
    }

  zh = _lang("zh")
  if not zh:
    return None, "中文标题必填"
  if not zh["action_text"]:
    return None, "中文按钮文案必填"
  i18n = {"zh-Hans": zh}
  en = _lang("en")
  if en:
    if not en["action_text"]:
      return None, "填了英文标题时英文按钮文案也必填"
    i18n["en"] = en

  start_at, end_at = _ts_field(form, "start_local"), _ts_field(form, "end_local")
  if start_at == "invalid" or end_at == "invalid":
    return None, "展示时间格式不正确"
  if start_at is not None and end_at is not None and start_at >= end_at:
    return None, "开始时间必须早于结束时间"

  priority = _int_field(form, "priority")
  if priority == "invalid":
    return None, "优先级必须是整数"

  # 落站内消息：survey 恒落；mall/ad 看勾选（类型默认值已预填）
  push_message = True if ptype == "survey" else (form.get("push_message") is not None)

  popup = {
    "popup_id": popup_id,
    "type": ptype,
    "placement": "home",
    "priority": 50 if priority is None else priority,
    "action_type": action_type,
    "action_payload": action_payload,
    "image_url": (form.get("image_url") or "").strip(),
    "push_message": push_message,
    "i18n": i18n,
  }
  if start_at is not None:
    popup["start_at"] = start_at
  if end_at is not None:
    popup["end_at"] = end_at

  # 展示规则：填了才下发；dismiss_stops 表单恒有值（勾选=true 缺省，取消=false 显式）
  display_rule = {"dismiss_stops": form.get("dismiss_stops") is not None}
  max_show = _int_field(form, "max_show_count")
  cooldown = _int_field(form, "cooldown_seconds")
  if max_show == "invalid" or cooldown == "invalid":
    return None, "展示规则里的数值必须是整数"
  if max_show is not None:
    display_rule["max_show_count"] = max_show
  if cooldown is not None:
    display_rule["cooldown_seconds"] = cooldown
  popup["display_rule"] = display_rule
  return popup, None


def _lang_fieldset(prefix: str, title: str, required: bool, meta: dict) -> str:
  req = " required" if required else ""
  styles = "".join(
    f'<option value="{s}"{" selected" if s == meta["badge_style"] else ""}>{s}</option>'
    for s in ("purple", "orange", "blue")
  )
  return f"""
<fieldset><legend>{_esc(title)}</legend>
  <p>左上角标签：<input type="text" name="{prefix}_badge" value="{_esc(meta['badge'])}" size="12">
  标签配色：<select name="{prefix}_badge_style">{styles}</select></p>
  <p>主标题：<input type="text" name="{prefix}_title" size="40"{req}></p>
  <p>副标题：<input type="text" name="{prefix}_subtitle" size="60"></p>
  <p>按钮文案：<input type="text" name="{prefix}_action_text" size="20"{req}></p>
</fieldset>"""


async def handle_publish_get(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny
  return _html_response("消息发布", await _publish_form_body(session), session)


async def _publish_form_body(session: dict, tip: str = "", values: Optional[dict] = None) -> str:
  """发布表单页 body。values 用于发布失败后回填。"""
  v = values or {}
  meta_result = await asyncio.to_thread(
    _user_server_call, "/ops/popup_meta", {"jwt_token": session["jwt"]},
  )
  meta = (meta_result.get("data") or {}) if meta_result.get("code") == 0 else {}
  survey_ids = meta.get("survey_ids") or []
  popup_ids = meta.get("popup_ids") or []

  survey_options = '<option value="">— 请选择问卷 —</option>' + "".join(
    f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in survey_ids
  )
  route_options = "".join(
    f'<option value="{r}">{r}（{_esc(_ROUTE_LABELS.get(r, ""))}）</option>'
    for r in sorted(POPUP_ROUTE_WHITELIST)
  )
  type_radios = "".join(
    f'<label style="margin-right:16px"><input type="radio" name="type" value="{t}"'
    f'{" checked" if (v.get("type") or "survey") == t else ""} onchange="onTypeChange()"> '
    f'<b>{_esc(m["label"])}</b>（{t}）</label>'
    for t, m in _TYPE_META.items()
  )

  body = tip + f"""
<style>
fieldset {{ margin: 12px 0; border: 1px solid #ccc; padding: 10px 14px; }}
legend {{ font-weight: bold; }}
.hint {{ color: #888; font-size: 12px; }}
</style>
<form method="post" action="/publish" enctype="multipart/form-data">
<fieldset><legend>基本信息</legend>
  <p>消息类型：{type_radios}<br>
  <span class="hint">survey=参与调研（点按钮打开问卷，恒落站内消息）；mall=商城活动（建议落站内消息）；ad=纯广告/品牌推荐（建议不落）</span></p>
  <p>popup_id：<input type="text" name="popup_id" id="popup_id" size="32" required
     value="{_esc(v.get('popup_id', ''))}" oninput="checkDup()">
  <button type="button" onclick="suggestId()">生成建议</button>
  <span id="dupwarn" class="hint"></span></p>
  <p>优先级：<input type="number" name="priority" value="{_esc(v.get('priority', '50'))}" size="6">
  <span class="hint">数值大者优先；每次回前台最多展示一条</span></p>
  <p>展示时间窗（按本服务器本地时区，可留空 = 不限）：
  从 <input type="datetime-local" name="start_local" value="{_esc(v.get('start_local', ''))}">
  到 <input type="datetime-local" name="end_local" value="{_esc(v.get('end_local', ''))}"></p>
</fieldset>

<fieldset><legend>动作（用户点按钮后）</legend>
  <div id="act-survey">
    <p>问卷：<select name="survey_id">{survey_options}</select>
    <span class="hint">type=survey 固定打开问卷；该类型恒落站内消息</span></p>
  </div>
  <div id="act-mall-ad">
    <p>动作：<select name="action_type" id="action_type" onchange="onActionChange()">
      <option value="dismiss">dismiss（仅关闭，客户端默认落商城 Tab）</option>
      <option value="route">route（App 内路由）</option>
      <option value="url">url（打开网页）</option>
    </select></p>
    <p id="act-route">路由：<select name="route">{route_options}</select>
    <span class="hint">客户端硬编码白名单，新路由须先发客户端版本</span></p>
    <p id="act-url">链接：<input type="url" name="url" size="50" placeholder="https://…"></p>
    <p><label><input type="checkbox" name="push_message" id="push_message"> 同步落一条站内消息</label></p>
  </div>
</fieldset>

{_lang_fieldset("zh", "中文文案（必填）", True, _TYPE_META[(v.get('type') or 'survey')])}
{_lang_fieldset("en", "英文文案（可选；填了标题才下发 en 组）", False, _TYPE_META[(v.get('type') or 'survey')])}

<fieldset><legend>展示规则与配图（可选）</legend>
  <p>最多展示次数：<input type="number" name="max_show_count" size="6" min="1">
  两次展示最小间隔（秒）：<input type="number" name="cooldown_seconds" size="8" min="0">
  <label><input type="checkbox" name="dismiss_stops" checked> 用户手动关闭后不再展示</label></p>
  <p>主图 URL：<input type="url" name="image_url" size="60"
     value="{_esc(v.get('image_url', ''))}" placeholder="约 590×286；留空用客户端按类型内置底图"></p>
  <p>或上传图片：<input type="file" name="image_file" accept="image/png,image/jpeg,image/webp">
  <span class="hint">png / jpg / webp，≤2MB；上传后存服务端并生成公网 URL（两者都填时以上传文件为准）</span></p>
</fieldset>
<p><button type="submit">发布</button></p>
</form>

<details><summary>高级模式：直接编辑 JSON（字段见 tanchuang_suvey.md）</summary>
<form method="post" action="/publish">
  <textarea name="popup_json" rows="20">{html.escape(json.dumps({"popup_id": "", "type": "mall", "placement": "home", "priority": 50, "action_type": "route", "action_payload": {"route": "store"}, "image_url": "", "push_message": True, "i18n": {"zh-Hans": {"badge": "", "badge_style": "orange", "title": "", "subtitle": "", "action_text": ""}}}, ensure_ascii=False, indent=2))}</textarea>
  <p><button type="submit">按 JSON 发布</button></p>
</form>
</details>

<script>
const EXISTING_IDS = {json.dumps(popup_ids)};
const PUSH_DEFAULT = {json.dumps({t: m["push_default"] for t, m in _TYPE_META.items()})};
function curType() {{ return document.querySelector('input[name="type"]:checked').value; }}
function onTypeChange() {{
  const t = curType();
  document.getElementById('act-survey').style.display = t === 'survey' ? '' : 'none';
  document.getElementById('act-mall-ad').style.display = t === 'survey' ? 'none' : '';
  document.getElementById('push_message').checked = PUSH_DEFAULT[t];
}}
function onActionChange() {{
  const a = document.getElementById('action_type').value;
  document.getElementById('act-route').style.display = a === 'route' ? '' : 'none';
  document.getElementById('act-url').style.display = a === 'url' ? '' : 'none';
}}
function suggestId() {{
  const t = curType();
  const d = new Date();
  const ymd = `${{d.getFullYear()}}${{String(d.getMonth()+1).padStart(2,'0')}}${{String(d.getDate()).padStart(2,'0')}}`;
  for (let n = 1; n < 100; n++) {{
    const id = `pop_${{ymd}}_${{t}}_${{String(n).padStart(2,'0')}}`;
    if (!EXISTING_IDS.includes(id)) {{ document.getElementById('popup_id').value = id; checkDup(); return; }}
  }}
}}
function checkDup() {{
  const id = document.getElementById('popup_id').value;
  document.getElementById('dupwarn').textContent =
    EXISTING_IDS.includes(id) ? '⚠ 该 popup_id 已存在，发布会被拒绝' : '';
}}
onTypeChange(); onActionChange(); checkDup();
</script>
"""
  return body


async def handle_publish_post(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  form = await request.post()
  raw = form.get("popup_json") or ""
  if raw.strip():
    # 高级模式：直接 JSON
    try:
      popup = json.loads(raw)
    except json.JSONDecodeError as e:
      return _html_response("消息发布", f'<div class="err">JSON 解析失败：{_esc(e)}</div>', session, status=400)
  else:
    popup, error = _build_popup_from_form(form)
    if error:
      tip = f'<div class="err">表单校验失败：{_esc(error)}</div>'
      return _html_response("消息发布", await _publish_form_body(session, tip, dict(form)), session, status=400)
    # 图片：上传文件优先于手填 URL（先传到 user_server 拿公网 URL，再随 popup 发布）
    image_file = form.get("image_file")
    if image_file is not None and getattr(image_file, "filename", ""):
      up = await asyncio.to_thread(
        _user_server_upload, image_file.filename, image_file.file.read(), session["jwt"],
      )
      if up.get("code") != 0:
        tip = f'<div class="err">图片上传失败：{_esc(up.get("msg"))}</div>'
        return _html_response("消息发布", await _publish_form_body(session, tip, dict(form)), session, status=400)
      popup["image_url"] = (up.get("data") or {}).get("url") or popup.get("image_url", "")

  result = await asyncio.to_thread(_user_server_call, "/ops/push", {"jwt_token": session["jwt"], "popup": popup})
  if result.get("code") == 0:
    tip = (f'<div class="msg">发布成功：{_esc(result.get("msg"))}'
           f'（App 下次 query_popups 生效；<a href="/publish_logs">查看发布记录</a>）</div>')
    logging.info("ops publish by %s: code=%s popup_id=%s", session["email"], result.get("code"),
                 popup.get("popup_id") if isinstance(popup, dict) else None)
    return _html_response("消息发布", await _publish_form_body(session, tip), session)

  tip = f'<div class="err">发布失败：{_esc(result.get("msg"))}</div>'
  logging.info("ops publish by %s: code=%s popup_id=%s", session["email"], result.get("code"),
               popup.get("popup_id") if isinstance(popup, dict) else None)
  if raw.strip():
    body = tip + f"""
<form method="post" action="/publish">
  <textarea name="popup_json" rows="20">{html.escape(raw)}</textarea>
  <p><button type="submit">按 JSON 发布</button></p>
</form>"""
    return _html_response("消息发布", body, session)
  return _html_response("消息发布", await _publish_form_body(session, tip, dict(form)), session)


# -------------------- 发布记录（审计日志） --------------------

async def handle_publish_logs(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  result = await asyncio.to_thread(_user_server_call, "/ops/publish_logs", {"jwt_token": session["jwt"]})
  if result.get("code") != 0:
    return _html_response("发布记录", f'<div class="err">拉取失败：{_esc(result.get("msg"))}</div>', session)

  records = (result.get("data") or {}).get("records") or []
  rows = []
  for r in records:
    popup = r.get("popup") or {}
    i18n = popup.get("i18n") or {}
    title = (i18n.get("zh-Hans") or {}).get("title") or ""
    payload = html.escape(json.dumps(popup, ensure_ascii=False, indent=2))
    rows.append(
      "<tr>"
      f"<td>{_fmt_ts(r.get('published_at'))}</td>"
      f"<td>{_esc(r.get('operator_email') or r.get('operator_uid'))}</td>"
      f"<td>{_esc(r.get('popup_id'))}</td>"
      f"<td>{_esc(popup.get('type'))}</td>"
      f"<td>{_esc(title)}</td>"
      f"<td><details><summary>payload</summary><pre>{payload}</pre></details></td>"
      "</tr>"
    )

  body = f"""
<p>共 {len(records)} 条发布记录（append-only 审计日志，按发布时间倒序）</p>
<table><tr><th>发布时间</th><th>操作者</th><th>popup_id</th><th>类型</th><th>标题</th><th>完整内容</th></tr>
{''.join(rows)}</table>
"""
  return _html_response("发布记录", body, session)


# -------------------- 管理员授权（仅 super） --------------------

async def handle_admins_get(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session, allow_roles=("super",))
  if deny:
    return deny
  body = """
<p>授权他人成为运营（admin）或撤销（none）。0号管理员（super）只能由数据库直接设置：
<code>UPDATE user_auth SET ops_role='super' WHERE email='...';</code></p>
<form method="post" action="/admins">
  <p>目标用户邮箱：<input type="email" name="target_email" required size="30"></p>
  <p>角色：<select name="ops_role">
    <option value="admin">admin（运营，可发布消息）</option>
    <option value="none">none（撤销运营权限）</option>
  </select>
  <button type="submit">授权</button></p>
</form>
"""
  return _html_response("管理员授权", body, session)


async def handle_admins_post(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session, allow_roles=("super",))
  if deny:
    return deny

  form = await request.post()
  target_email = (form.get("target_email") or "").strip()
  ops_role = (form.get("ops_role") or "").strip()
  result = await asyncio.to_thread(
    _auth_call, "grant_ops_role",
    {"jwt_token": session["jwt"], "target_email": target_email, "ops_role": ops_role},
  )
  if result.get("code") == 0:
    tip = f'<div class="msg">授权成功：{_esc(target_email)} → {_esc(ops_role)}</div>'
  else:
    tip = f'<div class="err">授权失败：{_esc(result.get("msg") or result.get("detail"))}</div>'
  logging.info("ops grant by %s: %s -> %s code=%s", session["email"], target_email, ops_role, result.get("code"))
  return _html_response("管理员授权", tip + '<p><a href="/admins">返回</a></p>', session)


# -------------------- 启动 --------------------

def build_app() -> web.Application:
  app = web.Application()
  app.router.add_get("/", handle_index)
  app.router.add_post("/send_code", handle_send_code)
  app.router.add_post("/login", handle_login)
  app.router.add_get("/logout", handle_logout)
  app.router.add_get("/surveys", handle_surveys)
  app.router.add_get("/answers", handle_answers)
  app.router.add_get("/publish", handle_publish_get)
  app.router.add_post("/publish", handle_publish_post)
  app.router.add_get("/publish_logs", handle_publish_logs)
  app.router.add_get("/admins", handle_admins_get)
  app.router.add_post("/admins", handle_admins_post)
  return app


async def start():
  app = build_app()
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, "0.0.0.0", Config.OPS_ADMIN_PORT)
  await site.start()
  logging.info("ops admin server started on http://0.0.0.0:%s", Config.OPS_ADMIN_PORT)
  await asyncio.Event().wait()


if __name__ == "__main__":
  try:
    asyncio.run(start())
  except KeyboardInterrupt:
    logging.warning("Shutting down ops admin server.")
