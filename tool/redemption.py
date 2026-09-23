import argparse
import datetime as dt
import json
import os
import time
from typing import Any

import requests


DEFAULT_BASE_URL = os.getenv("AUTH_SERVER_URL", "http://127.0.0.1:9103/auth")
DEFAULT_ADMIN_SECRET = os.getenv("REDEMPTION_ADMIN_SECRET", "")
DEFAULT_LANGUAGE = os.getenv("USER_SERVER_LANGUAGE", "zh-Hans")


class RedemptionClient:
  def __init__(
    self,
    base_url: str = DEFAULT_BASE_URL,
    admin_secret: str = DEFAULT_ADMIN_SECRET,
    jwt_token: str = "",
    timeout: int = 30,
  ):
    self.base_url = base_url.rstrip("/")
    self.admin_secret = admin_secret
    self.jwt_token = jwt_token
    self.timeout = timeout
    self.session = requests.Session()
    self.session.trust_env = False

  def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
    url = self.base_url
    try:
      response = self.session.post(url, json=payload, timeout=self.timeout)
      try:
        body: Any = response.json()
      except ValueError:
        body = response.text

      return {
        "status_code": response.status_code,
        "ok": response.ok,
        "url": url,
        "request": payload,
        "response": body,
      }
    except requests.exceptions.RequestException as exc:
      return {
        "status_code": None,
        "ok": False,
        "url": url,
        "request": payload,
        "response": {"error": str(exc)},
      }

  def generate_codes(
    self,
    batch_id: str,
    target_level: str,
    duration_days: int,
    quantity: int,
    expire_at: str | None = None,
  ) -> dict[str, Any]:
    """Admin endpoint: generate a batch of unbound redemption codes."""
    payload = {
      "request_type": "generate_redemption_codes",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {
        "admin_secret": self.admin_secret,
        "batch_id": batch_id,
        "target_level": target_level,
        "duration_days": duration_days,
        "quantity": quantity,
      },
    }
    if expire_at:
      payload["data"]["code_expire_at"] = expire_at
    return self._post(payload)

  def redeem_code(self, redemption_code: str, jwt_token: str | None = None) -> dict[str, Any]:
    """User endpoint: redeem a code with JWT."""
    token = jwt_token or self.jwt_token
    if not token:
      raise ValueError("jwt_token is required for redeem")

    payload = {
      "request_type": "redeem_redemption_code",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {
        "jwt_token": token,
        "redemption_code": redemption_code,
      },
    }
    return self._post(payload)


def print_result(title: str, result: Any):
  print(f"\n{'=' * 20} {title} {'=' * 20}")
  print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Test client for auth_server redemption code APIs")

  common = argparse.ArgumentParser(add_help=False)
  common.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Auth server URL")
  common.add_argument("--admin-secret", default=DEFAULT_ADMIN_SECRET, help="Admin secret for generating codes")
  common.add_argument("--jwt-token", default=os.getenv("JWT_TOKEN", ""), help="User JWT for redeeming codes")
  common.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")

  subparsers = parser.add_subparsers(dest="action", required=True)

  gen_parser = subparsers.add_parser(
    "generate",
    parents=[common],
    help="Generate a batch of redemption codes",
  )
  gen_parser.add_argument("--batch-id", required=True, help="Batch ID")
  gen_parser.add_argument("--target-level", default="premium", choices=["free", "pro", "premium"], help="Target user level")
  gen_parser.add_argument("--duration-days", type=int, default=30, help="Membership duration in days")
  gen_parser.add_argument("--quantity", type=int, default=10, help="Number of codes to generate")
  gen_parser.add_argument("--expire-at", default=None, help="Code expiration time (ISO 8601)")

  redeem_parser = subparsers.add_parser(
    "redeem",
    parents=[common],
    help="Redeem a code with JWT",
  )
  redeem_parser.add_argument("--code", required=True, help="Redemption code")

  return parser


def main():
  args = build_parser().parse_args()

  client = RedemptionClient(
    base_url=args.base_url,
    admin_secret=args.admin_secret,
    jwt_token=args.jwt_token,
    timeout=args.timeout,
  )

  if args.action == "generate":
    result = client.generate_codes(
      batch_id=args.batch_id,
      target_level=args.target_level,
      duration_days=args.duration_days,
      quantity=args.quantity,
      expire_at=args.expire_at,
    )
  elif args.action == "redeem":
    result = client.redeem_code(args.code)
  else:
    raise ValueError(f"Unknown action: {args.action}")

  print_result(args.action, result)


if __name__ == "__main__":
  main()
