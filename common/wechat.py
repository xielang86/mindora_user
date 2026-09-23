"""
WeChat Open Platform OAuth 2.0 — PC 扫码登录.

Flow:
  1. Frontend: open qrcode_url in <iframe> or as QR image
  2. User scans with WeChat app → WeChat redirects to WECHAT_REDIRECT_URI?code=xxx&state=yyy
  3. Client sends POST /auth  with request_type=wechat_callback & wechat_code=xxx
  4. Server calls exchange_code() → get_user_info() → upsert user → return JWT

Docs: https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Wechat_Login.html
"""
import os
import secrets
import logging
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

_AUTH_URL      = "https://open.weixin.qq.com/connect/qrconnect"
_TOKEN_URL     = "https://api.weixin.qq.com/sns/oauth2/access_token"
_USERINFO_URL  = "https://api.weixin.qq.com/sns/userinfo"


def is_wechat_enabled() -> bool:
  return bool(os.getenv("WECHAT_APPID") and os.getenv("WECHAT_SECRET"))


def get_qrcode_url() -> tuple[str, str]:
  """
  Build WeChat QR-code page URL and a random state token.
  Embed the returned URL in an <iframe> on the frontend.
  Returns (qrcode_url, state).
  """
  appid       = os.getenv("WECHAT_APPID", "")
  redirect    = os.getenv("WECHAT_REDIRECT_URI", "")
  state       = secrets.token_urlsafe(16)
  params = {
    "appid":         appid,
    "redirect_uri":  redirect,
    "response_type": "code",
    "scope":         "snsapi_login",
    "state":         state,
  }
  url = _AUTH_URL + "?" + urlencode(params) + "#wechat_redirect"
  return url, state


def exchange_code(code: str) -> dict:
  """
  Exchange OAuth code for access_token + openid (+ unionid if bound).
  Returns full token dict from WeChat.
  Raises ValueError on WeChat error.
  """
  resp = httpx.get(
    _TOKEN_URL,
    params={
      "appid":      os.getenv("WECHAT_APPID", ""),
      "secret":     os.getenv("WECHAT_SECRET", ""),
      "code":       code,
      "grant_type": "authorization_code",
    },
    timeout=10,
  )
  data = resp.json()
  if "errcode" in data and data["errcode"] != 0:
    raise ValueError(f"WeChat token error {data['errcode']}: {data.get('errmsg')}")
  return data   # keys: access_token, openid, scope, unionid (optional)


def get_user_info(access_token: str, openid: str) -> dict:
  """
  Fetch WeChat user profile.
  Returns dict with: openid, nickname, headimgurl, unionid (optional).
  Raises ValueError on WeChat error.
  """
  resp = httpx.get(
    _USERINFO_URL,
    params={"access_token": access_token, "openid": openid, "lang": "zh_CN"},
    timeout=10,
  )
  data = resp.json()
  if "errcode" in data and data["errcode"] != 0:
    raise ValueError(f"WeChat userinfo error {data['errcode']}: {data.get('errmsg')}")
  return data
