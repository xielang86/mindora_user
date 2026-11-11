"""Versioned profile parts, with lossless legacy reads and atomic migration.

No model defaults are used by the storage codec. A partial read may only patch
its explicitly loaded parts; missing parts never mean empty model fields.
The caller holds the existing UID lock for every mutation.
"""
import copy
import json
from contextlib import contextmanager
from urllib.parse import quote


V4_GROUPS = {
    "internal:preferences": ("uid_emb", "long_term_profile"),
    "plans:state": ("sleep_plan", "sleep_plans", "sleep_plans_synced_at"),
    "internal:health_sync": ("health_sync_days", "health_sync_field_versions", "health_sync_timezone"),
    "sleep:records": ("sleep_data",),
    "internal:scene_history": ("mindora_record",),
    "internal:sop_tags": ("sop_tag_profile",),
    "internal:reco_details": ("sop_recommendation_details",),
    "recommendation:current": ("sleep_scenarios_reco", "standard_sop_reco"),
    "analysis:insight": ("sleep_insight",),
    "analysis:content": ("bedtime_advice", "analysis_reports"),
    "engagement:popup": ("popup_states", "inbox_messages"),
    "engagement:survey": ("survey_submissions",),
    "engagement:footprint": ("footprint_days",),
    "request_meta": ("last_request_timezone", "last_request_language", "last_request_at"),
}
V4_PARTS = frozenset(V4_GROUPS) | {
    "profile:details", "profile:avatar", "internal:health",
    "internal:interaction", "scene:summary", "analysis:memory", "internal:extra",
}
V5_MERGED_PARTS = {
    "analysis:signals": frozenset({"internal:health", "internal:interaction", "internal:scene_history"}),
    "analysis:text": frozenset({"analysis:content", "analysis:insight", "analysis:memory"}),
    "device:derived": frozenset({"internal:preferences", "scene:summary", "recommendation:current"}),
}
V5_REPLACED_PARTS = frozenset().union(*V5_MERGED_PARTS.values())
V5_PARTS = (V4_PARTS - V5_REPLACED_PARTS) | frozenset(V5_MERGED_PARTS)
PARTS = (V5_PARTS - {"analysis:signals", "request_meta"}) | {"analysis:context"}
ANALYSIS_PLAN_FIELDS = frozenset({
    "plan_id", "kind", "target_minutes", "sleep_time", "wake_time",
    "updated_at", "activated_at", "status", "deleted",
})
GROUPS = {part: fields for part, fields in V4_GROUPS.items() if part in PARTS}
ANALYSIS_BASE_PARTS = frozenset({
    "sleep:records", "analysis:context", "analysis:text",
})
ANALYSIS_PARTS_BY_TYPE = {
    "analysis_overview": ANALYSIS_BASE_PARTS | {"profile:details"},
    "analysis_sleep_day": ANALYSIS_BASE_PARTS,
    "analysis_sleep_week": ANALYSIS_BASE_PARTS,
    "analysis_sleep_month": ANALYSIS_BASE_PARTS,
    "analysis_explore": ANALYSIS_BASE_PARTS | {"profile:details"},
}
ANALYSIS_PARTS = ANALYSIS_BASE_PARTS | {"profile:details"}
DEVICE_PARTS = frozenset({
    "profile:details", "device:derived", "sleep:records", "analysis:text",
})
assert all(parts <= PARTS for parts in ANALYSIS_PARTS_BY_TYPE.values())
assert DEVICE_PARTS <= PARTS
AVATAR_FIELDS = {"avatar_base64", "avatar_mime_type"}
DETAIL_FIELDS = ("basic_info", "sleep_health", "sleep_mode")
# Schema 4 combines the two analysis content parts. Schema 2 also stored
# profile details in settings:sleep; schema 3 moved them to profile:details.
V3_PARTS = (V4_PARTS - {"analysis:content"}) | {"analysis:advice", "analysis:reports"}
V2_PARTS = V3_PARTS | {"settings:sleep"}


def encoded_uid(uid):
    if not isinstance(uid, str) or not uid or uid.startswith(("_meta:", "p2:")):
        raise ValueError("invalid profile uid")
    return quote(uid, safe="")


def key(uid, part):
    if part not in PARTS | V5_PARTS | V4_PARTS | {"meta"}:
        raise ValueError("unknown profile part")
    return f"p2:{encoded_uid(uid)}:{part}".encode()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def split_profile_v4(data):
    """Old layout codec retained for reading and migrating schema v2-v4."""
    data = copy.deepcopy(data)
    revision = data.pop("revision", 0)
    parts = {}
    for part, fields in V4_GROUPS.items():
        parts[part] = {field: data.pop(field) for field in fields if field in data}
    profile = data.pop("profile", None)
    # Preserve absent vs explicit null, and absent vs empty avatar fields.
    parts["profile:details"] = {field: data.pop(field) for field in DETAIL_FIELDS if field in data}
    parts["profile:avatar"] = {}
    if isinstance(profile, dict):
        parts["profile:details"]["profile"] = {k: v for k, v in profile.items() if k not in AVATAR_FIELDS}
        parts["profile:avatar"] = {"profile": {k: v for k, v in profile.items() if k in AVATAR_FIELDS}}
    elif profile is not None:
        raise ValueError("profile must be an object or null")
    behaviors = data.pop("behaviors", None)
    parts["internal:health"] = {}
    parts["internal:interaction"] = {}
    if isinstance(behaviors, dict):
        parts["internal:health"] = {"behaviors": {k: v for k, v in behaviors.items() if k not in {"plays", "clicks"}}}
        parts["internal:interaction"] = {"behaviors": {k: v for k, v in behaviors.items() if k in {"plays", "clicks"}}}
    elif behaviors is not None:
        raise ValueError("behaviors must be an object or null")
    analysis = data.pop("sleep_analysis", None)
    parts["scene:summary"] = {}
    parts["analysis:memory"] = {}
    if isinstance(analysis, dict):
        parts["scene:summary"] = {"sleep_analysis": {k: v for k, v in analysis.items() if k != "insight_memory"}}
        parts["analysis:memory"] = {"sleep_analysis": {k: v for k, v in analysis.items() if k == "insight_memory"}}
    elif analysis is not None:
        raise ValueError("sleep_analysis must be an object or null")
    parts["internal:extra"] = data
    return parts, revision


def decompose_v4(data):
    parts, revision = split_profile_v4(data)
    for field, part in (("profile", "profile:details"), ("behaviors", "internal:health"),
                        ("sleep_analysis", "scene:summary")):
        if field in data and data[field] is None:
            parts[part][field] = None
    return parts, revision


def split_profile_v5(data):
    """Previous layout codec retained for migrating schema v5."""
    old, revision = decompose_v4(data)
    parts = {part: value for part, value in old.items() if part in V5_PARTS}
    for target, sources in V5_MERGED_PARTS.items():
        merged = assemble({part: old[part] for part in sources}, revision)
        merged.pop("revision")
        parts[target] = merged
    return parts, revision


def split_profile(data):
    """Schema v6: analysis reads a compact active plan alongside its signals."""
    old, revision = split_profile_v5(data)
    parts = {part: value for part, value in old.items() if part in PARTS}
    context = assemble({part: old[part] for part in ("analysis:signals", "request_meta")}, revision)
    context.pop("revision")
    plans = old["plans:state"]
    if "sleep_plan" in plans:
        context["sleep_plan"] = copy.deepcopy(plans["sleep_plan"])
    if "sleep_plans" in plans:
        context["sleep_plans"] = [
            {field: copy.deepcopy(value) for field, value in plan.items()
             if field in ANALYSIS_PLAN_FIELDS}
            for plan in plans["sleep_plans"]
            if isinstance(plan, dict) and plan.get("status") == "active"
            and not plan.get("deleted") and plan.get("target_minutes")
        ]
    parts["analysis:context"] = context
    return parts, revision


def decompose(data):
    return split_profile(data)


def assemble(parts, revision, *, copy_values=True):
    result = {}
    for part in sorted(parts):
        for field, value in parts[part].items():
            item = copy.deepcopy(value) if copy_values else value
            if field in {"profile", "behaviors", "sleep_analysis"} and isinstance(value, dict):
                if result.get(field) is None:
                    result[field] = {}
                result[field].update(item)
            else:
                result[field] = item
    result["revision"] = revision
    return result


class ProfileRepository:
    SCHEMA = 6
    REVISION_PREFIX = b"_meta:revision:"
    USER_PREFIX = b"_meta:user:"

    def __init__(self, service):
        self.service = service

    @contextmanager
    def snapshot(self):
        if self.service.storage_mode == "leveldb":
            with self.service.db.snapshot() as snapshot:
                yield snapshot
        else:
            with self.service.lock:
                yield self.service.text_profiles

    @staticmethod
    def _get(reader, k):
        if isinstance(reader, dict):
            return copy.deepcopy(reader.get(k.decode()))
        raw = reader.get(k)
        return json.loads(raw) if raw is not None else None

    def _batch(self, values, deletions=()):
        if self.service.storage_mode == "leveldb":
            with self.service.db.write_batch(transaction=True) as batch:
                for k, value in values.items():
                    batch.put(k, encode(value))
                for k in deletions:
                    batch.delete(k)
        else:
            with self.service.lock:
                old = self.service.text_profiles.copy()
                self.service.text_profiles.update({k.decode(): copy.deepcopy(v) for k, v in values.items()})
                for k in deletions:
                    self.service.text_profiles.pop(k.decode(), None)
                try:
                    self.service._flush_text_profiles_unlocked()
                except Exception:
                    self.service.text_profiles = old
                    raise

    def _meta(self, reader, uid):
        meta = self._get(reader, key(uid, "meta"))
        if meta is not None and (meta.get("schema_version") not in {2, 3, 4, 5, self.SCHEMA} or not meta.get("migration_complete")):
            raise ValueError("unsupported or incomplete profile layout")
        return meta

    def _read_v2(self, reader, uid, meta):
        loaded = {}
        for part in V2_PARTS:
            raw_key = f"p2:{encoded_uid(uid)}:{part}".encode()
            value = self._get(reader, raw_key)
            if value is None:
                raise ValueError(f"missing committed v2 profile part: {part}")
            loaded[part] = value
        return assemble(loaded, meta["commit_revision"])

    def _read_v3(self, reader, uid, meta):
        """Read schema 3 before translating it to the current layout."""
        loaded = {}
        for part in V3_PARTS:
            raw_key = f"p2:{encoded_uid(uid)}:{part}".encode()
            value = self._get(reader, raw_key)
            if value is None:
                raise ValueError(f"missing committed v3 profile part: {part}")
            loaded[part] = value
        return assemble(loaded, meta["commit_revision"], copy_values=False)

    def _read_v4(self, reader, uid, meta):
        loaded = {}
        for part in V4_PARTS:
            value = self._get(reader, key(uid, part))
            if value is None:
                raise ValueError(f"missing committed v4 profile part: {part}")
            loaded[part] = value
        return assemble(loaded, meta["commit_revision"], copy_values=False)

    def _read_v5(self, reader, uid, meta):
        loaded = {}
        for part in V5_PARTS:
            value = self._get(reader, key(uid, part))
            if value is None:
                raise ValueError(f"missing committed v5 profile part: {part}")
            loaded[part] = value
        return assemble(loaded, meta["commit_revision"], copy_values=False)

    def read(self, uid, parts=None):
        selected = PARTS if parts is None else frozenset(parts)
        if not selected <= PARTS:
            raise ValueError("unknown profile parts")
        with self.snapshot() as reader:
            meta = self._meta(reader, uid)
            if meta is None:
                legacy = self._get(reader, uid.encode())
                if legacy is None:
                    return None
                if not isinstance(legacy, dict):
                    raise ValueError("legacy profile is not an object")
                all_parts, revision = decompose(legacy)
                return assemble({p: all_parts[p] for p in selected}, revision)
            if meta["schema_version"] in {2, 3, 4, 5}:
                old = (self._read_v2(reader, uid, meta) if meta["schema_version"] == 2
                       else self._read_v3(reader, uid, meta) if meta["schema_version"] == 3
                       else self._read_v4(reader, uid, meta) if meta["schema_version"] == 4
                       else self._read_v5(reader, uid, meta))
                all_parts, revision = decompose(old)
                return assemble({p: all_parts[p] for p in selected}, revision)
            loaded = {}
            for part in selected:
                value = self._get(reader, key(uid, part))
                if value is None:
                    raise ValueError(f"missing committed profile part: {part}")
                loaded[part] = value
            # LevelDB values were decoded into new Python objects above.  The
            # assembled result owns them; copying every nested record again is
            # unnecessary on this hot query path.
            return assemble(loaded, meta["commit_revision"], copy_values=False)

    def migrate(self, uid, dry_run=False):
        """Caller owns the UID lock; never changes business values or revision."""
        with self.snapshot() as reader:
            meta = self._meta(reader, uid)
            if meta is not None and meta["schema_version"] == self.SCHEMA:
                return "already_migrated"
            raw = (self._read_v2(reader, uid, meta) if meta is not None and meta["schema_version"] == 2
                   else self._read_v3(reader, uid, meta) if meta is not None and meta["schema_version"] == 3
                   else self._read_v4(reader, uid, meta) if meta is not None and meta["schema_version"] == 4
                   else self._read_v5(reader, uid, meta) if meta is not None and meta["schema_version"] == 5
                   else self._get(reader, uid.encode()))
            if raw is None:
                return "not_found"
            parts, revision = decompose(raw)
            if assemble(parts, revision) != {**raw, "revision": revision}:
                raise ValueError("profile migration round-trip mismatch")
        if not dry_run:
            self._write(uid, parts, revision, meta)
        return "migrated" if not dry_run else "would_migrate"

    def _write(self, uid, changes, revision, old_meta):
        meta = copy.deepcopy(old_meta) if old_meta else {
            "schema_version": self.SCHEMA, "migration_complete": True,
            "part_revisions": {},
        }
        meta["schema_version"] = self.SCHEMA
        meta["part_revisions"] = {
            part: rev for part, rev in meta["part_revisions"].items() if part in PARTS
        }
        for part in changes:
            meta["part_revisions"][part] = revision
        meta["commit_revision"] = revision
        values = {key(uid, part): value for part, value in changes.items()}
        values[key(uid, "meta")] = meta
        values[self.REVISION_PREFIX + uid.encode()] = revision
        values[self.USER_PREFIX + encoded_uid(uid).encode()] = uid
        old_parts = (V2_PARTS if old_meta and old_meta["schema_version"] == 2 else
                     V3_PARTS if old_meta and old_meta["schema_version"] == 3 else
                     V4_PARTS if old_meta and old_meta["schema_version"] == 4 else
                     V5_PARTS if old_meta and old_meta["schema_version"] == 5 else frozenset())
        retired = [f"p2:{encoded_uid(uid)}:{part}".encode()
                   for part in old_parts - PARTS]
        self._batch(values, deletions=retired)

    def save(self, uid, data, parts=None):
        """Patch loaded parts, preserving every other part; increment old API revision."""
        selected = PARTS if parts is None else frozenset(parts)
        if not selected <= PARTS:
            raise ValueError("unknown profile parts")
        incoming, _ = decompose(data)
        with self.snapshot() as reader:
            meta = self._meta(reader, uid)
            if meta is None:
                legacy = self._get(reader, uid.encode())
                previous, old_revision = decompose(legacy or {})
                previous.update({p: incoming[p] for p in selected})
                changes = previous
            elif meta["schema_version"] == 2:
                previous, old_revision = decompose(self._read_v2(reader, uid, meta))
                previous.update({p: incoming[p] for p in selected})
                changes = previous
            elif meta["schema_version"] == 3:
                previous, old_revision = decompose(self._read_v3(reader, uid, meta))
                previous.update({p: incoming[p] for p in selected})
                changes = previous
            elif meta["schema_version"] == 4:
                previous, old_revision = decompose(self._read_v4(reader, uid, meta))
                previous.update({p: incoming[p] for p in selected})
                changes = previous
            elif meta["schema_version"] == 5:
                previous, old_revision = decompose(self._read_v5(reader, uid, meta))
                previous.update({p: incoming[p] for p in selected})
                changes = previous
            else:
                old_revision = meta["commit_revision"]
                changes = {p: incoming[p] for p in selected
                           if self._get(reader, key(uid, p)) != incoming[p]}
        revision = int(old_revision or 0) + 1
        self._write(uid, changes, revision, meta)
        return revision

    def list_uids(self):
        if self.service.storage_mode == "leveldb":
            with self.service.db.snapshot() as snapshot:
                with snapshot.iterator() as rows:
                    keys = [k for k, _ in rows]
                legacy = {k.decode() for k in keys if k and not k.startswith((b"_meta:", b"p2:"))}
                users = {self._get(snapshot, k) for k in keys if k.startswith(self.USER_PREFIX)}
        else:
            with self.service.lock:
                legacy = {k for k in self.service.text_profiles if k and not k.startswith(("_meta:", "p2:"))}
                users = {v for k, v in self.service.text_profiles.items() if k.startswith(self.USER_PREFIX.decode())}
        return sorted(legacy | users)
