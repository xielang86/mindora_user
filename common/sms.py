"""
SMS verification code service.

• DEV (no ALIYUN_ACCESS_KEY_ID in .env): prints code to console.
• PROD: calls Alibaba Cloud SMS API (阿里云短信服务).

Docs: https://help.aliyun.com/document_detail/101414.html
"""
import os
import json
import hmac
import base64
import hashlib
import logging
import uuid as _uuid
from datetime import datetime, timezone
from urllib.parse import quote

logger = logging.getLogger(__name__)

_ALIYUN_ENDPOINT = "https://dysmsapi.aliyuncs.com"


def send_verify_code_via_sms(phone: str, code: str) -> dict:
  """
  发送短信验证码。
  若未配置阿里云密钥则 mock（控制台打印）。
  Returns: {"code": 0, "msg": "..."} or {"code": 500, "msg": "..."}
  """
  access_key_id = os.getenv("ALIYUN_ACCESS_KEY_ID", "")
  access_key_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "")

  if not access_key_id:
    # ── Dev mock ────────────────────────────────────────────────────────
    logger.info("[DEV SMS] To: %s | Code: %s", phone, code)
    print(f"\n{'='*50}")
    print(f"  📱 SMS (dev mock)")
    print(f"  To:   {phone}")
    print(f"  Code: {code}")
    print(f"{'='*50}\n")
    return {"code": 0, "msg": "验证码发送成功（mock）"}

  try:
    result = _call_aliyun(phone, code, access_key_id, access_key_secret)
    if result.get("Code") == "OK":
      logger.info("SMS sent to %s", phone)
      return {"code": 0, "msg": "验证码发送成功"}
    err_code = result.get("Code", "UNKNOWN")
    logger.error("Aliyun SMS error: %s", result)
    return {"code": 500, "msg": f"短信发送失败：{err_code}"}
  except Exception as e:
    logger.error("SMS exception for %s: %s", phone, e)
    return {"code": 500, "msg": f"短信服务异常：{str(e)}"}


def _call_aliyun(phone: str, code: str, key_id: str, key_secret: str) -> dict:
  """HMAC-SHA1 signed GET request to Aliyun SMS API."""
  import httpx

  sign_name = os.getenv("ALIYUN_SMS_SIGN_NAME", "第七生命")
  tpl_code = os.getenv("ALIYUN_SMS_TEMPLATE_CODE", "")

  params = {
    "SignatureMethod": "HMAC-SHA1",
    "SignatureNonce": str(_uuid.uuid4()),
    "SignatureVersion": "1.0",
    "AccessKeyId": key_id,
    "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "Format": "JSON",
    "Action": "SendSms",
    "Version": "2017-05-25",
    "RegionId": "cn-hangzhou",
    "PhoneNumbers": phone,
    "SignName": sign_name,
    "TemplateCode": tpl_code,
    "TemplateParam": json.dumps({"code": code}),
  }

  query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(params.items()))
  string_to_sign = f"GET&{_enc('/')}&{_enc(query)}"
  sig = base64.b64encode(
    hmac.new((key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
  ).decode()
  params["Signature"] = sig

  resp = httpx.get(_ALIYUN_ENDPOINT, params=params, timeout=10)
  return resp.json()


def _enc(s: str) -> str:
  return quote(str(s), safe="")
