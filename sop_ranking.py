"""Deterministic SOP ranking from existing, explicitly labelled evidence.

Rules express editorial content suitability, not medical efficacy. Repeated exposure
never becomes a preference score; historical outcome means remain associations.
"""
import json
import math
from functools import lru_cache
from pathlib import Path
from statistics import mean

from content_tags import canonical_cmd, catalog_by_command


@lru_cache(maxsize=1)
def rules():
    return json.loads((Path(__file__).parent / "data/content_tags/recommendation_rules.json").read_text(encoding="utf-8"))


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def rank_sops(profile, commands):
    config = rules()
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
        historical = preference_score + outcome_score - penalty
        rows.append({"cmd_name": command, "annotated": content is not None,
                     "score": round(historical + goal_score, 4), "historical_score": round(historical, 4),
                     "components": {"profile_goal": goal_score, "explicit_preference": round(preference_score, 4),
                                    "outcome_association": round(outcome_score, 4), "risk_penalty": penalty},
                     "matched_tags": sorted(matched), "evidence": evidence})
    return sorted(rows, key=lambda row: -row["score"])
