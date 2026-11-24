"""Offline exporter checks follow the public cards and duration-aware scores."""
import datetime
import sys
from types import SimpleNamespace

import pytest

from analysis_builders import build_overview, build_sleep_day, build_sleep_week, build_sleep_month, build_explore
from tool import dump_analysis
from tool.dump_analysis import deployment_diagnostic, run_checks, source_data_diagnostic, render_report
from user_profile import UserProfile, SleepResult, SleepElement, SleepPlan


def _case():
    ts=int(datetime.datetime(2026,9,14,18,tzinfo=datetime.timezone.utc).timestamp())
    p=UserProfile(last_request_timezone="Asia/Shanghai",last_request_language="zh-Hans",
        sleep_plan=SleepPlan(target_duration_min=450,target_bed_time="02:00",target_wake_time="06:00"),sleep_data=[
        SleepResult(timestamp=ts,sleep_quality=95,soe=96.4,onset=10,source="healthkit",sleep_arch_index=100,
            first_sleep_time="02:00",wake_time="06:00",in_bed_intervals=[(ts-260*60,ts)],
            sleep_status=[SleepElement(start_time=ts-240*60,duration=100,sleep_type="deep"),
                          SleepElement(start_time=ts-140*60,duration=140,sleep_type="rem")])])
    d=SimpleNamespace(date="2026-09-17",start_date=None,end_date=None,language="zh-Hans",timezone="Asia/Shanghai",modules=[])
    builders={"analysis_overview":build_overview,"analysis_sleep_day":build_sleep_day,
              "analysis_sleep_week":build_sleep_week,"analysis_sleep_month":build_sleep_month,"analysis_explore":build_explore}
    return p.model_dump(mode="json"), {k:{"data":v(d,p)} for k,v in builders.items()}


def test_current_schema_and_scores_pass_without_legacy_insight():
    p,r=_case()
    checker=run_checks(p,r,"2026-09-17",timezone="Asia/Shanghai")
    assert not [row for row in checker.rows if row[3].startswith("❌")]
    assert r["analysis_sleep_week"]["data"]["score_summary"]["label"] == "睡眠不足"
    assert r["analysis_explore"]["data"]["sleep_structure"]["score"] is not None


def test_empty_description_is_not_hidden_by_nonempty_date():
    p,r=_case()
    r["analysis_explore"]["data"]["onset_efficiency"]["description"]=""
    errors=[row for row in run_checks(p,r,"2026-09-17").rows if row[3].startswith("❌")]
    assert any(row[1]=="onset_efficiency.description" for row in errors)


def test_old_schema_and_uncapped_score_are_detected():
    p,r=_case()
    r["analysis_explore"]["data"]["insight"]={}
    r["analysis_sleep_week"]["data"]["score_summary"].update(score=95,label="良好")
    errors=[row for row in run_checks(p,r,"2026-09-17").rows if row[3].startswith("❌")]
    assert any(row[1]=="schema.insight_absent" for row in errors)
    assert any(row[1]=="score_summary.score<60" for row in errors)


def test_missing_source_skips_numeric_checks():
    p,r=_case()
    p["sleep_data"]=[]
    assert not [row for row in run_checks(p,r,"2026-09-17",has_sleep_source=False).rows if row[3].startswith("❌")]


def test_deprecated_ready_and_optional_intro_detail_do_not_fail():
    p,r=_case()
    r["analysis_explore"]["data"].pop("data_ready",None)
    r["analysis_explore"]["data"]["header_summary"]["intro_detail_text"]=""
    assert not [row for row in run_checks(p,r,"2026-09-17").rows if row[3].startswith("❌")]


def test_old_service_contract_is_identified_without_hiding_failures():
    p, responses = _case()
    assert deployment_diagnostic(responses) is None
    overview = responses["analysis_overview"]["data"]
    overview["overall_score"].pop("score_version")
    overview["sleep_metrics"].pop("sleep_onset_source")
    assert "仍运行旧版代码" in deployment_diagnostic(responses)
    failures = [row for row in run_checks(p, responses, "2026-09-17").rows
                if row[3].startswith("❌")]
    assert any(row[1] == "sleep_metrics" for row in failures)


def test_missing_weekly_best_reports_missing_source_and_revision_regression():
    profile, responses = _case()
    profile.update(revision=1, mindora_record={"sleep.scene.rain": []})
    warning = source_data_diagnostic(profile, responses, previous_revision=2286)
    assert warning is not None and "revision=1" in warning and "revision=2286" in warning
    report = render_report(run_checks(profile, responses, "2026-09-17"), "u", "2026-09-17", {},
                           source=warning, source_url="http://127.0.0.1:9001")
    assert "数据源诊断" in report and "127.0.0.1:9001" in report


def test_partial_fetch_stops_before_scoring_and_keeps_error_artifacts(monkeypatch, tmp_path):
    class FailedClient:
        def __init__(self, **kwargs):
            pass

        def query_profile(self, **kwargs):
            return {"status_code": None, "ok": False, "response": {"error": "connect timeout"}}

        def _analysis_request(self, request_type, data):
            return {"status_code": 200, "ok": True, "response": {"code": 0, "data": {}}}

    monkeypatch.setattr(dump_analysis, "UserServerClient", FailedClient)
    monkeypatch.setattr(dump_analysis, "run_checks", lambda *args, **kwargs: pytest.fail("must not score partial data"))
    monkeypatch.setattr(sys, "argv", ["dump_analysis", "--out", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        dump_analysis.main()
    assert exc.value.code == 2
    assert "query_profile" in (tmp_path / "FETCH_ERRORS.md").read_text()
    assert not (tmp_path / "CHECK_REPORT.md").exists()


def test_explore_score_without_latest_night_reports_error_instead_of_crashing():
    profile, responses = _case()
    profile["sleep_data"] = []
    errors = [row for row in run_checks(profile, responses, "2026-09-17").rows
              if row[3].startswith("❌")]
    assert any(row[1] == "source.sleep_data" for row in errors)
