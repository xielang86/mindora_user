"""dump_analysis.py — 用与 app 相同的 uid/参数拉取全部 /analysis 响应 + query_profile，
落地成 JSON 并自动与画像源数据对账，生成逐屏字段对照表（CHECK_REPORT.md）。

用途：app 上逐屏核对字段时，不用在 raw profile 里翻 —— app 渲染的是
/analysis 响应（骨架 + LLM 库存报告 deep_merge 后的结果），本工具把
"app 每个界面元素 → JSON 路径 → 值 → 与源数据对账结果" 整理成一张表。

用法：
  python tool/dump_analysis.py --uid <uid> --host 192.168.x.x
  python tool/dump_analysis.py --jwt-token <token> --base-url http://127.0.0.1:9001

输出目录（默认 out_analysis/<uid>_<ts>/）：
  query_profile.json        完整画像（含 LLM 结果 sleep_insight / analysis_reports）
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

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tool.user_server_client import UserServerClient
from user_profile import short_scene_id

ANALYSIS_TYPES = [
  "analysis_overview",
  "analysis_sleep_day",
  "analysis_sleep_week",
  "analysis_sleep_month",
  "analysis_explore",
]


# ── 与服务端 analysis_builders 同口径的源数据计算 ──────────────────

def _date_of(ts: int) -> datetime.date:
  return datetime.date.fromtimestamp(ts)


def _window_scores(sleep_data: list, start: str, end: str) -> list[float]:
  start_d = datetime.date.fromisoformat(start)
  end_d = datetime.date.fromisoformat(end)
  return [
    r["sleep_quality"] for r in sleep_data
    if r.get("sleep_quality") is not None and start_d <= _date_of(r["timestamp"]) <= end_d
  ]


def _top_scenes(mindora_record: dict, days: int, limit: int) -> list[str]:
  cutoff = int(time.time()) - days * 86400
  counts = {}
  for scene_id, records in (mindora_record or {}).items():
    # 与服务端 short_scene_id 同口径：strip 所有已知前缀（sleep.scene. / sleep.pure_music. 等）
    name = short_scene_id(scene_id).replace("_", " ").title()
    cnt = sum(
      1 for e in (records or [])
      if isinstance(e, (list, tuple)) and e and int(e[0]) >= cutoff
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

  def check_text(self, screen: str, path: str, value):
    """文案字段：来自 LLM 或空串降级，无法对账，只标注来源。"""
    if value in (None, ""):
      self.add(screen, path, value, "➖ 空（无 LLM 报告，app 应显示空/--）")
    else:
      self.add(screen, path, value, "📝 文案（LLM 生成或骨架兜底，人工核对语言/语义）")


def run_checks(profile: dict, responses: dict[str, dict], date: str, has_sleep_source: bool = True) -> Checker:
  c = Checker()
  sleep_data = profile.get("sleep_data") or []
  latest = sleep_data[-1] if sleep_data else {}
  today = datetime.date.fromisoformat(date)
  week_start = (today - datetime.timedelta(days=6)).isoformat()
  month_start = (today - datetime.timedelta(days=29)).isoformat()

  def check_sleep_eq(screen: str, path: str, actual, expected, note: str = ""):
    """依赖 sleep_data 源数据的对账；未拉取 sleep_data 时跳过比对（避免误报 ❌）。"""
    if not has_sleep_source:
      c.add(screen, path, actual, "➖ 未拉取 sleep_data（加 --include-sleep-data 可对账）")
    else:
      c.check_eq(screen, path, actual, expected, note)

  # ── 概览 ──
  d = responses["analysis_overview"].get("data") or {}
  sc = (d.get("overall_score") or {}).get("score")
  scores = _window_scores(sleep_data, week_start, date)
  if sc is not None:
    check_sleep_eq("概览 overview", "overall_score.score", sc, int(round(sum(scores) / len(scores))) if scores else None, "7 天窗口平均")
  else:
    c.add("概览 overview", "overall_score", "(缺省)", "➖ 窗口内无数据，app 应显示 --")
  wb = d.get("weekly_best") or {}
  c.add("概览 overview", "weekly_best.audio_name", wb.get("audio_name"), "📝 应对应 sleep_analysis.most_used_scene_7d.scene_name")
  si = d.get("sleep_insight") or {}
  c.check_text("概览 overview", "sleep_insight.title", si.get("title"))
  c.check_text("概览 overview", "sleep_insight.description", si.get("description"))

  # ── 日 ──
  d = responses["analysis_sleep_day"].get("data") or {}
  sc = (d.get("score_summary") or {}).get("score")
  if sc is not None:
    check_sleep_eq("睡眠日 day", "score_summary.score", sc, latest.get("sleep_quality") and int(latest["sleep_quality"]), "当夜 sleep_quality")
  ssc = d.get("sleep_scenarios") or {}
  c.check_text("睡眠日 day", "sleep_scenarios.title", ssc.get("title"))
  c.check_text("睡眠日 day", "sleep_scenarios.description", ssc.get("description"))
  for stage in ("awake", "rem", "core", "deep"):
    c.check_text("睡眠日 day", f"stage_insights.{stage}.description", (d.get("stage_insights") or {}).get(stage, {}).get("description"))

  # ── 周 / 月 ──
  for rt, start, label in (("analysis_sleep_week", week_start, "周"), ("analysis_sleep_month", month_start, "月")):
    d = responses[rt].get("data") or {}
    sc = (d.get("score_summary") or {}).get("score")
    scores = _window_scores(sleep_data, start, date)
    if sc is not None:
      check_sleep_eq(f"睡眠{label}", "score_summary.score", sc, int(round(sum(scores) / len(scores))) if scores else None, f"{label}窗口平均")
    c.check_text(f"睡眠{label}", "score_summary.label", (d.get("score_summary") or {}).get("label"))
    tr = d.get("sleep_trends") or {}
    c.check_text(f"睡眠{label}", "sleep_trends.body", tr.get("body"))
    c.check_text(f"睡眠{label}", "sleep_trends.description", tr.get("description"))
    if rt == "analysis_sleep_month":
      series = tr.get("score_series") or []
      expected_len = len(scores)
      check_sleep_eq(f"睡眠{label}", "sleep_trends.score_series.length", len(series), expected_len, "窗口内有分天数")
      sl = (d.get("onset_efficiency") or {}).get("scenario_list")
      if sl:
        c.check_eq(f"睡眠{label}", "onset_efficiency.scenario_list", sl, _top_scenes(profile.get("mindora_record"), 30, 3), "mindora_record 30 天 top3")
    else:
      oe = d.get("onset_efficiency") or {}
      c.add(f"睡眠{label}", "onset_efficiency.scenario_name", oe.get("scenario_name"), "📝 应对应 most_used_scene_7d")

  # ── 探索 ──
  d = responses["analysis_explore"].get("data") or {}
  c.add("探索 explore", "data_ready", d.get("data_ready"), ("✅" if d.get("data_ready") == bool(sleep_data) else "❌ 与 sleep_data 是否为空不符") if has_sleep_source else "➖ 未拉取 sleep_data，无法核对")
  if d.get("data_ready"):
    ss = d.get("score_summary") or {}
    check_sleep_eq("探索 explore", "score_summary.score", ss.get("score"), latest.get("sleep_quality") and int(latest["sleep_quality"]), "当夜 sleep_quality")
    check_sleep_eq("探索 explore", "score_summary.efficiency_score", ss.get("efficiency_score"), latest.get("soe") and int(latest["soe"]), "当夜 soe")
    check_sleep_eq("探索 explore", "score_summary.structure_score", ss.get("structure_score"), latest.get("sleep_arch_index") and int(latest["sleep_arch_index"]), "当夜 sleep_arch_index")
    check_sleep_eq("探索 explore", "score_summary.fluctuation_score", ss.get("fluctuation_score"), latest.get("night_var_index") and int(latest["night_var_index"]), "当夜 night_var_index")
    nf = d.get("night_fluctuation") or {}
    hr_min, hr_max = latest.get("hr_min"), latest.get("hr_max")
    expected_hr = f"{int(hr_min)}-{int(hr_max)}bpm" if hr_min is not None and hr_max is not None else None
    check_sleep_eq("探索 explore", "night_fluctuation.heart_rate_range", nf.get("heart_rate_range"), expected_hr, "当夜 hr_min/hr_max")
    check_sleep_eq("探索 explore", "onset_efficiency.onset_minutes", (d.get("onset_efficiency") or {}).get("onset_minutes"), latest.get("onset") and int(latest["onset"]), "当夜 onset")
    sp = d.get("scene_preference") or {}
    c.add("探索 explore", "scene_preference.scene_name", sp.get("scene_name"), "📝 应对应 most_used_scene_7d")
    # M27 洞察数据：三个展示指数（规则现算，不经过 LLM）
    io_ = d.get("insight_overview") or {}
    if io_:
        idx_ok = all(io_.get(k) is None or 0 <= io_.get(k) <= 100
                     for k in ("onset_index", "structure_index", "stability_index"))
        c.add("探索 explore", "insight_overview",
              {k: io_.get(k) for k in ("onset_index", "structure_index", "stability_index", "data_state")},
              "✅ 指数在 0-100（规则现算）" if idx_ok else "❌ 指数越界")
    else:
        c.add("探索 explore", "insight_overview", "(缺省)",
              "➖ 数据不足无指数" if not has_sleep_source else "❌ 有睡眠数据应返回 insight_overview")
    for mod in ("header_summary", "onset_efficiency", "sleep_structure", "night_fluctuation", "scene_preference", "sleep_advice"):
      c.check_text("探索 explore", f"{mod}.description/intro", json.dumps({k: v for k, v in (d.get(mod) or {}).items() if isinstance(v, str) and v}, ensure_ascii=False) or "(空)")
    insight = d.get("insight")
    if insight:
      for key in ("greeting", "onset", "architecture", "intervention", "scene_preference", "micro_education"):
        m = insight.get(key)
        if m is None:
          c.add("探索 explore", f"insight.{key}", "(隐藏)", "➖ visible=False，app 不应展示该模块")
        else:
          c.check_text("探索 explore", f"insight.{key}.title", m.get("title"))
    else:
      c.add("探索 explore", "insight", None, "➖ 无洞察报告（LLM 未生成或已过期）")

  return c


def render_report(checker: Checker, uid: str, date: str, out_files: dict[str, str]) -> str:
  lines = [
    f"# /analysis 字段对照表 — uid={uid} date={date}",
    "",
    "app 逐屏核对时按本表路径取值；✅=已与源数据自动对账，📝=文案需人工核对，➖=缺省（app 应显示 --/空态），❌=不一致（bug）。",
    "",
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
  parser.add_argument("--out", default=None)
  parser.add_argument("--include-sleep-data", action="store_true",
                      help="query_profile 携带 sleep_data（默认不拉；behaviors 始终不拉）。"
                           "要对账数值字段（评分/hr_range/onset 等）时需要打开")
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
    include_sleep_data=args.include_sleep_data, include_behaviors=False,
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

  checker = run_checks(profile, responses, args.date, has_sleep_source=args.include_sleep_data)
  report = render_report(checker, uid, args.date,
                         {p.name: str(p) for p in sorted(out_dir.glob('*.json'))})
  (out_dir / "CHECK_REPORT.md").write_text(report)

  fails = [r for r in checker.rows if r[3].startswith("❌")]
  print(f"\n对账: {len(checker.rows) - len(fails)}/{len(checker.rows)} 项通过"
        + (f"，❌ {len(fails)} 处不一致:" if fails else "，全部一致"))
  for screen, path, value, verdict in fails:
    print(f"  ❌ [{screen}] {path}: {value} — {verdict}")
  print(f"\n对照表: {out_dir / 'CHECK_REPORT.md'}")
  sys.exit(1 if fails else 0)


if __name__ == "__main__":
  main()
