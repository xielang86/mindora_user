"""Versioned profile parts, with lossless legacy reads and atomic migration.

No model defaults are used by the storage codec. A partial read may only patch
its explicitly loaded parts; missing parts never mean empty model fields.
The caller holds the existing UID lock for every mutation.
"""
import copy
import json
from contextlib import contextmanager
from urllib.parse import quote


GROUPS = {
    "internal:preferences": ("uid_emb", "basic_info", "long_term_profile"),
    "settings:sleep": ("sleep_health", "sleep_mode"),
    "plans:state": ("sleep_plan", "sleep_plans", "sleep_plans_synced_at"),
    "internal:health_sync": ("health_sync_days", "health_sync_field_versions", "health_sync_timezone"),
    "sleep:records": ("sleep_data",),
    "internal:scene_history": ("mindora_record",),
    "internal:sop_tags": ("sop_tag_profile",),
    "internal:reco_details": ("sop_recommendation_details",),
    "recommendation:current": ("sleep_scenarios_reco", "standard_sop_reco"),
    "analysis:insight": ("sleep_insight",),
    "analysis:advice": ("bedtime_advice",),
    "analysis:reports": ("analysis_reports",),
    "engagement:popup": ("popup_states", "inbox_messages"),
    "engagement:survey": ("survey_submissions",),
    "engagement:footprint": ("footprint_days",),
    "request_meta": ("last_request_timezone", "last_request_language", "last_request_at"),
}
PARTS = frozenset(GROUPS) | {
    "profile:details", "profile:avatar", "internal:health",
    "internal:interaction", "scene:summary", "analysis:memory", "internal:extra",
}
ANALYSIS_PARTS = PARTS - {
    "profile:avatar", "recommendation:current", "internal:reco_details",
    "engagement:popup", "engagement:survey", "engagement:footprint",
}
AVATAR_FIELDS = {"avatar_base64", "avatar_mime_type"}


def encoded_uid(uid):
    if not isinstance(uid, str) or not uid or uid.startswith(("_meta:", "p2:")):
        raise ValueError("invalid profile uid")
    return quote(uid, safe="")


def key(uid, part):
    if part not in PARTS | {"meta"}:
        raise ValueError("unknown profile part")
    return f"p2:{encoded_uid(uid)}:{part}".encode()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def split_profile(data):
    """Losslessly split a JSON object, including sparse/unknown legacy fields."""
    data = copy.deepcopy(data)
    revision = data.pop("revision", 0)
    parts = {}
    for part, fields in GROUPS.items():
        parts[part] = {field: data.pop(field) for field in fields if field in data}
    profile = data.pop("profile", None)
    # Preserve absent vs explicit null, and absent vs empty avatar fields.
    parts["profile:details"] = {}
    parts["profile:avatar"] = {}
    if isinstance(profile, dict):
        parts["profile:details"] = {"profile": {k: v for k, v in profile.items() if k not in AVATAR_FIELDS}}
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


def decompose(data):
    parts, revision = split_profile(data)
    for field, part in (("profile", "profile:details"), ("behaviors", "internal:health"),
                        ("sleep_analysis", "scene:summary")):
        if field in data and data[field] is None:
            parts[part] = {field: None}
    return parts, revision


def assemble(parts, revision):
    result = {}
    for part in sorted(parts):
        for field, value in parts[part].items():
            if field in {"profile", "behaviors", "sleep_analysis"} and isinstance(value, dict):
                if result.get(field) is None:
                    result[field] = {}
                result[field].update(copy.deepcopy(value))
            else:
                result[field] = copy.deepcopy(value)
    result["revision"] = revision
    return result


class ProfileRepository:
    SCHEMA = 2
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

    def _batch(self, values):
        if self.service.storage_mode == "leveldb":
            with self.service.db.write_batch(transaction=True) as batch:
                for k, value in values.items():
                    batch.put(k, encode(value))
        else:
            with self.service.lock:
                old = self.service.text_profiles.copy()
                self.service.text_profiles.update({k.decode(): copy.deepcopy(v) for k, v in values.items()})
                try:
                    self.service._flush_text_profiles_unlocked()
                except Exception:
                    self.service.text_profiles = old
                    raise

    def _meta(self, reader, uid):
        meta = self._get(reader, key(uid, "meta"))
        if meta is not None and (meta.get("schema_version") != self.SCHEMA or not meta.get("migration_complete")):
            raise ValueError("unsupported or incomplete profile layout")
        return meta

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
            loaded = {}
            for part in selected:
                value = self._get(reader, key(uid, part))
                if value is None:
                    raise ValueError(f"missing committed profile part: {part}")
                loaded[part] = value
            return assemble(loaded, meta["commit_revision"])

    def migrate(self, uid, dry_run=False):
        """Caller owns the UID lock; never changes business values or revision."""
        with self.snapshot() as reader:
            if self._meta(reader, uid) is not None:
                return "already_migrated"
            raw = self._get(reader, uid.encode())
            if raw is None:
                return "not_found"
            parts, revision = decompose(raw)
            if assemble(parts, revision) != {**raw, "revision": revision}:
                raise ValueError("profile migration round-trip mismatch")
        if not dry_run:
            self._write(uid, parts, revision, None)
        return "migrated" if not dry_run else "would_migrate"

    def _write(self, uid, changes, revision, old_meta):
        meta = copy.deepcopy(old_meta) if old_meta else {
            "schema_version": self.SCHEMA, "migration_complete": True,
            "part_revisions": {},
        }
        for part in changes:
            meta["part_revisions"][part] = revision
        meta["commit_revision"] = revision
        values = {key(uid, part): value for part, value in changes.items()}
        values[key(uid, "meta")] = meta
        values[self.REVISION_PREFIX + uid.encode()] = revision
        values[self.USER_PREFIX + encoded_uid(uid).encode()] = uid
        self._batch(values)

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
                legacy = {k.decode() for k in keys if not k.startswith((b"_meta:", b"p2:"))}
                users = {self._get(snapshot, k) for k in keys if k.startswith(self.USER_PREFIX)}
        else:
            with self.service.lock:
                legacy = {k for k in self.service.text_profiles if not k.startswith(("_meta:", "p2:"))}
                users = {v for k, v in self.service.text_profiles.items() if k.startswith(self.USER_PREFIX.decode())}
        return sorted(legacy | users)
