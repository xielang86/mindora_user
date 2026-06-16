import argparse
import datetime as dt
import json
import os
import time
from typing import Any

import requests


DEFAULT_BASE_URL = os.getenv("USER_SERVER_URL", "http://127.0.0.1:9001")
DEFAULT_LANGUAGE = os.getenv("USER_SERVER_LANGUAGE", "zh-Hans")
DEFAULT_TIMEZONE = os.getenv("USER_SERVER_TIMEZONE", "Asia/Shanghai")
DEFAULT_DEBUG_UID = os.getenv("USER_SERVER_DEBUG_UID", "test_user")


class UserServerClient:
  def __init__(
    self,
    base_url: str = DEFAULT_BASE_URL,
    jwt_token: str = "",
    uid: str = DEFAULT_DEBUG_UID,
    timeout: int = 30,
    language: str = DEFAULT_LANGUAGE,
    timezone: str = DEFAULT_TIMEZONE,
  ):
    self.base_url = base_url.rstrip("/")
    self.jwt_token = jwt_token
    self.uid = uid
    self.timeout = timeout
    self.language = language
    self.timezone = timezone
    self.session = requests.Session()
    self.session.trust_env = False

  def _auth_data(self) -> dict[str, Any]:
    if self.jwt_token:
      return {"jwt_token": self.jwt_token}
    return {"uid": self.uid}

  def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{self.base_url}{path}"
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

  def login_with_jwt(self, jwt_token: str | None = None) -> dict[str, Any]:
    token = jwt_token or self.jwt_token
    payload = {
      "request_type": "login_with_jwt",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {"jwt_token": token},
    }
    return self._post("/login", payload)

  def query_profile(self) -> dict[str, Any]:
    payload = {
      "request_type": "query_profile",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": self._auth_data(),
    }
    return self._post("/user_profile", payload)

  def update_profile(
    self,
    user_profile: dict[str, Any] | None = None,
    skip_sleep_scenarios_reco_update: bool = False,
  ) -> dict[str, Any]:
    payload = {
      "request_type": "update_profile",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {
        **self._auth_data(),
        "user_profile": user_profile or self.default_user_profile(),
        "skip_sleep_scenarios_reco_update": skip_sleep_scenarios_reco_update,
      },
    }
    return self._post("/user_profile", payload)

  def analysis_overview(self, date: str | None = None, modules: list[str] | None = None) -> dict[str, Any]:
    return self._analysis_request(
      "analysis_overview",
      {"date": date or self.today(), "modules": modules or ["overall_score", "weekly_best", "sleep_insight"]},
    )

  def analysis_sleep_day(self, date: str | None = None, modules: list[str] | None = None) -> dict[str, Any]:
    return self._analysis_request(
      "analysis_sleep_day",
      {"date": date or self.today(), "modules": modules or ["score_summary", "sleep_scenarios", "stage_insights"]},
    )

  def analysis_sleep_week(
    self,
    start_date: str | None = None,
    end_date: str | None = None,
    modules: list[str] | None = None,
  ) -> dict[str, Any]:
    return self._analysis_request(
      "analysis_sleep_week",
      {
        "start_date": start_date or self.days_ago(6),
        "end_date": end_date or self.today(),
        "modules": modules or ["score_summary", "sleep_trends", "onset_efficiency"],
      },
    )

  def analysis_sleep_month(
    self,
    start_date: str | None = None,
    end_date: str | None = None,
    modules: list[str] | None = None,
  ) -> dict[str, Any]:
    return self._analysis_request(
      "analysis_sleep_month",
      {
        "start_date": start_date or self.days_ago(29),
        "end_date": end_date or self.today(),
        "modules": modules or ["score_summary", "sleep_trends", "onset_efficiency"],
      },
    )

  def analysis_explore(self, date: str | None = None, modules: list[str] | None = None) -> dict[str, Any]:
    return self._analysis_request(
      "analysis_explore",
      {
        "date": date or self.today(),
        "modules": modules or [
          "header_summary",
          "score_summary",
          "onset_efficiency",
          "sleep_structure",
          "night_fluctuation",
          "scene_preference",
          "sleep_advice",
        ],
      },
    )

  def sleep_advice(
    self,
    date: str | None = None,
    focus: list[str] | None = None,
    language: str | None = None,
  ) -> dict[str, Any]:
    payload = {
      "request_type": "sleep_analysis_advice",
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {
        **self._auth_data(),
        "date": date or self.today(),
        "language": language or self.language,
        "timezone": self.timezone,
        "focus": focus or [],
      },
    }
    return self._post("/sleep_advice", payload)

  def run_all(self) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if self.jwt_token:
      results["login_with_jwt"] = self.login_with_jwt()

    results["update_profile"] = self.update_profile()
    results["query_profile"] = self.query_profile()
    results["analysis_overview"] = self.analysis_overview()
    results["analysis_sleep_day"] = self.analysis_sleep_day()
    results["analysis_sleep_week"] = self.analysis_sleep_week()
    results["analysis_sleep_month"] = self.analysis_sleep_month()
    results["analysis_explore"] = self.analysis_explore()
    results["sleep_analysis_advice"] = self.sleep_advice()
    return results

  def _analysis_request(self, request_type: str, extra_data: dict[str, Any]) -> dict[str, Any]:
    payload = {
      "request_type": request_type,
      "timestamp": int(time.time()),
      "version": "1.0",
      "data": {
        **self._auth_data(),
        "language": self.language,
        "timezone": self.timezone,
        **extra_data,
      },
    }
    return self._post("/analysis", payload)

  @staticmethod
  def today() -> str:
    return dt.date.today().isoformat()

  @staticmethod
  def days_ago(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()

  def default_user_profile(self) -> dict[str, Any]:
    return {
      "uid_emb": [],
      "long_term_profile": [],
      "behaviors": {
        "heart_rate": [],
        "blood_oxygen": [],
        "sleep_status": [],
        "clicks": [],
        "plays": [],
      },
      "profile": {
        "nickname": "Mindora Test User",
        "gender": "Unknown",
        "age": "28",
        "birthday": "1998-08-12",
        "email": "profile@example.com",
        "phone": "13800138000",
        "address_list": [
          {
            "id": "addr_001",
            "is_default": True,
            "region": "Shanghai",
            "detail": "Zhonghai Center A-1501",
            "name": "Test User",
            "phone": "13800138000",
          }
        ],
        "avatar_base64": "",
        "avatar_mime_type": "image/jpeg",
      },
    }


def print_result(title: str, result: Any):
  print(f"\n{'=' * 20} {title} {'=' * 20}")
  print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Client for all HTTP APIs exposed by user_server.py")
  parser.add_argument(
    "action",
    nargs="?",
    default="run_all",
    choices=[
      "run_all",
      "login",
      "query_profile",
      "update_profile",
      "analysis_overview",
      "analysis_sleep_day",
      "analysis_sleep_week",
      "analysis_sleep_month",
      "analysis_explore",
      "sleep_advice",
    ],
  )
  parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
  parser.add_argument("--jwt-token", default=os.getenv("JWT_TOKEN", ""))
  parser.add_argument("--uid", default=DEFAULT_DEBUG_UID)
  parser.add_argument("--timeout", type=int, default=30)
  parser.add_argument("--date", default=None)
  parser.add_argument("--start-date", default=None)
  parser.add_argument("--end-date", default=None)
  parser.add_argument("--language", default=DEFAULT_LANGUAGE)
  parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
  parser.add_argument("--focus", nargs="*", default=None)
  parser.add_argument("--modules", nargs="*", default=None)
  parser.add_argument("--skip-sleep-scenarios-reco-update", action="store_true")
  return parser


def main():
  args = build_parser().parse_args()
  client = UserServerClient(
    base_url=args.base_url,
    jwt_token=args.jwt_token,
    uid=args.uid,
    timeout=args.timeout,
    language=args.language,
    timezone=args.timezone,
  )

  if args.action == "run_all":
    result = client.run_all()
  elif args.action == "login":
    result = client.login_with_jwt()
  elif args.action == "query_profile":
    result = client.query_profile()
  elif args.action == "update_profile":
    result = client.update_profile(skip_sleep_scenarios_reco_update=args.skip_sleep_scenarios_reco_update)
  elif args.action == "analysis_overview":
    result = client.analysis_overview(date=args.date, modules=args.modules)
  elif args.action == "analysis_sleep_day":
    result = client.analysis_sleep_day(date=args.date, modules=args.modules)
  elif args.action == "analysis_sleep_week":
    result = client.analysis_sleep_week(start_date=args.start_date, end_date=args.end_date, modules=args.modules)
  elif args.action == "analysis_sleep_month":
    result = client.analysis_sleep_month(start_date=args.start_date, end_date=args.end_date, modules=args.modules)
  elif args.action == "analysis_explore":
    result = client.analysis_explore(date=args.date, modules=args.modules)
  else:
    result = client.sleep_advice(date=args.date, focus=args.focus, language=args.language)

  print_result(args.action, result)


if __name__ == "__main__":
  main()
