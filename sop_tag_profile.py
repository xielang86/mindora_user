"""Rebuild bounded history evidence, keeping exposure, preference and outcomes separate."""
from collections import defaultdict
from content_tags import canonical_cmd, catalog_by_command


def rebuild_sop_tag_profile(profile):
    catalog = catalog_by_command()
    sessions = {}
    unknown = set()
    for cmd, records in profile.mindora_record.items():
        command = canonical_cmd(cmd)
        content = catalog.get(command)
        for record in records:
            try:
                timestamp = int(record[0])
            except (ValueError, TypeError, IndexError, OverflowError):
                continue
            if content is None:
                unknown.add(command)
            key = f"{command}@{timestamp}"
            sessions[key] = {"usage_id": key, "cmd_name": command, "started_at": timestamp,
                             "content_id": content["content_id"] if content else None, "tags": content["tags"] if content else []}
    exposures = defaultdict(lambda: {"usage_count": 0, "last_used_at": 0})
    for session in sessions.values():
        for tag in session["tags"]:
            exposures[tag]["usage_count"] += 1
            exposures[tag]["last_used_at"] = max(exposures[tag]["last_used_at"], session["started_at"])

    # Timestamp is update time, so never use it as the sleep window. Require stages.
    links = []
    effects = defaultdict(lambda: {"sleep_quality": [], "onset_minutes": []})
    seen_windows = set()
    for sleep in profile.sleep_data:
        stages = [s for s in sleep.sleep_status if s.duration > 0]
        if not stages:
            continue
        start = min(s.start_time for s in stages)
        end = max(s.start_time + s.duration * 60 for s in stages)
        window = (start, end)
        if window in seen_windows:
            continue
        seen_windows.add(window)
        # Conservative inferred link: SOP starts within 2h before sleep or during it.
        usages = [s for s in sessions.values() if start - 7200 <= s["started_at"] < end]
        if not usages:
            continue
        links.append({"sleep_window_start": start, "sleep_window_end": end,
                      "sleep_record_timestamp": sleep.timestamp,
                      "usage_ids": [s["usage_id"] for s in usages],
                      "association_method": "stage_window_inferred",
                      "ambiguous": len(usages) != 1})
        if len(usages) != 1:
            continue
        for tag in usages[0]["tags"]:
            for metric, value in [("sleep_quality", sleep.sleep_quality), ("onset_minutes", sleep.onset)]:
                if value is not None:
                    effects[tag][metric].append(value)
    profile.sop_tag_profile = {
        "schema_version": 1, "history_scope": "retained_mindora_record",
        "tag_exposure": dict(exposures),
        "tag_preferences": {},
        "preference_status": "insufficient_explicit_content_feedback",
        "tag_outcome_associations": {tag: {metric: {"sample_count": len(values),
            "mean": sum(values) / len(values)} for metric, values in metrics.items() if values}
            for tag, metrics in effects.items()},
        "sleep_usage_links": links,
        "unmapped_commands": sorted(unknown),
        "limitations": ["Exposure is not preference; content goals are not measured effects.",
                        "Window links are inferred, not causal; multi-use nights excluded from tag outcomes.",
                        "Guide variant and actual tag audibility are unknown in legacy usage records."]}
