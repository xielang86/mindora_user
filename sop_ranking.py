"""Deterministic SOP ranking from existing, explicitly labelled evidence.

Rules express editorial content suitability, not medical efficacy. Repeated exposure
never becomes a preference score; historical outcome means remain associations.
"""
import json
import math
import random
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from statistics import mean

from content_tags import canonical_cmd, catalog_by_command
from sop_tag_profile import sop_play_events


@lru_cache(maxsize=1)
def rules():
    return json.loads((Path(__file__).parent / "data/content_tags/recommendation_rules.json").read_text(encoding="utf-8"))


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def recent_play_feedback(profile, now):
    config = rules()["play_feedback"]
    cutoff = now - config["lookback_days"] * 86400
    sessions = {}
    for cmd, started, event in sop_play_events((profile.behaviors or {}).get("plays")):
        duration = number(event.get("duration"))
        if cutoff <= started <= now and config["minimum_duration_seconds"] <= duration <= 16 * 3600:
            key = (canonical_cmd(cmd), started)
            sessions[key] = max(duration, sessions.get(key, 0))

    feedback = defaultdict(lambda: {"plays": 0, "short_plays": 0, "latest_short_play": 0})
    for (cmd, started), duration in sessions.items():
        counts = feedback[cmd]
        counts["plays"] += 1
        if duration <= config["short_duration_seconds"]:
            counts["short_plays"] += 1
            counts["latest_short_play"] = max(started, counts["latest_short_play"])
    return dict(feedback)


def short_play_refresh_due(profile, now):
    if not profile:
        return False
    recommendations = [*profile.standard_sop_reco, *(profile.sleep_scenarios_reco or [])]
    if not recommendations:
        return False
    config = rules()["play_feedback"]
    generated_at = number((profile.sop_recommendation_details or {}).get("generated_at"))
    if now - generated_at < config["refresh_min_interval_seconds"]:
        return False
    feedback = recent_play_feedback(profile, now)
    since = max(generated_at, now - config["refresh_max_event_age_seconds"])
    for scenario in recommendations:
        cmd = (scenario.stages[0].cmd_name if scenario.stages else None) or scenario.scenario_name
        counts = feedback.get(canonical_cmd(cmd)) if cmd else None
        if (counts and counts["short_plays"] >= config["minimum_short_plays"]
                and counts["latest_short_play"] > since):
            return True
    return False


def exploration_candidate(ranking, uid, now):
    """Sample at most one third-slot candidate, stable per account and UTC day."""
    if not uid or len(ranking) < 4:
        return None
    config = rules()["exploration"]
    rng = random.Random(f"{uid}:{now // 86400}")
    if rng.random() >= config["probability"]:
        return None
    allowed = set(config["device_commands"])
    score_floor = ranking[2]["score"] - config["max_score_gap"]
    eligible = [
        row for row in ranking[3:]
        if row["cmd_name"] in allowed and row["score"] >= score_floor
        and row["components"]["risk_penalty"] == row["components"]["short_play_penalty"] == 0
    ]
    if not eligible:
        return None
    top_types = {canonical_cmd(row["cmd_name"]).split(".")[1] for row in ranking[:2]}
    weights = [
        (2 if canonical_cmd(row["cmd_name"]).split(".")[1] not in top_types else 1)
        / (1 + row["recent_plays"])
        for row in eligible
    ]
    return rng.choices(eligible, weights=weights, k=1)[0]


def rank_sops(profile, commands, *, now=None):
    config = rules()
    feedback = recent_play_feedback(profile, int(time.time()) if now is None else now)
    play_config = config["play_feedback"]
    catalog = catalog_by_command()
    snapshot = profile.sop_tag_profile or {}
    effects = snapshot.get("tag_outcome_associations", {})
    preferences = snapshot.get("tag_preferences", {})
    active = [key for key, value in profile.long_term_profile if number(value) > 0]
    if profile.sleep_health:
        active.extend(filter(None, [profile.sleep_health.improvement_goal, profile.sleep_health.stress_type]))
    profile_text = " ".join(active)
    goals = [g for g in config["goals"] if any(term in profile_text for term in g["profile_terms"])]
    sensitive = any(term in active for term in config["sensitive_sound_terms"])
    # Only stage-backed nights contribute a reference. No synthetic HRV inference.
    windows = {}
    for sleep in profile.sleep_data:
        stages = [s for s in sleep.sleep_status if s.duration > 0]
        if stages:
            window = (min(s.start_time for s in stages), max(s.start_time + s.duration * 60 for s in stages))
            if window not in windows or sleep.timestamp > windows[window].timestamp:
                windows[window] = sleep
    nights = list(windows.values())
    baseline = {}
    for metric, field in [("sleep_quality", "sleep_quality"), ("onset_minutes", "onset")]:
        values = [number(getattr(s, field), None) for s in nights]
        values = [v for v in values if v is not None]
        if len(values) >= config["minimum_baseline_nights"]:
            baseline[metric] = mean(values)
    rows = []
    for command in commands:
        content = catalog.get(canonical_cmd(command))
        tags = content["tags"] if content else []
        matched, evidence = set(), []
        goal_score = 0
        for goal in goals:
            hits = [t for t in tags if any(term in t for term in goal["tag_terms"])]
            if hits:
                goal_score += goal["weight"]
                matched.update(hits)
                evidence.append({"type": "profile_goal", "goal": goal["id"], "tags": hits})
        pref_scores, outcome_scores = [], []
        for tag in tags:
            pref = preferences.get(tag, {})
            if isinstance(pref, dict) and pref.get("source") == "explicit":
                value = max(-1, min(1, number(pref.get("preference_score"))))
                pref_scores.append(value)
                if value:
                    evidence.append({"type": "explicit_preference", "tag": tag, "score": value})
            metrics = effects.get(tag, {})
            if not isinstance(metrics, dict):
                continue
            metric_scores = []
            for metric, reference in baseline.items():
                sample = metrics.get(metric, {})
                if not isinstance(sample, dict):
                    continue
                count = number(sample.get("sample_count"))
                observed = number(sample.get("mean"), None)
                if count < config["minimum_outcome_samples"] or observed is None:
                    continue
                delta = observed - reference if metric == "sleep_quality" else reference - observed
                score = max(-1, min(1, delta / config["outcome_scales"][metric])) * count / (count + 5)
                metric_scores.append(score)
                evidence.append({"type": "outcome_association", "tag": tag, "metric": metric,
                                 "sample_count": count, "mean": observed, "reference_mean": reference})
            if metric_scores:
                outcome_scores.append(mean(metric_scores))
        # Average correlated tag evidence rather than rewarding verbose annotations.
        preference_score = mean(pref_scores) * config["preference_weight"] if pref_scores else 0
        outcome_score = mean(outcome_scores) * config["outcome_weight"] if outcome_scores else 0
        risk_hits = []
        if sensitive:
            risk_hits = [t for t in tags if t.startswith("animal_sound:") or
                         any(term in t for term in config["stimulating_tag_terms"]) or
                         (t.startswith("continuity:") and any(word in t for word in ("点状", "突发", "间歇")))]
        # Penalize two independent traits, not the number of synonymous tags.
        risk_groups = int(any(t.startswith("animal_sound:") for t in risk_hits)) + int(any(not t.startswith("animal_sound:") for t in risk_hits))
        penalty = config["sensitive_sound_penalty"] * risk_groups
        if sensitive and content is None:
            penalty = config["sensitive_unknown_content_penalty"]
            evidence.append({"type": "unknown_sound_metadata", "reason": "Cannot assess sound sensitivity compatibility"})
        if risk_hits:
            evidence.append({"type": "sound_sensitivity", "tags": risk_hits})
        counts = feedback.get(canonical_cmd(command), {"plays": 0, "short_plays": 0})
        short_plays = counts["short_plays"]
        short_penalty = 0
        if short_plays >= play_config["minimum_short_plays"]:
            short_penalty = min(play_config["maximum_penalty"], short_plays * play_config["short_play_weight"])
            short_penalty *= short_plays / counts["plays"]
            evidence.append({"type": "repeated_short_play", "short_plays": short_plays,
                             "recorded_plays": counts["plays"]})
        historical = preference_score + outcome_score - penalty - short_penalty
        rows.append({"cmd_name": command, "annotated": content is not None,
                     "recent_plays": counts["plays"],
                     "score": round(historical + goal_score, 4), "historical_score": round(historical, 4),
                     "components": {"profile_goal": goal_score, "explicit_preference": round(preference_score, 4),
                                    "outcome_association": round(outcome_score, 4), "risk_penalty": penalty,
                                    "short_play_penalty": round(short_penalty, 4)},
                     "matched_tags": sorted(matched), "evidence": evidence})
    return sorted(rows, key=lambda row: -row["score"])
