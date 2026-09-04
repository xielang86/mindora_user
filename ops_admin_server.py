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
  try:
    return resp.json()
  except ValueError:
    # 非 JSON 响应（典型：user_server 未重启到带该端点的版本，404 返回纯文本）——
    # 不上抛 500，按调用失败返回，页面降级为错误提示
    logging.error("user_server %s non-JSON response (HTTP %s): %.200s", path, resp.status_code, resp.text)
    return {
      "code": resp.status_code or 502,
      "msg": f"user_server 响应异常（HTTP {resp.status_code}），请确认 user_server 已重启到最新版本",
    }


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
      f'<p><a href="/surveys">问卷记录</a> | <a href="/answers">问卷作答</a> | <a href="/publish">消息发布</a> | <a href="/survey_edit">新建问卷</a> | <a href="/survey_list">问卷列表</a> | <a href="/publish_logs">发布记录</a> | <a href="/insight_rules">洞察阈值</a>{admin_link}'
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

  # 动作与消息类型绑定：survey 固定打开问卷；mall 固定打开网页；ad 可选 url/route/dismiss
  if ptype == "survey":
    action_type = "survey"
    survey_id = (form.get("survey_id") or "").strip()
    if not survey_id:
      return None, "survey 类型必须选择问卷 survey_id"
    action_payload = {"survey_id": survey_id}
  elif ptype == "mall":
    action_type = "url"
    url = (form.get("url") or "").strip()
    if not url:
      return None, "mall 类型动作为「打开网页」，链接 URL 必填"
    action_payload = {"url": url}
  else:  # ad
    action_type = (form.get("action_type") or "dismiss").strip()
    if action_type not in ("url", "route", "dismiss"):
      return None, "ad 的动作必须是 url / route / dismiss"
    if action_type == "url":
      url = (form.get("ad_url") or "").strip()
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

  # i18n：中文 / 英文二选一至少填一组（标题 + 按钮文案），最终按语言存进 i18n
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

  zh, en = _lang("zh"), _lang("en")
  if not zh and not en:
    return None, "中文 / 英文文案至少填一种（标题 + 按钮文案）"
  if zh and not zh["action_text"]:
    return None, "中文按钮文案必填"
  if en and not en["action_text"]:
    return None, "英文按钮文案必填"
  i18n = {}
  if zh:
    i18n["zh-Hans"] = zh
  if en:
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
  if max_show == "invalid":
    return None, "最多展示次数必须是整数"
  if max_show is not None:
    display_rule["max_show_count"] = max_show
  # 冷却间隔：表单按小时填（可小数，如 0.5），落地 JSON 换成秒（display_rule.cooldown_seconds）
  raw_hours = (form.get("cooldown_hours") or "").strip()
  if raw_hours:
    try:
      cooldown_seconds = int(float(raw_hours) * 3600)
      if cooldown_seconds < 0:
        raise ValueError
    except ValueError:
      return None, "两次展示最小间隔必须是非负数字（小时）"
    display_rule["cooldown_seconds"] = cooldown_seconds
  popup["display_rule"] = display_rule
  return popup, None


def _lang_fields(prefix: str, meta: dict) -> str:
  """单一语言文案的字段组（不含 fieldset；放在中/英 tab 页里，二选一至少填一组）。"""
  styles = "".join(
    f'<option value="{s}"{" selected" if s == meta["badge_style"] else ""}>{s}</option>'
    for s in ("purple", "orange", "blue")
  )
  return f"""
  <p>左上角标签：<input type="text" name="{prefix}_badge" value="{_esc(meta['badge'])}" size="12">
  标签配色：<select name="{prefix}_badge_style">{styles}</select></p>
  <p>主标题：<input type="text" name="{prefix}_title" size="40"></p>
  <p>副标题：<input type="text" name="{prefix}_subtitle" size="60"></p>
  <p>按钮文案：<input type="text" name="{prefix}_action_text" size="20"></p>"""


# 发布页「预览」弹层：按当前表单内容渲染消息卡片；动作按钮遵照 action_type ——
# url 真跳转（新标签页）、survey 弹出问卷预览（逐题「下一道题」直到最后）、
# route 只提示（Web 无法跳 App 内路由）、dismiss 关闭。__SURVEYS__ 注入问卷内容。
_PREVIEW_BLOCK = """
<div id="preview-overlay" style="display:none;position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.55);z-index:100;overflow:auto">
  <div style="width:340px;margin:8vh auto;background:#fff;border-radius:14px;padding:22px;position:relative;box-shadow:0 8px 30px rgba(0,0,0,.3)">
    <span onclick="closePreview()" style="position:absolute;right:14px;top:10px;cursor:pointer;color:#999;font-size:16px">✕</span>
    <div id="preview-body"></div>
  </div>
</div>
<script>
const SURVEYS = __SURVEYS__;
const BADGE_COLORS = {purple: '#7c4dff', orange: '#ff8f00', blue: '#1e88e5'};
let _svQuestions = [], _svIdx = 0, _svTitle = '';

function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function closePreview() { document.getElementById('preview-overlay').style.display = 'none'; }

function _field(name) { const el = document.querySelector('[name="' + name + '"]'); return el ? el.value.trim() : ''; }

function previewPopup() {
  // 文案取当前表单里填了标题的那组语言（中文优先）
  let badge = _field('zh_badge'), badgeStyle = _field('zh_badge_style'), title = _field('zh_title'),
      subtitle = _field('zh_subtitle'), actionText = _field('zh_action_text');
  if (!title) {
    badge = _field('en_badge'); badgeStyle = _field('en_badge_style'); title = _field('en_title');
    subtitle = _field('en_subtitle'); actionText = _field('en_action_text');
  }
  if (!title) { alert('请先填写标题（中文或英文）'); return; }
  const t = curType();
  let img = _field('image_url');
  const f = document.querySelector('input[name="image_file"]').files[0];
  if (f) img = URL.createObjectURL(f);  // 未上传的本地文件也能预览
  let h = '';
  if (badge) h += '<div style="display:inline-block;background:' + (BADGE_COLORS[badgeStyle] || BADGE_COLORS.purple)
    + ';color:#fff;font-size:11px;padding:2px 8px;border-radius:4px;margin-bottom:8px">' + esc(badge) + '</div>';
  if (img) h += '<img src="' + esc(img) + '" style="width:100%;border-radius:8px;margin-bottom:10px">';
  h += '<h3 style="margin:4px 0">' + esc(title) + '</h3>';
  if (subtitle) h += '<p style="color:#666;font-size:13px;margin:4px 0 14px">' + esc(subtitle) + '</p>';
  h += '<button type="button" onclick="previewAction()" style="width:100%;padding:10px;border:none;border-radius:8px;background:#2f6fdd;color:#fff;font-size:15px;cursor:pointer">'
    + esc(actionText || '查看详情') + '</button>';
  document.getElementById('preview-body').innerHTML = h;
  document.getElementById('preview-overlay').style.display = 'block';
}

function previewAction() {
  const t = curType();
  if (t === 'survey') {
    const sid = _field('survey_id');
    const sv = SURVEYS[sid];
    if (!sv) { alert('请先选择问卷（survey_id）'); return; }
    const i18n = sv.i18n || {};
    const content = i18n['zh-Hans'] || i18n['en'] || Object.values(i18n)[0] || {};
    _svQuestions = content.questions || [];
    if (!_svQuestions.length) { alert('该问卷没有题目'); return; }
    _svIdx = 0;
    _svTitle = content.title || sid;
    renderSurveyQuestion();
    return;
  }
  if (t === 'mall') {
    const url = _field('url');
    if (url) window.open(url, '_blank'); else alert('请先填写链接 URL');
    return;
  }
  const a = document.getElementById('action_type').value;
  if (a === 'url') {
    const url = _field('ad_url');
    if (url) window.open(url, '_blank'); else alert('请先填写链接 URL');
  } else if (a === 'route') {
    alert('预览无法跳转 App 内路由：' + _field('route'));
  } else {
    closePreview();  // dismiss
  }
}

function renderSurveyQuestion() {
  const q = _svQuestions[_svIdx];
  const last = _svIdx === _svQuestions.length - 1;
  let h = '<div class="hint">' + esc(_svTitle) + ' · ' + (_svIdx + 1) + ' / ' + _svQuestions.length + '</div>'
    + '<h4 style="margin:6px 0">' + esc(q.title) + '</h4>';
  if (q.hint) h += '<p class="hint" style="margin:2px 0 10px">' + esc(q.hint) + '</p>';
  if (q.type === 'text') {
    h += '<textarea rows="3" style="width:100%;box-sizing:border-box" placeholder="' + esc(q.placeholder || '') + '"></textarea>';
  } else {
    const it = q.type === 'multi_choice' ? 'checkbox' : 'radio';
    (q.options || []).forEach(function (o) {
      h += '<p style="margin:6px 0"><label><input type="' + it + '" name="sv_q"> ' + esc(o.text) + '</label></p>';
    });
  }
  h += '<button type="button" onclick="' + (last ? 'closePreview()' : 'nextSurveyQuestion()')
    + '" style="width:100%;padding:10px;border:none;border-radius:8px;background:#2f6fdd;color:#fff;font-size:15px;cursor:pointer;margin-top:8px">'
    + (last ? '完成' : '下一道题') + '</button>';
  document.getElementById('preview-body').innerHTML = h;
  document.getElementById('preview-overlay').style.display = 'block';
}
function nextSurveyQuestion() { _svIdx++; renderSurveyQuestion(); }
</script>
"""


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
  surveys = meta.get("surveys") or {}  # 预览按钮按 survey_id 本地渲染题目流程

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
.tabs {{ margin: 4px 0 10px; }}
.tab {{ padding: 4px 14px; margin-right: 6px; border: 1px solid #bbb; background: #f4f4f4; cursor: pointer; }}
.tab.active {{ background: #2f6fdd; color: #fff; border-color: #2f6fdd; }}
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

<fieldset><legend>动作（用户点按钮后，与消息类型绑定）</legend>
  <div id="act-survey">
    <p>动作：<b>survey（打开问卷）</b>
    <span class="hint">type=survey 固定打开问卷，直接落进 JSON；该类型恒落站内消息</span></p>
    <p>问卷：<select name="survey_id">{survey_options}</select>
    <a href="/survey_edit" target="_blank">新建问卷</a></p>
  </div>
  <div id="act-mall">
    <p>动作：<b>url（打开网页）</b>
    <span class="hint">type=mall 固定打开网页，直接落进 JSON</span></p>
    <p>链接：<input type="url" name="url" size="50" value="{_esc(v.get('url', ''))}" placeholder="https://…"></p>
    <p><label><input type="checkbox" name="push_message" id="push_message"> 同步落一条站内消息</label></p>
  </div>
  <div id="act-ad">
    <p>动作：<select name="action_type" id="action_type" onchange="onActionChange()">
      <option value="dismiss">dismiss（仅关闭，客户端默认落商城 Tab）</option>
      <option value="route">route（App 内路由）</option>
      <option value="url">url（打开网页）</option>
    </select></p>
    <p id="act-route">路由：<select name="route">{route_options}</select>
    <span class="hint">客户端硬编码白名单，新路由须先发客户端版本</span></p>
    <p id="act-ad-url">链接：<input type="url" name="ad_url" size="50" value="{_esc(v.get('ad_url', ''))}" placeholder="https://…"></p>
    <p><label><input type="checkbox" name="push_message" id="push_message_ad"> 同步落一条站内消息</label></p>
  </div>
</fieldset>

<fieldset><legend>文案（中文 / English 二选一，至少填一组；最终 JSON 按语言存进 i18n）</legend>
  <p class="tabs">
    <button type="button" id="tab-zh" class="tab" onclick="showLang('zh')">中文文案</button>
    <button type="button" id="tab-en" class="tab" onclick="showLang('en')">English</button>
  </p>
  <div id="lang-zh">{_lang_fields("zh", _TYPE_META[(v.get('type') or 'survey')])}</div>
  <div id="lang-en">{_lang_fields("en", _TYPE_META[(v.get('type') or 'survey')])}</div>
</fieldset>

<fieldset><legend>展示规则与配图（可选）</legend>
  <p>最多展示次数：<input type="number" name="max_show_count" size="6" min="1">
  两次展示最小间隔（小时，可小数如 0.5）：<input type="number" name="cooldown_hours" size="6" min="0" step="0.5">
  <span class="hint">落地 JSON 里换成秒（cooldown_seconds）</span>
  <label><input type="checkbox" name="dismiss_stops" checked> 用户手动关闭后不再展示</label></p>
  <p>主图 URL：<input type="url" name="image_url" size="60"
     value="{_esc(v.get('image_url', ''))}" placeholder="约 590×286；留空用客户端按类型内置底图"></p>
  <p>或上传图片：<input type="file" name="image_file" accept="image/png,image/jpeg,image/webp">
  <span class="hint">png / jpg / webp，≤2MB；上传后存服务端并生成公网 URL（两者都填时以上传文件为准）</span></p>
</fieldset>
<p><button type="submit">发布</button>
<button type="button" onclick="previewPopup()">预览</button>
<span class="hint">预览不发布：按当前表单内容弹出演示弹窗</span></p>
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
  document.getElementById('act-mall').style.display = t === 'mall' ? '' : 'none';
  document.getElementById('act-ad').style.display = t === 'ad' ? '' : 'none';
  // mall / ad 各有一个同名 push_message 复选框（只显示一个），按类型默认值勾选
  document.getElementById('push_message').checked = PUSH_DEFAULT[t];
  document.getElementById('push_message_ad').checked = PUSH_DEFAULT[t];
}}
function onActionChange() {{
  const a = document.getElementById('action_type').value;
  document.getElementById('act-route').style.display = a === 'route' ? '' : 'none';
  document.getElementById('act-ad-url').style.display = a === 'url' ? '' : 'none';
}}
function showLang(p) {{
  document.getElementById('lang-zh').style.display = p === 'zh' ? '' : 'none';
  document.getElementById('lang-en').style.display = p === 'en' ? '' : 'none';
  document.getElementById('tab-zh').classList.toggle('active', p === 'zh');
  document.getElementById('tab-en').classList.toggle('active', p === 'en');
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
onTypeChange(); onActionChange(); showLang('zh'); checkDup();
</script>
"""
  return body + _PREVIEW_BLOCK.replace("__SURVEYS__", json.dumps(surveys, ensure_ascii=False))


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


# -------------------- 新建问卷（tanchuang_suvey.md 5. query_survey 结构） --------------------
#
# 页面是纯前端动态表单（新增题目/选项由 JS 加行），保存时 JS 把内容组装成
# {survey_id, i18n: {<language>: {title, questions[], reward?}}} —— 每个语言的内容
# 与 query_survey 响应 data 同构 —— 放进隐藏字段 survey_json 提交；
# 服务端只负责解析 + 转发 user_server /ops/save_survey（结构校验在那边 ops_config 里）。

def _survey_lang_editor(prefix: str) -> str:
  """单一语言的问卷编辑区（标题 + 题目动态行 + 奖励），zh / en 各一份，tab 切换。"""
  return (
    f'<p>问卷标题：<input type="text" id="{prefix}_title" size="50"></p>\n'
    f'<div id="{prefix}_questions"></div>\n'
    f'<p>新增题目（先选类型）：<select id="{prefix}_qtype">'
    f'<option value="single_choice">单选 single_choice</option>'
    f'<option value="multi_choice">多选 multi_choice</option>'
    f'<option value="text">文本填写 text</option></select>\n'
    f'<button type="button" onclick="addQuestion(\'{prefix}\')">+ 新增题目</button></p>\n'
    f'<fieldset><legend>奖励（可选，无礼品则不展示礼品领取页）</legend>\n'
    f'<p>礼品类型：<select id="{prefix}_gift_type" onchange="onGiftChange(\'{prefix}\')">'
    f'<option value="">无礼品</option>'
    f'<option value="physical">实体礼品 physical</option>'
    f'<option value="virtual">虚拟礼品 virtual</option></select></p>\n'
    f'<div id="{prefix}_gift" style="display:none">\n'
    f'<p>礼品名称：<input type="text" id="{prefix}_gift_name" size="30"></p>\n'
    f'<p>奖励说明（提交成功页文案）：<input type="text" id="{prefix}_gift_desc" size="60"></p>\n'
    f'<p id="{prefix}_gift_email_row" style="display:none">客服邮箱（实体礼品必填，客户端无兜底）：'
    f'<input type="email" id="{prefix}_contact_email" size="30"></p>\n'
    f'</div>\n</fieldset>\n'
  )


def _survey_edit_body(survey_ids: list) -> str:
  """新建问卷表单页 body（纯前端动态表单；__SURVEY_IDS__ 注入现有 ID 做查重/建议）。"""
  zh_editor = _survey_lang_editor("zh")
  en_editor = _survey_lang_editor("en")
  page = """
<style>
fieldset { margin: 12px 0; border: 1px solid #ccc; padding: 10px 14px; }
legend { font-weight: bold; }
.hint { color: #888; font-size: 12px; }
.tabs { margin: 4px 0 10px; }
.tab { padding: 4px 14px; margin-right: 6px; border: 1px solid #bbb; background: #f4f4f4; cursor: pointer; }
.tab.active { background: #2f6fdd; color: #fff; border-color: #2f6fdd; }
.question fieldset { border: 1px dashed #aaa; }
</style>
<form method="post" action="/survey_edit" onsubmit="return buildSurvey()">
<fieldset><legend>基本信息</legend>
  <p>survey_id：<input type="text" id="survey_id" size="32" required oninput="checkDup()"
     placeholder="如 sv_sleep_2026q3">
  <button type="button" onclick="suggestId()">生成建议</button>
  <span id="dupwarn" class="hint"></span><br>
  <span class="hint">稳定唯一；同一份问卷复用同一 ID，新问卷用新 ID（保存后不可改题，改题请新建 ID）</span></p>
</fieldset>

<fieldset><legend>问卷内容（中文 / English 二选一，至少填一种；最终 JSON 按语言存进 i18n）</legend>
  <p class="tabs">
    <button type="button" id="tab-zh" class="tab" onclick="showLang('zh')">中文</button>
    <button type="button" id="tab-en" class="tab" onclick="showLang('en')">English</button>
  </p>
  <div id="lang-zh">__ZH_EDITOR__</div>
  <div id="lang-en">__EN_EDITOR__</div>
</fieldset>
<input type="hidden" name="survey_json" id="survey_json">
<p><button type="submit">保存问卷</button></p>
</form>

<script>
const EXISTING_IDS = __SURVEY_IDS__;
const TYPE_LABEL = {single_choice: '单选', multi_choice: '多选', text: '文本'};

function showLang(p) {
  document.getElementById('lang-zh').style.display = p === 'zh' ? '' : 'none';
  document.getElementById('lang-en').style.display = p === 'en' ? '' : 'none';
  document.getElementById('tab-zh').classList.toggle('active', p === 'zh');
  document.getElementById('tab-en').classList.toggle('active', p === 'en');
}

function onGiftChange(p) {
  const gt = document.getElementById(p + '_gift_type').value;
  document.getElementById(p + '_gift').style.display = gt ? '' : 'none';
  document.getElementById(p + '_gift_email_row').style.display = gt === 'physical' ? '' : 'none';
}

function addQuestion(p) {
  const type = document.getElementById(p + '_qtype').value;
  const div = document.createElement('div');
  div.className = 'question';
  div.dataset.type = type;
  let inner = '<fieldset><legend>题目（' + TYPE_LABEL[type] + ' ' + type + '） '
    + '<button type="button" onclick="this.closest(\\'div.question\\').remove()">删除</button></legend>'
    + '<p>题干：<input type="text" class="q-title" size="60"></p>'
    + '<p>提示（灰色小字，可选）：<input type="text" class="q-hint" size="60"></p>';
  if (type === 'text') {
    inner += '<p>占位提示：<input type="text" class="q-placeholder" size="30"> '
      + '<label><input type="checkbox" class="q-required"> 必答</label></p>';
  } else {
    inner += '<div class="options"></div>'
      + '<p><button type="button" onclick="addOption(this)">+ 新增选项</button></p>';
  }
  inner += '</fieldset>';
  div.innerHTML = inner;
  document.getElementById(p + '_questions').appendChild(div);
  if (type !== 'text') {  // 选择题先给两个空选项，不够再点「新增选项」
    const btn = div.querySelector('.options').parentElement.querySelector('button');
    addOption(btn); addOption(btn);
  }
}

function addOption(btn) {
  const opts = btn.closest('div.question').querySelector('.options');
  const row = document.createElement('p');
  row.className = 'option';
  row.innerHTML = '选项：<input type="text" class="o-text" size="40"> '
    + '<button type="button" onclick="this.parentElement.remove()">删除</button>';
  opts.appendChild(row);
}

function buildLang(p) {
  // 标题为空视为该语言不填写（不下发）；填了标题则收集全部题目行
  const title = document.getElementById(p + '_title').value.trim();
  if (!title) return null;
  const questions = [];
  document.querySelectorAll('#' + p + '_questions .question').forEach(function (q, i) {
    const item = {
      question_id: 'q' + (i + 1),
      index: i + 1,
      type: q.dataset.type,
      title: q.querySelector('.q-title').value.trim(),
    };
    const hint = q.querySelector('.q-hint').value.trim();
    if (hint) item.hint = hint;
    if (item.type === 'text') {
      const ph = q.querySelector('.q-placeholder').value.trim();
      if (ph) item.placeholder = ph;
      item.required = q.querySelector('.q-required').checked;
    } else {
      item.options = [];
      q.querySelectorAll('.option').forEach(function (o, j) {
        item.options.push({option_id: 'o' + (j + 1), text: o.querySelector('.o-text').value.trim()});
      });
    }
    questions.push(item);
  });
  const content = {title: title, questions: questions};
  const gt = document.getElementById(p + '_gift_type').value;
  if (gt) {
    content.reward = {gift_type: gt, gift_name: document.getElementById(p + '_gift_name').value.trim()};
    const desc = document.getElementById(p + '_gift_desc').value.trim();
    if (desc) content.reward.desc = desc;
    if (gt === 'physical') {
      content.reward.contact_email = document.getElementById(p + '_contact_email').value.trim();
    }
  }
  return content;
}

function buildSurvey() {
  const i18n = {};
  const zh = buildLang('zh'); if (zh) i18n['zh-Hans'] = zh;
  const en = buildLang('en'); if (en) i18n['en'] = en;
  if (!Object.keys(i18n).length) {
    alert('中文 / English 至少填一种（问卷标题 + 题目）');
    return false;
  }
  const survey = {survey_id: document.getElementById('survey_id').value.trim(), i18n: i18n};
  document.getElementById('survey_json').value = JSON.stringify(survey);
  return true;
}

function suggestId() {
  const d = new Date();
  const ymd = String(d.getFullYear()) + String(d.getMonth() + 1).padStart(2, '0');
  for (let n = 1; n < 100; n++) {
    const id = 'sv_' + ymd + '_' + String(n).padStart(2, '0');
    if (!EXISTING_IDS.includes(id)) { document.getElementById('survey_id').value = id; checkDup(); return; }
  }
}
function checkDup() {
  const id = document.getElementById('survey_id').value;
  document.getElementById('dupwarn').textContent =
    EXISTING_IDS.includes(id) ? '⚠ 该 survey_id 已存在，保存会被拒绝' : '';
}
showLang('zh'); checkDup();
</script>
"""
  return (page
          .replace("__ZH_EDITOR__", zh_editor)
          .replace("__EN_EDITOR__", en_editor)
          .replace("__SURVEY_IDS__", json.dumps(survey_ids)))


async def handle_survey_edit_get(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny
  meta_result = await asyncio.to_thread(
    _user_server_call, "/ops/popup_meta", {"jwt_token": session["jwt"]},
  )
  meta = (meta_result.get("data") or {}) if meta_result.get("code") == 0 else {}
  return _html_response("新建问卷", _survey_edit_body(meta.get("survey_ids") or []), session)


async def handle_survey_edit_post(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  form = await request.post()
  raw = form.get("survey_json") or ""
  try:
    survey = json.loads(raw)
  except json.JSONDecodeError as e:
    return _html_response("新建问卷", f'<div class="err">JSON 解析失败：{_esc(e)}</div>', session, status=400)

  result = await asyncio.to_thread(
    _user_server_call, "/ops/save_survey", {"jwt_token": session["jwt"], "survey": survey},
  )
  if result.get("code") == 0:
    tip = (f'<div class="msg">问卷已保存：{_esc(result.get("msg"))}'
           f'（<a href="/survey_list">问卷列表</a> 查看；<a href="/publish">去发布 survey 类型消息</a>，问卷下拉里即可选到）</div>')
    logging.info("ops survey saved by %s: %s", session["email"],
                 survey.get("survey_id") if isinstance(survey, dict) else None)
    return _html_response("新建问卷", tip + '<p><a href="/survey_edit">再建一份</a></p>', session)

  tip = f'<div class="err">保存失败：{_esc(result.get("msg"))}</div>'
  logging.info("ops survey save failed by %s: code=%s", session["email"], result.get("code"))
  # 动态表单状态无法回填，失败时给出可编辑 JSON 供修正后重提
  body = tip + f"""
<form method="post" action="/survey_edit">
  <textarea name="survey_json" rows="24" style="width:100%">{html.escape(raw)}</textarea>
  <p><button type="submit">修正后重新保存</button></p>
</form>"""
  return _html_response("新建问卷", body, session, status=400)


# -------------------- 问卷列表（按创建时间筛选，点 ID 下方展开内容） --------------------

_QTYPE_LABELS = {"single_choice": "单选", "multi_choice": "多选", "text": "文本"}


def _fmt_ts(ts) -> str:
  if not ts:
    return "—"
  return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def _render_survey_detail(survey: dict) -> str:
  """问卷内容只读展示：按语言分组，题目（类型/题干/选项/占位/必答）+ 奖励。"""
  parts = [f'<h3>问卷内容：{_esc(survey.get("survey_id"))}'
           f' <small class="hint">创建时间 {_fmt_ts(survey.get("created_at"))}</small></h3>']
  i18n = survey.get("i18n") or {}
  if not i18n:
    parts.append('<p class="hint">（无内容）</p>')
  for lang, content in i18n.items():
    parts.append(f'<fieldset><legend>{_esc(lang)}：{_esc((content or {}).get("title") or "")}</legend><ol>')
    for q in (content or {}).get("questions") or []:
      qtype = _QTYPE_LABELS.get(q.get("type"), q.get("type") or "?")
      line = f'<li><p><b>{_esc(q.get("title"))}</b> <span class="hint">[{_esc(qtype)}]</span>'
      if q.get("hint"):
        line += f'<br><span class="hint">{_esc(q["hint"])}</span>'
      if q.get("type") == "text":
        meta = []
        if q.get("placeholder"):
          meta.append(f'占位：{_esc(q["placeholder"])}')
        meta.append("必答" if q.get("required") else "选答")
        line += f'<br><span class="hint">{"；".join(meta)}</span>'
      else:
        line += "<ul>" + "".join(
          f'<li>{_esc(o.get("text"))} <span class="hint">({_esc(o.get("option_id"))})</span></li>'
          for o in q.get("options") or []
        ) + "</ul>"
      parts.append(line + "</p></li>")
    parts.append("</ol>")
    reward = (content or {}).get("reward")
    if reward and reward.get("gift_type") in ("physical", "virtual"):
      line = f'<p>奖励：{_esc(reward.get("gift_name"))}（{reward["gift_type"]}）'
      if reward.get("desc"):
        line += f'<br><span class="hint">{_esc(reward["desc"])}</span>'
      if reward.get("contact_email"):
        line += f'<br><span class="hint">客服邮箱：{_esc(reward["contact_email"])}</span>'
      parts.append(line + "</p>")
    else:
      parts.append('<p class="hint">无礼品</p>')
    parts.append("</fieldset>")
  return "".join(parts)


async def handle_survey_list(request: web.Request) -> web.Response:
  session = _get_session(request)
  deny = _require_ops(session)
  if deny:
    return deny

  q = request.query
  start_at, end_at = _ts_field(q, "start_local"), _ts_field(q, "end_local")
  show = (q.get("show") or "").strip()

  result = await asyncio.to_thread(_user_server_call, "/ops/survey_list", {"jwt_token": session["jwt"]})
  surveys = (result.get("data") or {}).get("surveys") or [] if result.get("code") == 0 else []

  tip = ""
  if result.get("code") != 0:
    tip = f'<div class="err">拉取问卷列表失败：{_esc(result.get("msg"))}</div>'
  elif "invalid" in (start_at, end_at):
    tip = '<div class="err">时间格式不正确，未按时间筛选</div>'
    start_at = end_at = None
  # 按创建时间筛选；老配置缺 created_at 的问卷在时间筛选时不显示（无法判断归属）
  if isinstance(start_at, int):
    surveys = [s for s in surveys if s.get("created_at") and s["created_at"] >= start_at]
  if isinstance(end_at, int):
    surveys = [s for s in surveys if s.get("created_at") and s["created_at"] <= end_at]

  def _keep_params(sid: str) -> str:
    params = f"show={sid}"
    for k in ("start_local", "end_local"):
      if q.get(k):
        params += f"&{k}={q.get(k)}"
    return params

  rows = []
  for s in surveys:
    sid = s.get("survey_id") or ""
    i18n = s.get("i18n") or {}
    zh = i18n.get("zh-Hans") or {}
    en = i18n.get("en") or {}
    reward = zh.get("reward") or en.get("reward") or {}
    rows.append(
      f'<tr><td><a href="/survey_list?{_keep_params(sid)}">{_esc(sid)}</a></td>'
      f'<td>{_fmt_ts(s.get("created_at"))}</td>'
      f'<td>{_esc(zh.get("title") or "—")}</td><td>{_esc(en.get("title") or "—")}</td>'
      f'<td>{len(zh.get("questions") or [])}</td><td>{_esc(reward.get("gift_type") or "none")}</td></tr>'
    )
  body = tip + f"""
<form method="get" action="/survey_list">
  <p>创建时间：从 <input type="datetime-local" name="start_local" value="{_esc(q.get('start_local') or '')}">
  到 <input type="datetime-local" name="end_local" value="{_esc(q.get('end_local') or '')}">
  <button type="submit">筛选</button> <a href="/survey_list">重置</a>
  <span class="hint">老配置里没记录创建时间的问卷，在时间筛选时不显示</span></p>
</form>
<p>共 {len(surveys)} 份问卷 | <a href="/survey_edit">新建问卷</a></p>
<table border="1" cellpadding="6" cellspacing="0">
  <tr><th>survey_id（点击查看内容）</th><th>创建时间</th><th>中文标题</th><th>英文标题</th><th>题数</th><th>礼品</th></tr>
  {''.join(rows) or '<tr><td colspan="6">（无）</td></tr>'}
</table>
"""
  shown = next((s for s in surveys if s.get("survey_id") == show), None)
  if shown:
    body += "<hr>" + _render_survey_detail(shown)
  elif show:
    body += f'<hr><p class="hint">问卷 {_esc(show)} 不在当前筛选结果里（可能已被时间条件排除）</p>'
  return _html_response("问卷列表", body, session)


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

# -------------------- 洞察规则阈值（对照规范 v3 §4，运营可改） --------------------

_RULE_KEY_DOC = {
  "data_state": "数据状态机门槛：baseline7_min_nights(近7天≥N晚开近期基线) / baseline30_min_nights(近30天≥N晚开长期基线)",
  "onset": "入睡洞察：delta_stable_min(昨晚SOL与近7日基线相差±N分钟内视为稳定)",
  "structure": "结构洞察：minor_delta_pct(阶段Δ%超过N为轻微变化) / major_delta_pct(≥2项超过N为明显变化)",
  "fluctuation": "波动洞察：awake_min_minutes(觉醒≥N分钟计为事件) / list_max(逐条展示上限) / expand_max(汇总时最多展开条数) / intervention_window_min(觉醒后N分钟内配对设备干预)",
  "scene": "场景洞察：assoc_min_uses_7d(近7天≥N次才给关联描述) / assoc_min_uses_30d(近30天≥N次)",
  "advice": "睡眠建议：max_per_day(每日最多条数) / same_type_cooldown_days(同类建议冷却) / pattern_min_nights(连续模式最少晚数) / history_max_entries(历史上限)",
  "home_summary": "首页摘要：theme_cooldown_days(同主题不连续出现天数)",
  "trend": "周/月趋势：max_items_7d/max_items_30d(最多条数) / min_valid_7d/min_valid_30d(有效夜晚门槛) / min_change(各指标最小可报告变化)",
  "indices": "展示指数：onset/structure/stability 权重(缺失子分按剩余权重归一) / label_bands(产品状态分层) / subscore_params(子分公式常量)",
  "forbidden_terms": "洞察文案禁用词（逗号分隔；规则模板与 LLM 润色输出共同校验）",
  "polish_max_chars": "LLM 润色单字段长度上限（字符）",
}


def _rule_leaf_html(prefix: str, value) -> str:
  name = f"rule:{prefix}"
  label = prefix.split(".")[-1]
  if isinstance(value, bool):
    opts = "".join(
      f'<option value="{v}"{" selected" if value == v else ""}>{v}</option>'
      for v in (True, False))
    return f'<tr><td><code>{html.escape(label)}</code></td><td><select name="{name}">{opts}</select></td></tr>'
  if isinstance(value, (int, float)):
    return f'<tr><td><code>{html.escape(label)}</code></td><td><input type="number" step="any" name="{name}" value="{value}" style="width:120px"></td></tr>'
  if isinstance(value, list):
    return (f'<tr><td><code>{html.escape(label)}</code></td>'
            f'<td><textarea name="{name}" rows="2">{html.escape(", ".join(str(x) for x in value))}</textarea></td></tr>')
  return f'<tr><td><code>{html.escape(label)}</code></td><td><input name="{name}" value="{html.escape(str(value))}" style="width:320px"></td></tr>'


def _rules_group_html(prefix: str, value) -> str:
  if not isinstance(value, dict):
    return _rule_leaf_html(prefix, value)
  legend = prefix or "rules"
  doc = _RULE_KEY_DOC.get(legend, "")
  rows = "".join(_rules_group_html(f"{prefix}.{k}" if prefix else k, v) for k, v in value.items())
  return (f'<fieldset style="margin:14px 0;padding:10px;border:1px solid #ddd">'
          f'<legend><b>{html.escape(legend)}</b></legend>'
          f'<p style="margin:4px 0;color:#666;font-size:12px">{html.escape(doc)}</p>'
          f'<table>{rows}</table></fieldset>')


def _parse_rule_leaf(raw: str):
  raw = raw.strip()
  if "," in raw:
    return [x.strip() for x in raw.split(",") if x.strip()]
  for cast in (int, float):
    try:
      return cast(raw)
    except ValueError:
      continue
  if raw.lower() in ("true", "false"):
    return raw.lower() == "true"
  return raw


def _form_to_rules(form) -> dict:
  rules: dict = {}
  for key, raw in form.items():
    if not key.startswith("rule:"):
      continue
    node = rules
    parts = key[5:].split(".")
    for p in parts[:-1]:
      node = node.setdefault(p, {})
    node[parts[-1]] = _parse_rule_leaf(raw)
  return rules


async def handle_insight_rules_get(request: web.Request, tip: str = "", err: str = "") -> web.Response:
  session = _get_session(request)
  blocked = _require_ops(session)
  if blocked:
    return blocked
  result = await asyncio.to_thread(_user_server_call, "/ops/insight_rules", {"jwt_token": session["jwt"]})
  if result.get("code") != 0:
    body = f'<p class="err">加载失败：{html.escape(str(result.get("msg")))}</p>'
    return _html_response("洞察阈值", body, session)
  effective = (result.get("data") or {}).get("effective") or {}
  status_line = (f'<p><small>配置文件：{html.escape(str((result.get("data") or {}).get("path")))}'
                 f'（{"存在" if (result.get("data") or {}).get("exists") else "缺失，当前为内置默认"}；'
                 f'保存后按 mtime 热加载，无需重启）</small></p>')
  tip_html = f'<p class="msg">{html.escape(tip)}</p>' if tip else ""
  err_html = f'<p class="err">{html.escape(err)}</p>' if err else ""
  body = f"""{status_line}{tip_html}{err_html}
<p>阈值口径见 <code>Mindora_App睡眠数据展示与分析对照规范_v3.md</code> §4（标注「建议v1/待产品确认」的数值均在此调整）。
只改要调的项即可，未提交的项沿用当前生效值；所有字段提交时按 key 深度合并后整体保存。</p>
<form method="post" action="/insight_rules">
{_rules_group_html("", effective)}
<p><button type="submit">保存（热加载生效）</button></p>
</form>"""
  return _html_response("洞察阈值", body, session)


async def handle_insight_rules_post(request: web.Request) -> web.Response:
  session = _get_session(request)
  blocked = _require_ops(session)
  if blocked:
    return blocked
  form = await request.post()
  rules = _form_to_rules(form)
  if not rules:
    return await handle_insight_rules_get(request, err="表单为空")
  result = await asyncio.to_thread(
    _user_server_call, "/ops/save_insight_rules",
    {"jwt_token": session["jwt"], "rules": rules},
  )
  if result.get("code") == 0:
    return await handle_insight_rules_get(request, tip=f"已保存：{result.get('msg')}")
  return await handle_insight_rules_get(request, err=f"保存失败：{result.get('msg')}")


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
  app.router.add_get("/survey_edit", handle_survey_edit_get)
  app.router.add_post("/survey_edit", handle_survey_edit_post)
  app.router.add_get("/survey_list", handle_survey_list)
  app.router.add_get("/publish_logs", handle_publish_logs)
  app.router.add_get("/insight_rules", handle_insight_rules_get)
  app.router.add_post("/insight_rules", handle_insight_rules_post)
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
