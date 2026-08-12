"""端到端验证：拼接 prompt -> 真实请求 LLM -> 填入 profile.sleep_insight，并验证异步更新。

用法：
  # 第 1 步：直接验证 LLM 链路（不起服务器）
  python tool/test_sleep_insight_e2e.py llm

  # 第 2 步：起测试服务器（端口 19001，txt_json 存储在 /tmp，不影响 9001 的 LevelDB）
  python tool/test_sleep_insight_e2e.py server

  # 第 3 步：打更新请求，验证立即返回 + 后台 LLM 填入 sleep_insight
  python tool/test_sleep_insight_e2e.py client
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

TEST_PORT = 19001
TEST_UID = "mindora_test_uid1"


def build_profile_with_sleep_data() -> dict:
    """构造带 3 天睡眠数据的 user_profile（让 7 日统计非空）。"""
    now = int(time.time())
    sleep_data = []
    for i, (quality, onset, first_sleep) in enumerate([
        (85.0, 8.0, "23:10"), (78.0, 12.0, "23:20"), (82.0, 10.0, "23:05"),
    ]):
        day_ts = now - (i + 1) * 86400
        sleep_data.append({
            "timestamp": day_ts,
            "sleep_quality": quality,
            "onset": onset,
            "first_sleep_time": first_sleep,
            "hr_before_sleep": 66,
            "rr_before_sleep": 14,
            "avg_heart_rate": 60,
            "avg_respiratory": 14,
            "hrv": 42,
            "sleep_status": [
                {"start_time": day_ts, "duration": 20, "sleep_type": "awake"},
                {"start_time": day_ts + 1200, "duration": 200, "sleep_type": "core"},
                {"start_time": day_ts + 13200, "duration": 90, "sleep_type": "deep"},
                {"start_time": day_ts + 18600, "duration": 80, "sleep_type": "rem"},
                {"start_time": day_ts + 23400, "duration": 5, "sleep_type": "awake"},
                {"start_time": day_ts + 23700, "duration": 60, "sleep_type": "core"},
            ],
        })
    return {
        "uid_emb": [],
        "long_term_profile": [],
        "behaviors": {"heart_rate": [], "sleep_status": [], "clicks": [], "plays": []},
        "sleep_data": sleep_data,
        "mindora_record": {
            "sleep.scene.cocos_island_moonlight": [[now - 3600, 600], [now - 90000, 600]],
            "sleep.scene.kyoto_forest": [[now - 180000, 600]],
        },
    }


def test_llm_direct():
    """第 1 步：真实调用 LLM，验证 prompt -> 解析 -> SleepInsightReport 填入。"""
    import threading
    from user_profile import UserProfile, SleepInsightReport
    from llm import SleepAnalysisLLM, extract_sleep_context
    from user_server import UserProfileServ

    profile = UserProfile(**build_profile_with_sleep_data())

    class _FakeData:
        date = time.strftime("%Y-%m-%d")
        start_date = None
        end_date = None
        language = "zh-Hans"

    ctx = extract_sleep_context(profile, _FakeData())
    print(f"[ctx] 7日统计: record_count={ctx.get('record_count')}, "
          f"avg_onset={ctx.get('avg_onset_min')}, top_scene={ctx.get('weekly_top_scene_title')}")

    llm = SleepAnalysisLLM()
    assert llm.enabled, "LLM 未启用（检查 ARK_API_KEY 环境变量）"

    print("[llm] 真实请求 LLM (sleep_insight_report)...")
    t0 = time.time()
    raw = llm.generate_sync("sleep_insight_report", ctx, "zh-Hans", [])
    dt = time.time() - t0
    assert raw, "LLM 未返回有效 JSON"
    print(f"[llm] {dt:.1f}s 返回，模块: {list(raw.keys())}")

    # 走 server 侧填入逻辑
    serv = UserProfileServ.__new__(UserProfileServ)
    serv.lock = threading.RLock()
    serv.llm = llm
    report = serv.calc_sleep_insight(TEST_UID, profile)
    assert isinstance(report, SleepInsightReport)
    for key, _mid in UserProfileServ._INSIGHT_MODULE_KEYS:
        m = getattr(report, key)
        print(f"  模块{m.module_id} [{key}] title={m.title!r} visible={m.visible}")
        print(f"      content={m.content[:80]!r}")
    assert report.greeting.title, "模块0 标题为空"
    profile.sleep_insight = report

    # 序列化回环（LevelDB 存取路径）
    profile2 = UserProfile.model_validate(profile.model_dump())
    assert profile2.sleep_insight.greeting.title == report.greeting.title
    print("[ok] 第 1 步通过：prompt -> LLM -> 填入 sleep_insight -> 序列化回环")


def run_server():
    """第 2 步：起测试服务器（独立端口 + /tmp 存储，不碰 9001 的 LevelDB）。"""
    os.environ.setdefault("RUN_DIR", str(ROOT_DIR))
    from aiohttp import web
    from config import Config

    Config.PORT = TEST_PORT
    Config.USER_PROFILE_STORAGE_MODE = "txt_json"
    Config.USER_PROFILE_JSON_PATH = "/tmp/test_insight_e2e_profiles.txt"

    import user_server
    srv = user_server.UserServer()
    assert srv.llm.enabled, "LLM 未启用（检查 ARK_API_KEY）"
    print(f"[server] 测试服务器启动: 127.0.0.1:{TEST_PORT}, storage=txt_json")
    web.run_app(srv.app, host="127.0.0.1", port=TEST_PORT, print=None)


def run_client():
    """第 3 步：更新请求应立即返回；后台 LLM 完成后 sleep_insight 落库。"""
    import requests

    base = f"http://127.0.0.1:{TEST_PORT}/user_profile"
    payload = {
        "request_type": "update_profile",
        "timestamp": int(time.time()),
        "version": "1.0",
        "data": {
            "uid": TEST_UID,
            "user_profile": build_profile_with_sleep_data(),
            # 推荐与睡眠分析是独立开关，都要显式打开（light 模式默认都跳过）
            "skip_sleep_scenarios_reco_update": False,
            "skip_sleep_analysis_update": False,
        },
    }

    t0 = time.time()
    resp = requests.post(base, json=payload, timeout=10)
    update_dt = time.time() - t0
    body = resp.json()
    assert body.get("code") == 0, f"更新失败: {body}"
    print(f"[client] update_profile 返回 code=0，耗时 {update_dt:.2f}s")
    assert update_dt < 5, f"更新请求耗时 {update_dt:.1f}s，不是异步返回！"
    print("[ok] 更新请求立即返回（LLM 在后台执行）")

    # 轮询等后台 LLM 完成（含 reco + insight + 5 个分析报告，可能几分钟）
    deadline = time.time() + 420
    while time.time() < deadline:
        time.sleep(10)
        q = {
            "request_type": "query_profile",
            "timestamp": int(time.time()),
            "version": "1.0",
            "data": {"uid": TEST_UID},
        }
        prof = (requests.post(base, json=q, timeout=10).json().get("data") or {}).get("user_profile") or {}
        insight = prof.get("sleep_insight")
        reports = prof.get("analysis_reports") or {}
        day_reports = reports.get("analysis_sleep_day") or []
        if insight and insight.get("greeting", {}).get("title") and day_reports:
            total = time.time() - t0
            print(f"[client] {total:.0f}s 后 sleep_insight + analysis_reports 已落库:")
            for key in ["greeting", "onset", "architecture", "intervention", "scene_preference", "micro_education"]:
                m = insight.get(key) or {}
                print(f"  模块{m.get('module_id')} [{key}] {m.get('title')!r} visible={m.get('visible')}")
            for rt, rs in reports.items():
                print(f"  analysis_reports[{rt}]: {len(rs)} 条")
            print("[ok] 第 3 步通过：异步更新 -> 后台 LLM -> sleep_insight + 分析报告落库")

            # 第 4 步：验证三个读取路径
            verify_read_paths(prof)
            return
        print(f"[client] 等待后台 LLM... ({int(time.time() - t0)}s)")
    raise SystemExit("❌ 超时：sleep_insight/analysis_reports 未在 420s 内落库")


def verify_read_paths(prof):
    """第 4 步：验证 /analysis 库存报告与 analysis_explore 的洞察报告出口。"""
    import requests

    ts = int(time.time())
    uid_body = {"uid": TEST_UID}
    today = time.strftime("%Y-%m-%d")

    # 4.1 analysis_explore → insight key 携带 6 模块报告 + visible 过滤
    r = requests.post(f"http://127.0.0.1:{TEST_PORT}/analysis", json={
        "request_type": "analysis_explore", "timestamp": ts, "version": "1.0",
        "data": {**uid_body, "language": "en", "date": today, "modules": []},
    }, timeout=10)
    insight = (r.json().get("data") or {}).get("insight") or {}
    assert insight.get("greeting", {}).get("title"), f"explore 无 insight: {r.json()}"
    raw_visible = {k: (prof["sleep_insight"].get(k) or {}).get("visible", True)
                   for k, _ in UserProfileServKeys}
    invisible = [k for k, v in raw_visible.items() if v is False]
    for k in invisible:
        assert k not in insight, f"visible=False 的模块 {k} 未被过滤"
    print(f"4.1 analysis_explore 洞察出口 OK（6模块报告，过滤了 {invisible or '无'}）")

    # 4.2 /analysis analysis_sleep_day → 命中库存报告（不再同步调 LLM）
    t0 = time.time()
    r = requests.post(f"http://127.0.0.1:{TEST_PORT}/analysis", json={
        "request_type": "analysis_sleep_day", "timestamp": ts, "version": "1.0",
        "data": {**uid_body, "language": "en", "date": today, "modules": []},
    }, timeout=10)
    dt = time.time() - t0
    assert r.json().get("code") == 0, r.json()
    assert dt < 5, f"/analysis 耗时 {dt:.1f}s，疑似同步调了 LLM"
    print(f"4.2 /analysis 库存报告 OK（{dt:.2f}s 返回，纯查库）")

    # 4.3 已删除的端点应返回 400/404（/user_profile insight 分支、/sleep_advice）
    r = requests.post(f"http://127.0.0.1:{TEST_PORT}/user_profile", json={
        "request_type": "insight", "timestamp": ts, "version": "1.0", "data": uid_body,
    }, timeout=10)
    assert r.status_code == 400, f"已删除的 insight 分支仍可用: {r.status_code}"
    r = requests.post(f"http://127.0.0.1:{TEST_PORT}/sleep_advice", json={
        "request_type": "sleep_analysis_advice", "timestamp": ts, "version": "1.0", "data": uid_body,
    }, timeout=10)
    assert r.status_code == 404, f"已删除的 /sleep_advice 仍可用: {r.status_code}"
    print("4.3 已删除端点（/user_profile insight、/sleep_advice）正确拒绝 OK")
    print("\n✅ 全部 E2E 验证通过")


UserProfileServKeys = [
    ("greeting", 0), ("onset", 1), ("architecture", 2),
    ("intervention", 3), ("scene_preference", 4), ("micro_education", 5),
]


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "llm"
    {"llm": test_llm_direct, "server": run_server, "client": run_client}[mode]()
