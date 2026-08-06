"""
ops_client.py — regression client for the ops APIs that user_server_client.py does not cover:
  - tanchuang_suvey.md : query_popups / report_popup   → POST /popup
                         query_survey / submit_survey  → POST /survey
  - peibanzuji.md      : upload_footprint / query_footprint → POST /companion_footprint

Each case prints the full JSON response (for eyeballing field diffs) and checks the
expected code; the summary at the end prints PASS/FAIL and the exit code is non-zero
if any case failed, so it can run in CI.

Usage:
  python tool/ops_client.py [base_url] [jwt_token]

  base_url   : default http://127.0.0.1:9001
  jwt_token  : if omitted, falls back to JWT_TOKEN env var; if still missing,
               requests use uid="mindora_test_uid2" (debug whitelist in user_server.py)

Notes:
  - submit_survey is idempotent per uid+survey_id: the second submit in one run must
    return reward_granted=False with the same submission_id. Across runs the first
    submit may already report reward_granted=False — that is expected, not a failure.
  - footprint cases use a fixed month (2026-01) so repeated runs don't pollute the
    current month's stats of the shared debug uid.
"""

import datetime
import json
import os
import sys
import time

import requests

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────
BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.getenv("APP_SERVER_URL", "http://127.0.0.1:9001")
JWT_TOKEN = sys.argv[2] if len(sys.argv) > 2 else os.getenv("JWT_TOKEN", "")
DEBUG_UID = "mindora_test_uid2"   # debug whitelist, isolated from other tools' uid1

TIMEZONE = "Asia/Shanghai"
LANGUAGE = "zh-Hans"

# ids from data/popup_survey_config.json
SURVEY_ID = "sv_sleep_2026q3"
POPUP_ID = "pop_20260730_survey_01"

# fixed footprint window so regression runs are reproducible
FP_YEAR = 2026
FP_MONTH = 1
FP_DATE = "2026-01-15"

RESULTS = []  # (name, ok, detail)


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────
def _auth_data() -> dict:
    if JWT_TOKEN:
        return {"jwt_token": JWT_TOKEN}
    return {"uid": DEBUG_UID}


def envelope(request_type: str, data: dict) -> dict:
    return {
        "request_type": request_type,
        "timestamp": int(time.time()),
        "version": "1.0",
        "data": data,
    }


def post(endpoint: str, payload: dict) -> tuple[int, dict]:
    r = requests.post(f"{BASE_URL}{endpoint}", json=payload, timeout=30)
    return r.status_code, r.json()


def print_section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def run_case(name: str, endpoint: str, payload: dict, expect_code: int,
             check=None) -> dict | None:
    """Fire one request, print full response, record PASS/FAIL.

    expect_code : expected business code in the response body.
    check       : optional fn(resp_data) -> (ok, detail) for content assertions.
    Returns the response dict (or None on transport failure).
    """
    print_section(f"POST {endpoint}  [{payload['request_type']}] — {name}")
    try:
        http_status, resp = post(endpoint, payload)
    except Exception as e:
        print(f"  transport error: {e}")
        RESULTS.append((name, False, f"transport error: {e}"))
        return None

    print(json.dumps(resp, ensure_ascii=False, indent=2))

    code = resp.get("code")
    if code != expect_code:
        RESULTS.append((name, False, f"code={code}, expect {expect_code}"))
        return resp
    if check:
        ok, detail = check(resp.get("data") or {})
        RESULTS.append((name, ok, detail))
    else:
        RESULTS.append((name, True, f"code={code}"))
    return resp


# ──────────────────────────────────────────
# /popup  (tanchuang_suvey.md)
# ──────────────────────────────────────────
def req_query_popups():
    # 注意：display_rule（max_show_count/cooldown/已提交问卷抑制）会让返回列表为空，
    # 这是服务端正确行为，所以这里只校验响应结构，不校验非空。
    run_case(
        "query_popups",
        "/popup",
        envelope("query_popups", {
            **_auth_data(),
            "language": LANGUAGE,
            "timezone": TIMEZONE,
            "app_version": "1.0.0",
            "platform": "ios",
            "placement": "home",
        }),
        expect_code=0,
        check=lambda d: (
            isinstance(d.get("popups"), list),
            f"popups={len(d.get('popups', []))} (可能被 display_rule 抑制), "
            f"next_query_after={d.get('next_query_after')}",
        ),
    )


def req_report_popup():
    # 不依赖 query_popups 的返回（可能被 display_rule 抑制为空），直接用配置里的 id。
    for event in ("impression", "click"):
        run_case(
            f"report_popup {event}",
            "/popup",
            envelope("report_popup", {
                **_auth_data(),
                "language": LANGUAGE,
                "popup_id": POPUP_ID,
                "event": event,
                "event_at": int(time.time()),
            }),
            expect_code=0,
        )
    # unknown popup_id must be rejected
    run_case(
        "report_popup unknown id → expect 400",
        "/popup",
        envelope("report_popup", {
            **_auth_data(),
            "language": LANGUAGE,
            "popup_id": "pop_nonexistent",
            "event": "impression",
        }),
        expect_code=400,
    )


# ──────────────────────────────────────────
# /survey  (tanchuang_suvey.md)
# ──────────────────────────────────────────
def req_query_survey():
    run_case(
        f"query_survey {SURVEY_ID}",
        "/survey",
        envelope("query_survey", {
            **_auth_data(),
            "language": LANGUAGE,
            "survey_id": SURVEY_ID,
        }),
        expect_code=0,
        check=lambda d: (
            bool(d.get("questions")),
            f"title={d.get('title')!r}, questions={len(d.get('questions', []))}, "
            f"reward={d.get('reward', {}).get('gift_type')}",
        ),
    )
    run_case(
        "query_survey unknown id → expect 404",
        "/survey",
        envelope("query_survey", {
            **_auth_data(),
            "language": LANGUAGE,
            "survey_id": "sv_nonexistent",
        }),
        expect_code=404,
    )


def req_submit_survey():
    payload_data = {
        **_auth_data(),
        "language": LANGUAGE,
        "survey_id": SURVEY_ID,
        "answers": [
            {"question_id": "q1", "type": "single_choice", "option_ids": ["o2"], "text": ""},
            {"question_id": "q2", "type": "single_choice", "option_ids": ["o1"], "text": ""},
            {"question_id": "q3", "type": "text", "option_ids": [], "text": "希望支持更多助眠场景"},
        ],
        "duration_seconds": 95,
        # reward.gift_type=physical → gift_delivery 必填
        "gift_delivery": {
            "type": "physical",
            "name": "测试用户",
            "phone": "13800138000",
            "region": "上海市 浦东新区",
            "detail": " regression 测试地址 101 室",
        },
    }
    first = run_case(
        "submit_survey (first)",
        "/survey",
        envelope("submit_survey", dict(payload_data)),
        expect_code=0,
    )
    second = run_case(
        "submit_survey (repeat → idempotent, reward_granted must be False)",
        "/survey",
        envelope("submit_survey", dict(payload_data)),
        expect_code=0,
    )
    if not first or not second:
        return
    d1, d2 = first.get("data", {}), second.get("data", {})
    ok = (
        d2.get("reward_granted") is False
        and d1.get("submission_id") == d2.get("submission_id")
    )
    RESULTS.append((
        "submit_survey idempotency",
        ok,
        f"submission_id same={d1.get('submission_id') == d2.get('submission_id')}, "
        f"repeat reward_granted={d2.get('reward_granted')}",
    ))


# ──────────────────────────────────────────
# /companion_footprint  (peibanzuji.md)
# ──────────────────────────────────────────
def req_upload_footprint():
    run_case(
        f"upload_footprint {FP_DATE}",
        "/companion_footprint",
        envelope("upload_footprint", {
            **_auth_data(),
            "timezone": TIMEZONE,
            "days": [{
                "date": FP_DATE,
                "app_active": True,
                "sleep_companion": True,
                "plan_completed": True,
                "app_open_count": 5,
                "companion_minutes": 55,
            }],
        }),
        expect_code=0,
        check=lambda d: (d.get("accepted_days") == 1, f"accepted_days={d.get('accepted_days')}"),
    )


def req_upload_footprint_merge():
    """Re-upload same day with weaker flags: OR-merge must keep True flags."""
    run_case(
        f"upload_footprint {FP_DATE} weaker flags (OR-merge check)",
        "/companion_footprint",
        envelope("upload_footprint", {
            **_auth_data(),
            "timezone": TIMEZONE,
            "days": [{
                "date": FP_DATE,
                "app_active": True,
                "sleep_companion": False,
                "plan_completed": False,
                "app_open_count": 9,
                "companion_minutes": 20,
            }],
        }),
        expect_code=0,
    )


def req_query_footprint_month():
    def check(d):
        day = next((x for x in d.get("days", []) if x.get("day") == int(FP_DATE[-2:])), None)
        ok = day is not None and day.get("sleep_companion") and day.get("plan_completed")
        return ok, f"day={day}, stats={d.get('stats')}, milestones={len(d.get('milestones', []))}"

    run_case(
        f"query_footprint month {FP_YEAR}-{FP_MONTH:02d}",
        "/companion_footprint",
        envelope("query_footprint", {
            **_auth_data(),
            "timezone": TIMEZONE,
            "scope": "month",
            "year": FP_YEAR,
            "month": FP_MONTH,
        }),
        expect_code=0,
        check=check,
    )


def req_query_footprint_year():
    run_case(
        f"query_footprint year {FP_YEAR}",
        "/companion_footprint",
        envelope("query_footprint", {
            **_auth_data(),
            "timezone": TIMEZONE,
            "scope": "year",
            "year": FP_YEAR,
        }),
        expect_code=0,
    )


# ──────────────────────────────────────────
# negative cases
# ──────────────────────────────────────────
def req_bad_token():
    run_case(
        "bad JWT → expect 401",
        "/popup",
        envelope("query_popups", {
            "jwt_token": "bad.token.here",
            "language": LANGUAGE,
            "placement": "home",
        }),
        expect_code=401,
    )


def req_missing_auth():
    print_section("POST /survey  [missing auth → expect 400]")
    payload = {
        "request_type": "query_survey",
        "timestamp": int(time.time()),
        "version": "1.0",
        "data": {"language": LANGUAGE, "survey_id": SURVEY_ID},
    }
    try:
        http_status, resp = post("/survey", payload)
    except Exception as e:
        RESULTS.append(("missing auth", False, f"transport error: {e}"))
        return
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    ok = resp.get("code") == 400
    RESULTS.append(("missing auth", ok, f"code={resp.get('code')}, expect 400"))


ALL_CASES = [
    req_query_popups,
    req_report_popup,
    req_query_survey,
    req_submit_survey,
    req_upload_footprint,
    req_upload_footprint_merge,
    req_query_footprint_month,
    req_query_footprint_year,
    req_bad_token,
    req_missing_auth,
]

if __name__ == "__main__":
    print(f"\nOps Client — target: {BASE_URL}")
    print(f"Auth:  {'JWT token' if JWT_TOKEN else f'debug uid={DEBUG_UID!r}'}")
    print(f"Date:  {datetime.date.today().isoformat()}  |  footprint window: {FP_YEAR}-{FP_MONTH:02d}")

    for fn in ALL_CASES:
        fn()

    print(f"\n{'═'*60}")
    print("  Regression summary")
    print(f"{'═'*60}")
    failed = 0
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name} — {detail}")
    print(f"\n  {len(RESULTS) - failed}/{len(RESULTS)} passed")
    print(f"{'═'*60}\n")
    sys.exit(1 if failed else 0)
