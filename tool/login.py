import argparse
import json
import os
import sys
import time
import uuid
from enum import StrEnum

import requests

# DEFAULT_BASE_URL = os.getenv("AUTH_SERVER_URL", "http://127.0.0.1:9103/auth")
DEFAULT_BASE_URL = os.getenv("AUTH_SERVER_URL", "https://api.mindora316.com/auth")


class AuthRequestType(StrEnum):
  SEND_VERIFY_CODE = "send_verify_code"
  LOGIN_WITH_EMAIL_VERIFY_CODE = "login_with_email_verify_code"
  LOGIN_WITH_JWT = "login_with_jwt"
  DELETE_USER = "delete_user"
  LOGIN_WITH_EMAIL_PASSWORD = "login_with_email_password"


def print_step(message: str):
  print(f"\n{'=' * 20} {message} {'=' * 20}")


class AuthClient:
  def __init__(self, base_url: str, email: str, device_id: str | None = None, timeout: int = 15):
    self.base_url = base_url
    self.email = email
    self.device_id = uuid.UUID(device_id) if device_id else uuid.uuid4()
    self.timeout = timeout
    self.session = requests.Session()
    self.session.trust_env = False

  def send_verify_code(self) -> requests.Response:
    request = self._build_request(
      AuthRequestType.SEND_VERIFY_CODE,
      {"email": self.email, "device_id": str(self.device_id)},
    )
    return self._post(request)

  def login_with_verify_code(self, verify_code: str) -> tuple[requests.Response, dict]:
    request = self._build_request(
      AuthRequestType.LOGIN_WITH_EMAIL_VERIFY_CODE,
      {
        "email": self.email,
        "device_id": str(self.device_id),
        "verify_code": verify_code,
      },
    )
    response = self._post(request)
    return response, self._parse_auth_response(response)

  def login_with_email_password(self, password: str) -> tuple[requests.Response, dict]:
    request = self._build_request(
      AuthRequestType.LOGIN_WITH_EMAIL_PASSWORD,
      {
        "email": self.email,
        "password": password,
        "device_id": str(self.device_id),
      },
    )
    response = self._post(request)
    return response, self._parse_auth_response(response)

  def login_with_jwt(self, jwt_token: str) -> requests.Response:
    request = self._build_request(AuthRequestType.LOGIN_WITH_JWT, {"jwt_token": jwt_token})
    return self._post(request)

  def delete_user(self, jwt_token: str) -> requests.Response:
    request = self._build_request(AuthRequestType.DELETE_USER, {"jwt_token": jwt_token})
    return self._post(request)

  def _post(self, request: dict) -> requests.Response:
    return self.session.post(self.base_url, json=request, timeout=self.timeout)

  @staticmethod
  def _parse_auth_response(response: requests.Response) -> dict:
    try:
      return response.json()
    except ValueError:
      return {
        "code": response.status_code,
        "msg": "non-json response",
        "raw_text": response.text,
      }

  @staticmethod
  def _build_request(request_type: AuthRequestType, data: dict) -> dict:
    return {
      "request_type": request_type.value,
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": data,
    }


def print_response(label: str, response: requests.Response):
  print_step(label)
  print(f"status_code: {response.status_code}")
  try:
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
  except ValueError:
    print(response.text)


def run_verify_code_flow(client: AuthClient, verify_code: str | None, delete_after: bool):
  send_response = client.send_verify_code()
  print_response("步骤 1: 发送验证码", send_response)

  if not verify_code:
    verify_code = input("\n请输入服务端控制台显示的验证码: ").strip()

  login_response, auth_response = client.login_with_verify_code(verify_code)
  print_response("步骤 2: 邮箱验证码登录", login_response)

  jwt_token = ((auth_response.get("data") or {}).get("token"))
  if not jwt_token:
    return

  jwt_response = client.login_with_jwt(jwt_token)
  print_response("步骤 3: JWT 登录验证", jwt_response)

  if delete_after:
    delete_response = client.delete_user(jwt_token)
    print_response("步骤 4: 删除用户", delete_response)


def run_password_login(client: AuthClient, password: str):
  login_response, auth_response = client.login_with_email_password(password)
  print_response("邮箱密码登录", login_response)

  jwt_token = ((auth_response.get("data") or {}).get("token"))
  if jwt_token:
    jwt_response = client.login_with_jwt(jwt_token)
    print_response("JWT 登录验证", jwt_response)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Client for auth_server email login flows")
  parser.add_argument("email", nargs="?", help="email for auth flow")
  parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
  parser.add_argument("--device-id", default=None, help="reuse a fixed UUID for repeated verification tests")
  parser.add_argument("--timeout", type=int, default=15)
  parser.add_argument(
    "--mode",
    choices=["verify_code", "password"],
    default="verify_code",
    help="email login flow to test",
  )
  parser.add_argument("--verify-code", default=None, help="skip prompt and use this verification code directly")
  parser.add_argument("--password", default=None, help="password for email+password login")
  parser.add_argument("--delete-after", action="store_true", help="delete the user after a successful verify-code flow")
  return parser


def main():
  args = build_parser().parse_args()
  email = args.email or os.getenv("TEST_EMAIL")
  if not email:
    print("Usage: python3 tool/login.py <email> [--mode verify_code|password]")
    sys.exit(1)

  client = AuthClient(
    base_url=args.base_url,
    email=email,
    device_id=args.device_id,
    timeout=args.timeout,
  )
  print(f"base_url: {args.base_url}")
  print(f"email: {email}")
  print(f"device_id: {client.device_id}")

  if args.mode == "password":
    if not args.password:
      print("--password is required when --mode password")
      sys.exit(1)
    run_password_login(client, args.password)
    return

  run_verify_code_flow(client, args.verify_code, args.delete_after)


if __name__ == "__main__":
  try:
    main()
  except requests.exceptions.RequestException as exc:
    print(f"连接失败: {exc}")
    sys.exit(1)
  except KeyboardInterrupt:
    print("\n已取消")
    sys.exit(130)
