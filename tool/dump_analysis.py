"""dump_analysis.py — 用与 app 相同的 uid/参数拉取全部 /analysis 响应 + query_profile，
落地成 JSON 并自动与画像源数据对账，生成逐屏字段对照表（CHECK_REPORT.md）。

用途：app 上逐屏核对字段时，不用在 raw profile 里翻 —— app 渲染的是
/analysis 响应（骨架 + LLM 库存报告 deep_merge 后的结果），本工具把
"app 每个界面元素 → JSON 路径 → 值 → 与源数据对账结果" 整理成一张表。

用法：
  python tool/dump_analysis.py --uid <uid> --host 192.168.x.x
  python tool/dump_analysis.py --jwt-token <token> --base-url http://127.0.0.1:9001

输出目录（默认 out_analysis/<uid>_<ts>/）：
  query_profile.json        画像（含 LLM 结果，SOP 标签画像/推荐详情需显式开启 include 开关）
  analysis_<type>.json      5 个 /analysis 响应（与 app 同参数）
  CHECK_REPORT.md           逐屏对照表 + 自动对账结果

注意：响应受 date/modules/language 影响。与 app 对比时保持 --date 为 app
展示的日期；app 若按 modules 过滤请求，本工具拉的是全量（modules=[]），
字段只会多不会少，按路径对照即可。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tool.user_server_client import UserServerClient
from user_profile import short_scene_id, SleepResult, UserProfile
from sleep_metrics import build_sleep_metrics
from sleep_session_builder import (resolve_sleep_quality, resolve_sleep_structure_score,
                                   aggregate_sleep_quality, mean_sleep_duration)
from analysis_score import daily_score, period_score
from insight_rules import compute_baselines, compute_insight_indices
from analysis_builders import _week_top_scene, _week_scene_score

ANALYSIS_TYPES = [
  "analysis_overview",
  "analysis_sleep_day",
  "analysis_sleep_week",
  "analysis_sleep_month",
  "analysis_explore",
]


# ── 与服务端 analysis_builders 同口径的源数据计算 ──────────────────

def _date_of(ts: int, tz=None) -> datetime.date:
  return datetime.datetime.fromtimestamp(ts, tz).date()


def _is_valid_night(r: dict) -> bool:
  """与 insight_rules.is_valid_night 同口径：有睡眠分段或有评分才算有效夜。"""
  return bool(r.get("sleep_status")) or r.get("sleep_quality") is not None


def _latest_valid_night(sleep_data: list) -> dict:
  """时间戳最大的有效夜（服务端 _fresh_latest/anchor 口径）；无有效夜返回 {}。"""
  best = None
  for r in sleep_data:
    if r.get("timestamp") and _is_valid_night(r):
      if best is None or r["timestamp"] > best["timestamp"]:
        best = r
  return best or {}


def _effective_soe(record: dict) -> int | None:
  from sleep_session_builder import resolve_sleep_onset_efficiency
  value = resolve_sleep_onset_efficiency(SleepResult.model_validate(record)) if record else None
  return int(value) if value is not None else None


def _effective_onset(record: dict) -> int | None:
  from sleep_session_builder import resolve_sleep_onset
  value = resolve_sleep_onset(SleepResult.model_validate(record)) if record else None
  return int(value) if value is not None else None


def _quality(record: dict) -> int | None:
  value = resolve_sleep_quality(SleepResult.model_validate(record)) if record else None
  return int(value) if value is not None else None


def _structure(record: dict) -> int | None:
  value = resolve_sleep_structure_score(SleepResult.model_validate(record)) if record else None
  return int(value) if value is not None else None


def _window_records(sleep_data: list, start: str, end: str, tz=None) -> list[SleepResult]:
  start_d = datetime.date.fromisoformat(start)
  end_d = datetime.date.fromisoformat(end)
  return [SleepResult.model_validate(r) for r in sleep_data
          if start_d <= _date_of(r["timestamp"], tz) <= end_d]


def _window_scores(sleep_data: list, start: str, end: str, tz=None) -> list[float]:
  return [resolve_sleep_quality(r) for r in _window_records(sleep_data, start, end, tz)
          if r.sleep_quality is not None]


def _top_scenes(mindora_record: dict, days: int, limit: int, end_ts: int | None = None) -> list[str]:
  # 窗口终点与服务端一致：锚定最近有效夜（end_ts），缺省回退当前时刻
  window_end = int(end_ts) if end_ts is not None else int(time.time())
  cutoff = window_end - days * 86400
  counts = {}
  for scene_id, records in (mindora_record or {}).items():
    # 与服务端 short_scene_id 同口径：strip 所有已知前缀（sleep.scene. / sleep.pure_music. 等）
    name = short_scene_id(scene_id).replace("_", " ").title()
    cnt = sum(
      1 for e in (records or [])
      if isinstance(e, (list, tuple)) and e and cutoff <= int(e[0]) <= window_end
    )
    if cnt:
      counts[name] = cnt
  return [name for name, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


# ── 对账 ──────────────────────────────────────────────────────────

class Checker:
  def __init__(self):
    self.rows: list[tuple[str, str, str, str]] = []  # (screen, json_path, value, verdict)

  def add(self, screen: str, path: str, value, verdict: str):
    self.rows.append((screen, path, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value), verdict))

  def check_eq(self, screen: str, path: str, actual, expected, note: str = ""):
    ok = actual == expected
    verdict = f"✅ 与源数据一致" if ok else f"❌ 期望 {expected!r}（{note}）"
    self.add(screen, path, actual, verdict)

  def check_text(self, screen: str, path: str, value, required: bool = False):
    """检查实际文案字段；必填卡片不得用日期或标签掩盖空正文。"""
    if value in (None, ""):
      self.add(screen, path, value, "❌ 缺少规则/库存文案" if required else "➖ 空（缺少数据或文案）")
    else:
      self.add(screen, path, value, "📝 文案（LLM 生成或骨架兜底，人工核对语言/语义）")


def run_checks(profile: dict, responses: dict[str, dict], date: str, has_sleep_source: bool = True,
               timezone: str | None = None, language: str | None = None) -> Checker:
  c = Checker()
  tz = ZoneInfo(timezone or profile.get("last_request_timezone") or "UTC")
  language = language or profile.get("last_request_language") or "zh-Hans"
  sleep_data = profile.get("sleep_data") or []
  # 与服务端 anchor 口径一致：最新有效夜（而非 sleep_data[-1]，尾包可能是无效夜）
  latest = _latest_valid_night(sleep_data)
  model_profile = UserProfile.model_validate(profile)
  latest_model = SleepResult.model_validate(latest) if latest else None
  daily_display = daily_score(latest_model, model_profile) if latest_model else None
  latest_ts = int(latest["timestamp"]) if latest.get("timestamp") else None
  today = datetime.date.fromisoformat(date)
  # 周/月窗口：dump 未传 start/end，服务端缺省锚定最近有效夜（build_sleep_week/month），
  # 对账窗口同口径以 anchor 为终点；零有效夜回退 --date
  anchor = _date_of(latest_ts, tz) if latest_ts else today
  week_start = (anchor - datetime.timedelta(days=6)).isoformat()
  week_end = anchor.isoformat()
  month_start = (anchor - datetime.timedelta(days=29)).isoformat()
  # 锚定 7 天使用最多的场景名（服务端 _week_top_scene 同口径），overview/week/explore 共用
  week_top = (_top_scenes(profile.get("mindora_record"), 7, 1, end_ts=latest_ts) or [None])[0]

  def check_sleep_eq(screen: str, path: str, actual, expected, note: str = ""):
    """依赖 sleep_data 源数据的对账；未拉取 sleep_data 时跳过比对（避免误报 ❌）。"""
    if not has_sleep_source:
      c.add(screen, path, actual, "➖ 未拉取 sleep_data（--sleep-data-count N 可对账）")
    else:
      c.check_eq(screen, path, actual, expected, note)

  # ── 概览 ──
  d = responses["analysis_overview"].get("data") or {}
  sc = (d.get("overall_score") or {}).get("score")
  check_sleep_eq("概览 overview", "overall_score.score", sc, daily_display["score"] if daily_display else None, "至少三个可计算维度")
  if sc is None:
    c.add("概览 overview", "overall_score", "(缺省)", "➖ 窗口内无数据，app 应显示 --")
  wb = d.get("weekly_best") or {}
  if wb.get("audio_name") is not None or week_top is not None:
    check_sleep_eq("概览 overview", "weekly_best.audio_name", wb.get("audio_name"), week_top, "锚定 7 天使用最多场景")
  selected_scene = _week_top_scene(model_profile)
  if selected_scene:
    scene_score = _week_scene_score(model_profile, selected_scene)
    check_sleep_eq("概览 overview", "weekly_best.score", wb.get("score"), scene_score["score"], "关联睡眠分或使用匹配分")
    check_sleep_eq("概览 overview", "weekly_best.score_type", wb.get("score_type"), scene_score["score_type"])
  si = d.get("sleep_insight") or {}
  c.check_text("概览 overview", "sleep_insight.title", si.get("title"))
  c.check_text("概览 overview", "sleep_insight.description", si.get("description"))

  # ── 日 ──
  d = responses["analysis_sleep_day"].get("data") or {}
  sc = (d.get("score_summary") or {}).get("score")
  day_index = compute_insight_indices(model_profile, compute_baselines(model_profile, tz))["onset_index"] if latest else None
  check_sleep_eq("睡眠日 day", "score_summary.score", sc, day_index, "当夜入睡表现指数；估算值限幅")
  ssc = d.get("sleep_scenarios") or {}
  c.check_text("睡眠日 day", "sleep_scenarios.title", ssc.get("title"))
  c.check_text("睡眠日 day", "sleep_scenarios.description", ssc.get("description"))
  for stage in ("awake", "rem", "core", "deep"):
    c.check_text("睡眠日 day", f"stage_insights.{stage}.description", (d.get("stage_insights") or {}).get(stage, {}).get("description"))

  # ── 周 / 月 ──
  for rt, start, label in (("analysis_sleep_week", week_start, "周"), ("analysis_sleep_month", month_start, "月")):
    d = responses[rt].get("data") or {}
    sc = (d.get("score_summary") or {}).get("score")
    records = _window_records(sleep_data, start, week_end, tz)
    scores = [daily_score(r, model_profile) for r in records]
    expected_score = period_score(records, model_profile)
    check_sleep_eq(f"睡眠{label}", "score_summary.score", sc, expected_score["score"] if expected_score else None, f"{label}窗口同版本平均（锚定）")
    c.check_text(f"睡眠{label}", "score_summary.label", (d.get("score_summary") or {}).get("label"))
    duration = mean_sleep_duration(records)
    if sc is not None and duration is not None and duration < 300 and has_sleep_source:
      c.check_eq(f"睡眠{label}", "score_summary.score<60", sc < 60, True, "平均睡眠不足5小时")
      expected_label = "睡眠不足" if language in {"zh-Hans", "zh-Hant"} else "Insufficient Sleep"
      c.check_eq(f"睡眠{label}", "score_summary.label", (d.get("score_summary") or {}).get("label"), expected_label)

    tr = d.get("sleep_trends") or {}
    c.check_text(f"睡眠{label}", "sleep_trends.body", tr.get("body"))
    c.check_text(f"睡眠{label}", "sleep_trends.description", tr.get("description"), required=bool(records))
    if rt == "analysis_sleep_month":
      series = tr.get("score_series") or []
      expected_len = sum(score is not None for score in scores)
      check_sleep_eq(f"睡眠{label}", "sleep_trends.score_series.length", len(series), expected_len, "窗口内有分天数")
      expected_series = [{"date": _date_of(r.timestamp, tz).isoformat(), **score}
                         for r, score in zip(records, scores) if score]
      check_sleep_eq(f"睡眠{label}", "sleep_trends.score_series", series, expected_series, "时长约束后的逐夜分数")
      c.check_text(f"睡眠{label}", "onset_efficiency.description", (d.get("onset_efficiency") or {}).get("description"))
      sl = (d.get("onset_efficiency") or {}).get("scenario_list")
      if sl:
        c.check_eq(f"睡眠{label}", "onset_efficiency.scenario_list", sl,
                   _top_scenes(profile.get("mindora_record"), 30, 3, end_ts=latest_ts), "mindora_record 锚定 30 天 top3")
    else:
      oe = d.get("onset_efficiency") or {}
      if oe.get("scenario_name") is not None or week_top is not None:
        check_sleep_eq(f"睡眠{label}", "onset_efficiency.scenario_name", oe.get("scenario_name"), week_top, "锚定 7 天使用最多场景")

  # ── Home/Day 共用睡眠估算指标 ──
  expected_metrics = build_sleep_metrics(SleepResult.model_validate(latest), UserProfile.model_validate(profile), tz) if latest else None
  for rt in ("analysis_overview", "analysis_sleep_day"):
    metrics = (responses[rt].get("data") or {}).get("sleep_metrics")
    if has_sleep_source:
      c.check_eq(rt, "sleep_metrics", metrics, expected_metrics, "真实inBed优先，否则按当夜分段估算")
    if metrics:
      for field in ("sleep_onset_minutes", "time_in_bed_minutes"):
        value = metrics.get(field)
        c.check_eq(rt, f"sleep_metrics.{field}.type", value is None or type(value) is int, True, "整数分钟或null")
      c.check_eq(rt,"sleep_metrics.source.valid",metrics.get("source") in {"measured","estimated"},True)
  c.check_eq("Home/Day", "sleep_metrics.same", (responses["analysis_overview"].get("data") or {}).get("sleep_metrics"),
             (responses["analysis_sleep_day"].get("data") or {}).get("sleep_metrics"), "两接口全量请求结构和值相同")

  # ── 探索 ──
  d = responses["analysis_explore"].get("data") or {}
  # data_ready 已废弃：依据返回分数决定数值校验，并独立校验文案。
  c.add("探索 explore", "data_ready", d.get("data_ready"), "➖ 已废弃，不参与显示判定")
  has_scores = any(v is not None for k,v in (d.get("score_summary") or {}).items()
                   if k == "score" or k.endswith("_score")) or any(
                     (d.get(k) or {}).get("score") is not None
                     for k in ("onset_efficiency", "sleep_structure", "night_fluctuation"))
  if has_scores:
    ss = d.get("score_summary") or {}
    indices = compute_insight_indices(model_profile, compute_baselines(model_profile, tz))
    check_sleep_eq("探索 explore", "score_summary.score", ss.get("score"), daily_display["score"] if daily_display else None, "最新有效夜版本化产品分")
    check_sleep_eq("探索 explore", "score_summary.efficiency_score", ss.get("efficiency_score"), indices["onset_index"], "入睡表现指数")
    check_sleep_eq("探索 explore", "score_summary.structure_score", ss.get("structure_score"), indices["structure_index"], "睡眠结构指数")
    check_sleep_eq("探索 explore", "score_summary.fluctuation_score", ss.get("fluctuation_score"), indices["stability_index"], "夜间稳定度")
    nf = d.get("night_fluctuation") or {}
    hr_min, hr_max = latest.get("hr_min"), latest.get("hr_max")
    expected_hr = f"{int(hr_min)}-{int(hr_max)}bpm" if hr_min is not None and hr_max is not None else None
    check_sleep_eq("探索 explore", "night_fluctuation.heart_rate_range", nf.get("heart_rate_range"), expected_hr, "当夜 hr_min/hr_max")
    check_sleep_eq("探索 explore", "onset_efficiency.onset_minutes", (d.get("onset_efficiency") or {}).get("onset_minutes"),
                   (build_sleep_metrics(latest_model, model_profile, tz) or {}).get("sleep_onset_minutes"), "与 Home/Day 同一入睡估算口径")
    sp = d.get("scene_preference") or {}
    if sp.get("scene_name") is not None or week_top is not None:
      check_sleep_eq("探索 explore", "scene_preference.scene_name", sp.get("scene_name"), week_top, "锚定 7 天使用最多场景")
    for field in ("intro_text", "intro_detail_text"):
      c.check_text("探索 explore", f"header_summary.{field}",
                   (d.get("header_summary") or {}).get(field), required=field == "intro_text")
    for mod in ("onset_efficiency", "sleep_structure", "night_fluctuation", "sleep_advice"):
      c.check_text("探索 explore", f"{mod}.description", (d.get(mod) or {}).get("description"), required=True)
    if d.get("scene_preference"):
      c.check_text("探索 explore", "scene_preference.description", d["scene_preference"].get("description"))
    check_sleep_eq("探索 explore", "sleep_structure.score", (d.get("sleep_structure") or {}).get("score"),
                   indices["structure_index"], "与顶部结构分同口径")
  for legacy in ("insight", "insight_overview"):
    c.check_eq("探索 explore", f"schema.{legacy}_absent", legacy not in d, True, "接口已改为外层卡片结构")

  return c


def deployment_diagnostic(responses: dict[str, dict]) -> str | None:
  """Identify the old response contract before attributing failures to scoring."""
  overview = (responses.get("analysis_overview") or {}).get("data") or {}
  month = (responses.get("analysis_sleep_month") or {}).get("data") or {}
  explore = (responses.get("analysis_explore") or {}).get("data") or {}
  missing = []
  if overview.get("overall_score") and "score_version" not in overview["overall_score"]:
    missing.append("overall_score.score_version")
  if overview.get("sleep_metrics") and "sleep_onset_source" not in overview["sleep_metrics"]:
    missing.append("sleep_metrics.sleep_onset_source")
  if month.get("score_summary") and "score_version" not in month["score_summary"]:
    missing.append("month.score_summary.score_version")
  if explore.get("score_summary") and "index_version" not in explore["score_summary"]:
    missing.append("explore.score_summary.index_version")
  if len(missing) < 2:
    return None
  return ("响应缺少新版字段（" + ", ".join(missing) + "）；目标服务很可能仍运行旧版代码。"
          "请核对请求地址、部署文件和服务进程，并在加载新版代码后重跑对账。")


def source_data_diagnostic(profile: dict, responses: dict[str, dict], previous_revision: int | None = None) -> str | None:
  """Explain a missing Weekly Best when the queried profile has lost its inputs."""
  overview = (responses.get("analysis_overview") or {}).get("data") or {}
  if overview.get("weekly_best"):
    return None
  plays = sum(len(entries) for entries in (profile.get("mindora_record") or {}).values()
              if isinstance(entries, list))
  if plays:
    return None
  revision = profile.get("revision")
  nights = profile.get("sleep_data") or []
  latest = max((row.get("timestamp", 0) for row in nights if isinstance(row, dict)), default=0)
  message = (f"weekly_best 缺失：本次 query_profile 的 mindora_record 有 0 条播放，"
             f"revision={revision}、sleep_data={len(nights)} 晚、最新睡眠时间戳={latest}。"
             "当前响应无法从这些数据计算场景名称、使用次数或分数。")
  if isinstance(revision, int) and previous_revision is not None and revision < previous_revision:
    message += (f"同 UID 的旧导出曾有更高 revision={previous_revision}；"
                "请核对服务地址、RUN_DIR/数据库目录，或排查画像回退。")
  return message


def render_report(checker: Checker, uid: str, date: str, out_files: dict[str, str],
                  diagnostic: str | None = None, source: str | None = None,
                  source_url: str | None = None) -> str:
  lines = [
    f"# /analysis 字段对照表 — uid={uid} date={date}",
    "",
    "app 逐屏核对时按本表路径取值；✅=已与源数据自动对账，📝=文案需人工核对，➖=缺省（app 应显示 --/空态），❌=不一致（bug）。",
    "",
  ]
  if source_url:
    lines += [f"数据源：`{source_url}`", ""]
  if source:
    lines += [f"数据源诊断：{source}", ""]
  lines += [
    "| 界面 | JSON 路径 | 值 | 核对 |",
    "|---|---|---|---|",
  ]
  for screen, path, value, verdict in checker.rows:
    v = value if len(value) <= 60 else value[:57] + "..."
    lines.append(f"| {screen} | `{path}` | {v} | {verdict} |")
  lines += ["", "## 原始响应文件", ""]
  for name, p in out_files.items():
    lines.append(f"- {name}: `{p}`")
  fails = [r for r in checker.rows if r[3].startswith("❌")]
  lines += ["", f"## 结论: {'❌ ' + str(len(fails)) + ' 处不一致' if fails else '✅ 数值字段全部与源数据一致'}", ""]
  if diagnostic:
    lines += [f"版本诊断：{diagnostic}", ""]
  return "\n".join(lines)


def _unwrap(wrapped: dict) -> dict:
  """UserServerClient._post 把响应包在 {status_code, request, response} 里；取内层业务响应体。"""
  body = wrapped.get("response")
  return body if isinstance(body, dict) else {"code": -1, "error": body}


def _uid_from_jwt(token: str) -> str | None:
  """从 JWT payload 解析 uid（不验签，仅用于输出目录命名）。"""
  try:
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)  # base64url 补齐
    import base64
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload.get("uid") or payload.get("sub")
  except Exception:
    return None


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--base-url", default=os.getenv("APP_SERVER_URL", "http://127.0.0.1:9001"))
  parser.add_argument("--host", default=None, help="user_server IP；传入后覆盖 --base-url")
  parser.add_argument("--port", type=int, default=9001)
  parser.add_argument("--jwt-token", default=os.getenv("JWT_TOKEN", ""))
  parser.add_argument("--uid", default=None,
                      help="不传时：有 jwt-token 则从 token payload 解析 uid，否则回退 mindora_test_uid1")
  parser.add_argument("--date", default=datetime.date.today().isoformat(), help="与 app 展示日期保持一致")
  parser.add_argument("--language", default="zh-Hans")
  parser.add_argument("--timezone", default="Asia/Shanghai")
  parser.add_argument("--include-sop-tag-profile", action="store_true", help="query_profile 返回 SOP 标签画像")
  parser.add_argument("--include-sop-recommendation-details", action="store_true", help="query_profile 返回 SOP 推荐详情")
  parser.add_argument("--out", default=None)
  parser.add_argument("--include-sleep-data", action="store_true",
                      help="[已废弃] 现在默认就拉取 sleep_data，保留仅为兼容旧用法")
  parser.add_argument("--sleep-data-count", type=int, default=30,
                      help="query_profile 携带最近 N 晚 sleep_data（默认 30；0=不拉）")
  args = parser.parse_args()

  # uid 只用于输出目录命名/报告标注；带 jwt 时服务端按 token 里的 uid 取数，
  # 这里从 token payload 解析出真实 uid，避免目录名误标成默认 debug uid
  uid = args.uid or _uid_from_jwt(args.jwt_token) or "mindora_test_uid1"

  if args.host and "://" in args.host:
    # 容错：--host 误传完整 URL 时按 base-url 处理（正确用法是 --base-url）
    base_url = args.host
  else:
    base_url = f"http://{args.host}:{args.port}" if args.host else args.base_url
  client = UserServerClient(
    base_url=base_url, jwt_token=args.jwt_token, uid=uid,
    language=args.language, timezone=args.timezone,
  )

  out_dir = Path(args.out or f"out_analysis/{uid}_{int(time.time())}")
  out_dir.mkdir(parents=True, exist_ok=True)

  print(f"target: {base_url}  uid={uid}  date={args.date}")
  profile_resp = _unwrap(client.query_profile(
    sleep_data_count=args.sleep_data_count, include_behaviors=False,
    include_sop_tag_profile=args.include_sop_tag_profile,
    include_sop_recommendation_details=args.include_sop_recommendation_details,
  ))
  profile = ((profile_resp.get("data") or {}).get("user_profile")) or {}
  (out_dir / "query_profile.json").write_text(json.dumps(profile_resp, ensure_ascii=False, indent=2))

  responses: dict[str, dict] = {}
  for rt in ANALYSIS_TYPES:
    resp = _unwrap(client._analysis_request(rt, {"date": args.date}))
    responses[rt] = resp
    (out_dir / f"{rt}.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2))
    code = resp.get("code")
    print(f"  {rt}: code={code}, keys={sorted((resp.get('data') or {}).keys())}")
    if code != 0:
      print(f"    ⚠️ {json.dumps(resp, ensure_ascii=False)[:200]}")

  checker = run_checks(profile, responses, args.date, has_sleep_source=args.sleep_data_count > 0,
                       timezone=args.timezone, language=args.language)
  diagnostic = deployment_diagnostic(responses)
  previous_revision = None
  for old_path in out_dir.parent.glob(f"{uid}_*/query_profile.json"):
    if old_path.parent == out_dir:
      continue
    try:
      old_profile = ((json.loads(old_path.read_text()).get("data") or {}).get("user_profile") or {})
      old_revision = old_profile.get("revision")
      if isinstance(old_revision, int):
        previous_revision = max(previous_revision or 0, old_revision)
    except (OSError, ValueError):
      continue
  source = source_data_diagnostic(profile, responses, previous_revision)
  report = render_report(checker, uid, args.date,
                         {p.name: str(p) for p in sorted(out_dir.glob('*.json'))},
                         diagnostic, source, base_url)
  (out_dir / "CHECK_REPORT.md").write_text(report)

  fails = [r for r in checker.rows if r[3].startswith("❌")]
  print(f"\n对账: {len(checker.rows) - len(fails)}/{len(checker.rows)} 项通过"
        + (f"，❌ {len(fails)} 处不一致:" if fails else "，全部一致"))
  for screen, path, value, verdict in fails:
    print(f"  ❌ [{screen}] {path}: {value} — {verdict}")
  if diagnostic:
    print(f"\n版本诊断：{diagnostic}")
  if source:
    print(f"\n数据源诊断：{source}")
  print(f"\n对照表: {out_dir / 'CHECK_REPORT.md'}")
  sys.exit(1 if fails else 0)


if __name__ == "__main__":
  main()
