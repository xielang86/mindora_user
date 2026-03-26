"""
app_client.py — simulates all app → server requests defined in the MD specs:
  - 个人资料接口.md  : query_profile, update_profile  → POST /user_profile
  - 服务端分析接口.md: analysis_overview, analysis_sleep_day, analysis_sleep_week,
                       analysis_sleep_month, analysis_explore  → POST /analysis

Usage:
  python tool/app_client.py [base_url] [jwt_token]

  base_url   : default http://127.0.0.1:9001
  jwt_token  : if omitted, the script will try to read JWT_TOKEN env var;
               if still missing, profile requests will use uid="test_user" (debug mode only)

Examples:
  python tool/app_client.py
  python tool/app_client.py http://127.0.0.1:9001 eyJhbGci...
"""

import json, sys, time, datetime, os
import requests

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────
BASE_URL  = sys.argv[1] if len(sys.argv) > 1 else os.getenv("APP_SERVER_URL", "http://127.0.0.1:9001")
JWT_TOKEN = sys.argv[2] if len(sys.argv) > 2 else os.getenv("JWT_TOKEN", "")
DEBUG_UID = "test_user"   # used when JWT_TOKEN is empty and server runs in debug mode

TODAY       = datetime.date.today().isoformat()
WEEK_START  = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
MONTH_START = (datetime.date.today() - datetime.timedelta(days=29)).isoformat()
TIMEZONE    = "Asia/Shanghai"
LANGUAGE    = "zh-Hans"


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────
def _auth_data() -> dict:
    d: dict = {}
    if JWT_TOKEN:
        d["jwt_token"] = JWT_TOKEN
    else:
        d["uid"] = DEBUG_UID
    return d


def post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    try:
        r = requests.post(url, json=payload, timeout=120)
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"Connection refused — is the server running at {BASE_URL}?"}
    except Exception as e:
        return {"error": str(e)}


def print_section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def print_resp(resp: dict):
    print(json.dumps(resp, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────
# /user_profile  requests
# ──────────────────────────────────────────

def req_update_profile():
    """update_profile — upload personal info + address + avatar stub"""
    print_section("POST /user_profile  [update_profile]")
    payload = {
        "request_type": "update_profile",
        "timestamp": int(time.time()),
        "version": "1.0",
        "data": {
            **_auth_data(),
            "user_profile": {
                "uid_emb": [],
                "long_term_profile": [],
                "behaviors": {},
                "profile": {
                    "nickname": "DK",
                    "gender": "Male",
                    "age": "28",
                    "birthday": "1998.08.12",
                    "email": "profile@example.com",
                    "phone": "13866997600",
                    "address_list": [
                        {
                            "id": "addr_001",
                            "is_default": True,
                            "region": "上海市 普陀区",
                            "detail": "中海中心A座1501室",
                            "name": "张三",
                            "phone": "13866997600",
                        },
                        {
                            "id": "addr_002",
                            "is_default": False,
                            "region": "浙江省 杭州市",
                            "detail": "文三路 188 号",
                            "name": "李四",
                            "phone": "13900000000",
                        },
                    ],
                    "avatar_base64": "",
                    "avatar_mime_type": "image/jpeg",
                },
            },
        },
    }
    print_resp(post("/user_profile", payload))


def req_query_profile():
    """query_profile — fetch stored user profile"""
    print_section("POST /user_profile  [query_profile]")
    payload = {
        "request_type": "query_profile",
        "timestamp": int(time.time()),
        "version": "1.0",
        "data": _auth_data(),
    }
    print_resp(post("/user_profile", payload))


# ──────────────────────────────────────────
# /analysis  requests
# ──────────────────────────────────────────

def _analysis_base(request_type: str, extra: dict | None = None) -> dict:
    payload = {
        "request_type": request_type,
        "version": "1.0",
        "timestamp": int(time.time()),
        "data": {
            **_auth_data(),
            "language": LANGUAGE,
            "timezone": TIMEZONE,
            **(extra or {}),
        },
    }
    return payload


def req_analysis_overview():
    """analysis_overview — Home page: overall_score, weekly_best, sleep_insight"""
    print_section("POST /analysis  [analysis_overview]")
    payload = _analysis_base("analysis_overview", {
        "date": TODAY,
        "modules": ["overall_score", "weekly_best", "sleep_insight"],
    })
    print_resp(post("/analysis", payload))


def req_analysis_sleep_day():
    """analysis_sleep_day — Health Day: score_summary, sleep_scenarios, stage_insights"""
    print_section("POST /analysis  [analysis_sleep_day]")
    payload = _analysis_base("analysis_sleep_day", {
        "date": TODAY,
        "modules": ["score_summary", "sleep_scenarios", "stage_insights"],
    })
    print_resp(post("/analysis", payload))


def req_analysis_sleep_week():
    """analysis_sleep_week — Health Week: score_summary, sleep_trends, onset_efficiency"""
    print_section("POST /analysis  [analysis_sleep_week]")
    payload = _analysis_base("analysis_sleep_week", {
        "start_date": WEEK_START,
        "end_date":   TODAY,
        "modules": ["score_summary", "sleep_trends", "onset_efficiency"],
    })
    print_resp(post("/analysis", payload))


def req_analysis_sleep_month():
    """analysis_sleep_month — Health Month: score_summary, sleep_trends (with series), onset_efficiency"""
    print_section("POST /analysis  [analysis_sleep_month]")
    payload = _analysis_base("analysis_sleep_month", {
        "start_date": MONTH_START,
        "end_date":   TODAY,
        "modules": ["score_summary", "sleep_trends", "onset_efficiency"],
    })
    print_resp(post("/analysis", payload))


def req_analysis_explore():
    """analysis_explore — Explore page: all cards"""
    print_section("POST /analysis  [analysis_explore]")
    payload = _analysis_base("analysis_explore", {
        "date": TODAY,
        "modules": [
            "header_summary",
            "score_summary",
            "onset_efficiency",
            "sleep_structure",
            "night_fluctuation",
            "scene_preference",
            "sleep_advice",
        ],
    })
    print_resp(post("/analysis", payload))


def req_analysis_explore_partial():
    """analysis_explore — only score_summary + sleep_advice (module filtering demo)"""
    print_section("POST /analysis  [analysis_explore] — partial modules")
    payload = _analysis_base("analysis_explore", {
        "date": TODAY,
        "modules": ["score_summary", "sleep_advice"],
    })
    print_resp(post("/analysis", payload))


# ──────────────────────────────────────────
# Error / edge-case requests
# ──────────────────────────────────────────

def req_invalid_token():
    """analysis_overview with a bad JWT — expect 401"""
    print_section("POST /analysis  [bad JWT → expect 401]")
    payload = {
        "request_type": "analysis_overview",
        "version": "1.0",
        "timestamp": int(time.time()),
        "data": {
            "jwt_token": "invalid.token.value",
            "language": LANGUAGE,
            "timezone": TIMEZONE,
            "date": TODAY,
        },
    }
    print_resp(post("/analysis", payload))


def req_missing_auth():
    """analysis_overview with neither uid nor jwt_token — expect 400"""
    print_section("POST /analysis  [missing auth → expect 400]")
    payload = {
        "request_type": "analysis_overview",
        "version": "1.0",
        "timestamp": int(time.time()),
        "data": {
            "language": LANGUAGE,
            "timezone": TIMEZONE,
            "date": TODAY,
        },
    }
    print_resp(post("/analysis", payload))


# ──────────────────────────────────────────
# Main
# ──────────────────────────────────────────

ALL_CASES = [
    req_update_profile,
    req_query_profile,
    req_analysis_overview,
    req_analysis_sleep_day,
    req_analysis_sleep_week,
    req_analysis_sleep_month,
    req_analysis_explore,
    req_analysis_explore_partial,
    req_invalid_token,
    req_missing_auth,
]

if __name__ == "__main__":
    print(f"\nApp Client — target: {BASE_URL}")
    print(f"Auth:  {'JWT token' if JWT_TOKEN else f'debug uid={DEBUG_UID!r}'}")
    print(f"Date:  {TODAY}  |  week: {WEEK_START}~{TODAY}  |  month: {MONTH_START}~{TODAY}")

    for fn in ALL_CASES:
        fn()

    print(f"\n{'═'*60}")
    print("  All requests completed.")
    print(f"{'═'*60}\n")
