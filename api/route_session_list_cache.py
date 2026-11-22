"""Session-list cache helpers extracted from api.routes."""

import copy
import hashlib
from dataclasses import dataclass
import json
import os
import re
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from api.config import LOCK, SESSION_DIR, SESSIONS, SETTINGS_FILE
from api.models import _active_state_db_path, _active_stream_ids
from api.profiles import _profiles_match


# Cron session ids are ``cron_{job_id}_{run_timestamp}`` where the run
# timestamp is ``YYYYMMDD_HHMMSS`` (e.g. cron_job6728_20260803_100000). Used to
# validate that the text after a matched ``cron_{jid}_`` prefix is EXACTLY a run
# timestamp, so a shorter job id (backup) cannot claim a longer job id's session
# (backup_full) when the longer job is not itself running (#6728 gate fix).
_CRON_RUN_TS_RE = re.compile(r"\d{8}_\d{6}")


_SESSIONS_CACHE_TTL_SECONDS = 2.5
# #4808: while a turn is actively streaming the frontend polls /api/sessions on a
# fixed cadence (static/sessions.js `_streamingPollMs`). With the idle TTL of
# 2.5s, the entry expires between streaming polls, so each poll can find it stale
# and force a full all_sessions() rebuild on the hot path under the global store
# LOCK — pinning CPU and starving token rendering on large stores (recurrence of
# #4672). Hold the sidebar cache steady for longer than one poll interval while
# streaming; live runtime state (active stream, sort order, pending flags) is
# overlaid on every response regardless of cache, and structural/settings changes
# still invalidate immediately. Keep this strictly greater than
# `_streamingPollMs`/1000 (see tests/test_streaming_cache_ttl_vs_poll.py).
_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 45.0
_SESSIONS_CACHE_MAX_ENTRIES = 64
_SESSIONS_CACHE_WAIT_SECONDS = 0.25
_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10
_SESSIONS_CACHE: OrderedDict[tuple, tuple[float, tuple, dict]] = OrderedDict()
_SESSIONS_CACHE_LOCK = threading.RLock()
_SESSIONS_CACHE_INFLIGHT: dict[tuple, threading.Event] = {}
_SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION = 0
_SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION = 0
_SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION: dict[str, int] = {}
# Admission/lifecycle records only. Rows remain exclusively in _SESSIONS_CACHE.
_WARM_SCOPES: OrderedDict[tuple, dict] = OrderedDict()
_WARM_SCOPE_LIMIT = 8
_WARM_COUNTS = dict(publications=0, refresh_failures=0, safety_refusals=0,
                    served_during_refresh=0, cold_waits=0)


def admit_warm_scope(key, options, home, *, startup=False, stop_event=None):
    with _SESSIONS_CACHE_LOCK:
        if stop_event is not None and stop_event.is_set():
            return
        entry = _WARM_SCOPES.get(key)
        if entry is None:
            _WARM_SCOPES[key] = dict(options=dict(options), home=home,
                                     startup=startup, fingerprint=None, safety=0, content=0,
                                     published_content=-1, proofs={})
        elif startup:
            entry["startup"] = True
        _WARM_SCOPES.move_to_end(key)
        while len(_WARM_SCOPES) > _WARM_SCOPE_LIMIT:
            victim = next((k for k, v in _WARM_SCOPES.items() if not v["startup"]), None)
            if victim is None:
                break
            retire_warm_scope(victim)


def warm_scope_is_current(key):
    with _SESSIONS_CACHE_LOCK:
        return key in _WARM_SCOPES


def warm_scope_is_startup(key):
    with _SESSIONS_CACHE_LOCK:
        return bool(_WARM_SCOPES.get(key, {}).get("startup"))


def warm_scope_snapshot():
    with _SESSIONS_CACHE_LOCK:
        return [(k, dict(v["options"]), v["home"]) for k, v in _WARM_SCOPES.items()]


def retire_warm_scope(key):
    with _SESSIONS_CACHE_LOCK:
        _WARM_SCOPES.pop(key, None)
        _SESSIONS_CACHE.pop(key, None)
        event = _SESSIONS_CACHE_INFLIGHT.get(key)
        if event is not None:
            event.set()


def lookup_warm_session_row(session_id, *, all_profiles=False):
    from api.profiles import get_active_profile_name, get_active_hermes_home
    profile = get_active_profile_name() or 'default'
    home = str(Path(get_active_hermes_home()).resolve())
    for key, _options, scope_home in warm_scope_snapshot():
        if (key[1] != bool(all_profiles) or scope_home != home
                or not _profiles_match(key[0], profile)):
            continue
        entry = _eligible_warm_entry(key)
        if entry:
            rows = [row for row in entry[2].get('sessions', [])
                    if row.get('session_id') == session_id
                    and (all_profiles or _profiles_match(row.get('profile'), profile))]
            if len(rows) == 1:
                return _session_list_cache_copy_row(rows[0])
    return {}


def warm_gateway_snapshot(profile, home):
    """Reuse admitted interactive metadata for SSE, without another DB scan.

    None means no warm scope exists (legacy watcher use). An admitted scope
    still loading returns an empty tuple so the watcher waits for its owner.
    """
    for key, _, scope_home in warm_scope_snapshot():
        if (not key[1] and _profiles_match(key[0], profile)
                and (home is None or scope_home == str(home))):
            entry = _eligible_warm_entry(key)
            if entry is None:
                return ()
            return [_session_list_cache_copy_row(row) for row in entry[2].get('sessions', [])
                    if _profiles_match(row.get('profile'), profile)]
    return None


def stop_warm_scopes():
    """Fence late publication and release cold waiters during server teardown."""
    with _SESSIONS_CACHE_LOCK:
        for key in list(_WARM_SCOPES):
            retire_warm_scope(key)
            event = _SESSIONS_CACHE_INFLIGHT.pop(key, None)
            if event is not None:
                event.set()

    # CLI refresh workers can outlive the bounded watcher join. Fence their
    # publication too, and wake their followers without waiting on store I/O.
    from api import models
    models.clear_cli_sessions_cache()
    with models._CLI_SESSIONS_CACHE_LOCK:
        events = list(models._CLI_SESSIONS_CACHE_INFLIGHT.values())
        models._CLI_SESSIONS_CACHE_INFLIGHT.clear()
    for event in events:
        event.set()


def _warm_path_identity(path):
    try:
        path = Path(path)
        st = path.stat()
        return (str(path.resolve()), st.st_dev, st.st_ino, st.st_mtime_ns,
                st.st_ctime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _warm_store_identity(path):
    stamp = _warm_path_identity(path)
    return stamp[:3] if stamp else None


@dataclass(frozen=True)
class SidecarVersion:
    stamp: tuple
    profile: str | None
    eligibility: bytes | tuple
    projection: bytes | None


@dataclass(frozen=True)
class WarmFingerprint:
    identities: tuple
    controls: tuple
    authority: tuple
    sidecars: dict
    index: dict
    gateway: dict
    content: tuple


def _row_safety_digest(row):
    fields = ("session_id", "profile", "archived", "default_hidden", "source",
              "raw_source", "source_tag", "session_source", "source_label",
              "project_id", "owner", "user_id", "chat_id", "read_only", "platform",
              "thread_id", "chat_type", "is_cli_session", "parent_session_id", "session_key",
              "workspace", "access", "is_compression_snapshot")
    value = json.dumps([row.get(k) for k in fields], sort_keys=True)
    return hashlib.sha256(value.encode()).digest()


@lru_cache(maxsize=512)
def _sidecar_safety(path, stamp):
    """Cache compact visibility facts by exact file version, never transcripts."""
    from api.models import _read_metadata_json_prefix, sidebar_file_projection_digest
    prefix = _read_metadata_json_prefix(path)
    data = json.loads(prefix) if prefix else {}
    if data.get('_mc_v') != 1:
        # Older writers may put ownership fields after the transcript.
        if stamp[-1] > 1024 * 1024:
            return SidecarVersion(stamp, None, stamp, None)
        data = json.loads(path.read_bytes())
    return SidecarVersion(stamp, _session_list_cache_profile_scope(data.get('profile')),
                          _row_safety_digest(data), sidebar_file_projection_digest(data))


@lru_cache(maxsize=2)
def _index_safety(path, stamp):
    rows = json.loads(path.read_bytes())
    return {(_session_list_cache_profile_scope(r.get("profile")), r["session_id"]): _row_safety_digest(r)
            for r in rows}


@lru_cache(maxsize=2)
def _gateway_safety(path, stamp):
    from api.routes import _gateway_session_identities
    raw = json.loads(path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError("Gateway session identities unavailable")
    return {sid: _row_safety_digest(row) for sid, row in _gateway_session_identities(raw).items()}


def _warm_contexts(key):
    from api.models import _all_profiles_cli_contexts
    from api.profiles import get_active_hermes_home
    home = Path(get_active_hermes_home())
    if key[1]:
        contexts, _ = _all_profiles_cli_contexts()
        if not contexts:
            raise ValueError("Session profile inventory unavailable")
        return contexts
    return [(home, home / 'state.db', key[0])]


def warm_scope_fingerprint(key, previous=None):
    """Separate changes to eligibility from the content revision read by a build."""
    from api.models import _default_claude_code_projects_dir, _iter_claude_code_jsonl_files
    session_dir = _session_list_cache_session_dir()
    identities = []
    safety = [_warm_store_identity(session_dir)]
    content = []
    controls = [_session_list_cache_settings_file(), session_dir.parent / 'projects.json']
    for home, db, profile in _warm_contexts(key):
        home_identity = _warm_store_identity(home)
        if home_identity is None:
            raise FileNotFoundError("Session profile home unavailable")
        identities.extend(((home, home_identity), (db, _warm_store_identity(db))))
        safety.append((profile, home_identity, identities[-1][1]))
        controls.extend((home / 'config.yaml', home / '.env'))
        content.extend((_warm_path_identity(db), _warm_path_identity(Path(str(db) + '-wal'))))
    guard_files = tuple((path, _warm_path_identity(path)) for path in controls)
    safety.extend(guard_files)
    gateway_path = _session_list_cache_gateway_session_metadata_path()
    gateway_stamp = _warm_path_identity(gateway_path)
    gateway = _gateway_safety(gateway_path, gateway_stamp) if gateway_stamp else {}
    content.append(gateway_stamp)
    controls = []
    if key[2] and key[14]:
        root = _default_claude_code_projects_dir()
        if root:
            controls.extend(_iter_claude_code_jsonl_files(root) or ())
    safety.extend(_warm_path_identity(path) for path in controls)
    sidecars = {}
    index = {}
    try:
        paths = sorted(p for p in session_dir.iterdir() if p.suffix == '.json')
    except FileNotFoundError:
        paths = []
    for path in paths:
        stamp = _warm_path_identity(path)
        if stamp is None:
            continue
        if path.name == '_index.json':
            index = _index_safety(path, stamp)
        elif path == gateway_path:
            continue
        elif path.name.startswith('_'):
            safety.append(stamp)
        else:
            prior = previous.sidecars.get(path.stem) if previous else None
            if prior is not None and prior.stamp == stamp:
                sidecars[path.stem] = prior
            else:
                sidecars[path.stem] = _sidecar_safety(path, stamp)
        content.append(stamp)
    return WarmFingerprint(tuple(identities), guard_files, tuple(safety), sidecars, index, gateway, tuple(content))


def warm_database_proofs(key, rows):
    from api.agent_sessions import inventory_row_safety
    proofs = {}
    for _, db, owner in _warm_contexts(key):
        ids = {r['session_id'] for r in rows if r.get('session_id')
               and _profiles_match(r.get('profile'), owner)}
        proofs[owner] = inventory_row_safety(db, ids)
    return proofs


def _owned_sidecar_version(files, sid, owner):
    entry = files.get(sid)
    if entry and (entry.profile is None or entry.profile == owner):
        return entry.eligibility
    return None


def _unsafe_rows(rows, before, after, original_db, current_db):
    """An unrelated insertion cannot invalidate an old row; check candidate IDs too."""
    missing = object()
    changed = set()
    for row in rows:
        sid = row.get('session_id')
        profile = row.get('profile') or 'default'
        identity = profile, sid
        owner = _session_list_cache_profile_scope(profile)
        if (_owned_sidecar_version(before.sidecars, sid, owner) != _owned_sidecar_version(after.sidecars, sid, owner)
                or before.index.get((owner, sid)) != after.index.get((owner, sid))
                or before.gateway.get(sid) != after.gateway.get(sid)):
            changed.add(identity)
            continue
        for owner, db_rows in original_db.items():
            if (_profiles_match(profile, owner)
                    and db_rows.get(sid, missing) != current_db.get(owner, {}).get(sid, missing)):
                changed.add(identity)
                break
    return changed


def _without_rows(payload, removed):
    def keep(row):
        owner = row.get('profile') or 'default'
        return all((owner, row.get(field)) not in removed for field in (
            'session_id', 'parent_session_id', '_lineage_root_id', '_lineage_tip_id',
            '_parent_lineage_root_id', '_parent_lineage_tip_id'))
    updated = dict(payload)
    for group in ('sessions', 'sidebar_reference_sessions'):
        if group in payload:
            updated[group] = [row for row in payload[group] if keep(row)]
    return updated


def _eligible_warm_entry(key):
    """All warm consumers verify store ownership before reading published rows."""
    with _SESSIONS_CACHE_LOCK:
        scope = _WARM_SCOPES.get(key)
        entry = _SESSIONS_CACHE.get(key)
        if scope is None or entry is None:
            return None
        fingerprint = scope['fingerprint']
    try:
        valid = fingerprint is not None and all(
            _warm_store_identity(path) == expected for path, expected in fingerprint.identities)
        if valid:
            valid = all(_warm_path_identity(path) == stamp for path, stamp in fingerprint.controls)
    except OSError:
        valid = False
    with _SESSIONS_CACHE_LOCK:
        if scope is not _WARM_SCOPES.get(key):
            return None
        # A concurrent publication checked its own identities. Apply a negative
        # observation only to the instance that was actually checked.
        if not valid and scope['fingerprint'] is fingerprint:
            _refuse_scope(key, scope)
            return None
        entry = _SESSIONS_CACHE.get(key)
        if entry:
            _SESSIONS_CACHE.move_to_end(key)
        return entry


def _refuse_scope(key, scope):
    scope['safety'] += 1
    scope['fingerprint'] = None
    scope['proofs'] = {}
    _WARM_COUNTS['safety_refusals'] += 1
    _SESSIONS_CACHE.pop(key, None)


def refuse_warm_scope(key, scope):
    with _SESSIONS_CACHE_LOCK:
        if _WARM_SCOPES.get(key) is scope:
            _refuse_scope(key, scope)


def warm_scope_needs_refresh(key):
    with _SESSIONS_CACHE_LOCK:
        scope = _WARM_SCOPES.get(key)
        if scope is None:
            return False
        cached = _SESSIONS_CACHE.get(key)
        previous = scope['fingerprint']
        proofs = scope['proofs']
        dirty = scope['content'] != scope['published_content']
    try:
        current = warm_scope_fingerprint(key, previous)
        if previous == current and cached is not None:
            return dirty
        unsafe = previous is not None and previous.authority != current.authority
        removed = set()
        if not unsafe and previous and cached:
            verified = warm_database_proofs(key, cached[2].get('sessions', ()))
            removed = _unsafe_rows(cached[2].get('sessions', ()), previous, current, proofs, verified)
    except Exception:
        with _SESSIONS_CACHE_LOCK:
            if _WARM_SCOPES.get(key) is scope:
                _refuse_scope(key, scope)
        raise
    with _SESSIONS_CACHE_LOCK:
        if _WARM_SCOPES.get(key) is not scope:
            return False
        if unsafe or (removed and _SESSIONS_CACHE.get(key) is not cached):
            _refuse_scope(key, scope)
        elif removed and _SESSIONS_CACHE.get(key) is cached:
            scope['safety'] += 1
            _SESSIONS_CACHE[key] = cached[0], cached[1], _without_rows(cached[2], removed)
        return True


_SIDEBAR_SESSION_RESPONSE_FIELDS = {
    "session_id",
    "attention",
    "title",
    "display_title",
    "_state_db_title",
    "workspace",
    "model",
    "model_provider",
    "message_count",
    "visible_message_count",
    "transcript_generation",
    "transcript_generation_baseline",
    "user_message_count",
    "created_at",
    "updated_at",
    "last_message_at",
    "pinned",
    "archived",
    "project_id",
    "profile",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_hit_percent",
    "personality",
    "context_length",
    "config_context_length",
    "window_usage_percent",
    "source_tag",
    "raw_source",
    "session_source",
    "source_label",
    "is_cli_session",
    "is_messaging_session",
    "is_streaming",
    "cron_running",
    "active_stream_id",
    "has_pending_user_message",
    "pending_started_at",
    "default_hidden",
    "worktree_path",
    "worktree_branch",
    "parent_session_id",
    "parent_title",
    "parent_source",
    "parent_is_cli_session",
    "relationship_type",
    "pre_compression_snapshot",
    "_lineage_root_id",
    "_lineage_tip_id",
    "_compression_segment_count",
    "_lineage_collapsed_count",
    "_parent_lineage_root_id",
    "_parent_lineage_tip_id",
    "_cross_surface_child_session",
    "match_type",
    "match_preview",
    # Preserved so the sidebar can suppress rename / action-menu / swipe on
    # read-only sessions and render the detailed gateway model label. Only the
    # latest bounded routing object is included; routing history stays excluded.
    "read_only",
    "is_read_only",
    "gateway_routing",
    "last_used_model",
}


def _session_list_cache_sidebar_fields() -> set[str]:
    """Return the canonical bounded field set used by the list response."""
    return _SIDEBAR_SESSION_RESPONSE_FIELDS


def _session_list_cache_copy_value(value):
    """Copy only mutable values after the transcript-bearing projection."""
    if isinstance(value, (dict, list, set)):
        return copy.deepcopy(value)
    return value


def _session_list_cache_copy_row(row: dict) -> dict:
    return {
        key: _session_list_cache_copy_value(value)
        for key, value in row.items()
    }


def _session_list_cache_bounded_payload(payload: dict) -> dict:
    """Project a builder payload to data the sidebar can actually consume.

    Builders operate on full session snapshots because they also perform
    reconciliation and lineage decisions. The cache must not retain those
    snapshots: a single long transcript can otherwise make every cache set/get
    allocate hundreds of megabytes via deepcopy().
    """
    fields = _session_list_cache_sidebar_fields()

    def project_rows(rows):
        projected = []
        for row in rows or []:
            if not isinstance(row, dict):
                projected.append({})
                continue
            item = {key: row[key] for key in fields if key in row}
            projected.append(_session_list_cache_copy_row(item))
        return projected

    bounded = {
        key: _session_list_cache_copy_value(value)
        for key, value in payload.items()
        if key not in {"sessions", "sidebar_reference_sessions"}
    }
    if "sessions" in payload:
        bounded["sessions"] = project_rows(payload.get("sessions"))
    if "sidebar_reference_sessions" in payload:
        bounded["sidebar_reference_sessions"] = project_rows(
            payload.get("sidebar_reference_sessions")
        )
    return bounded


def _session_list_cache_copy_payload(payload: dict) -> dict:
    """Copy only the already-bounded cache shape for request-local mutation."""
    copied = {
        key: _session_list_cache_copy_value(value)
        for key, value in payload.items()
        if key not in {"sessions", "sidebar_reference_sessions"}
    }
    if "sessions" in payload:
        copied["sessions"] = [
            _session_list_cache_copy_row(row)
            for row in payload.get("sessions", [])
        ]
    if "sidebar_reference_sessions" in payload:
        copied["sidebar_reference_sessions"] = [
            _session_list_cache_copy_row(row)
            for row in payload.get("sidebar_reference_sessions", [])
        ]
    return copied


def get_session_list_cache_snapshot() -> dict[str, object]:
    """Return scalar cache occupancy without waiting or changing LRU state.

    Held-section discipline: between the nonblocking acquire and the release,
    only occupancy, bounded scalar counters and constants may be read. Nothing that can
    resolve config, resolve a profile, touch the filesystem, import a module, or
    wait on another lock may be added here. ``_SESSIONS_CACHE_LOCK`` is an
    ``RLock``, so a nonblocking acquire from a thread already holding it would
    report available mid-mutation; the health collector is this helper's only
    caller and never runs nested inside a cache rebuild.
    """
    result = {
        "available": False,
        "entries": 0,
        "inflight_rebuilds": 0,
        "cap": 0,
    }
    acquired = False
    try:
        acquired = _SESSIONS_CACHE_LOCK.acquire(blocking=False)
        if not acquired:
            return result
        return {
            "available": True,
            "entries": max(0, int(len(_SESSIONS_CACHE))),
            "inflight_rebuilds": max(0, int(len(_SESSIONS_CACHE_INFLIGHT))),
            "cap": max(0, int(_SESSIONS_CACHE_MAX_ENTRIES)),
            **_WARM_COUNTS,
        }
    except Exception:
        return result
    finally:
        if acquired:
            _SESSIONS_CACHE_LOCK.release()


def _session_list_cache_session_dir() -> Path:
    try:
        import api.routes as _routes

        value = getattr(_routes, "SESSION_DIR", SESSION_DIR)
        return Path(value)
    except Exception:
        return SESSION_DIR


def _session_list_cache_settings_file() -> Path:
    try:
        import api.routes as _routes

        value = getattr(_routes, "SETTINGS_FILE", SETTINGS_FILE)
        return Path(value)
    except Exception:
        return SETTINGS_FILE


def _session_list_cache_state_db_path():
    try:
        import api.routes as _routes

        override = getattr(_routes, "_active_state_db_path", None)
        if callable(override) and override is not _session_list_cache_state_db_path:
            return override()
    except Exception:
        pass
    return _active_state_db_path()


def _session_list_cache_gateway_session_metadata_path() -> Path:
    try:
        import api.routes as _routes

        override = getattr(_routes, "_gateway_session_metadata_path", None)
        if callable(override) and override is not _session_list_cache_gateway_session_metadata_path:
            return Path(override())
    except Exception:
        pass

    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()
    return hermes_home / "sessions" / "sessions.json"


def _session_list_cache_active_stream_ids():
    try:
        import api.routes as _routes

        override = getattr(_routes, "_active_stream_ids", None)
        if callable(override) and override is not _session_list_cache_active_stream_ids:
            return override()
    except Exception:
        pass
    return _active_stream_ids()


def _session_list_cache_running_cron_jobs() -> dict[str, float]:
    """Return {job_id: start_epoch} for cron jobs currently tracked as running.

    Cron liveness lives only in the in-memory ``_RUNNING_CRON_JOBS`` dict in
    api.routes (#6728): the sidebar polls /api/sessions (not /api/crons/status),
    so without this overlay a still-running cron job's session row looks
    completed the moment it appends a message. Fail closed to an empty dict.
    """
    try:
        import api.routes as _routes

        jobs = getattr(_routes, "_RUNNING_CRON_JOBS", None)
        lock = getattr(_routes, "_RUNNING_CRON_LOCK", None)
        if jobs is None or lock is None:
            return {}
        with lock:
            return dict(jobs)
    except Exception:
        return {}


def _session_list_cache_resolved_source_stamp(key: tuple):
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_source_stamp", None)
        if callable(override) and override is not _session_list_cache_source_stamp:
            return override(key)
    except Exception:
        pass
    return _session_list_cache_source_stamp(key)


def _session_list_cache_profile_scope(profile: str | None) -> str:
    normalized = str(profile or "").strip() or "default"
    if _profiles_match(normalized, "default"):
        return "default"
    return normalized


def _session_list_cache_key(
    active_profile: str | None,
    all_profiles: bool,
    show_cli_sessions: bool,
    show_previous_messaging_sessions: bool,
    show_cron_sessions: bool,
    include_archived: bool = False,
    exclude_hidden: bool = False,
    visible_only: bool = False,
    show_webhook_sessions: bool = False,
    show_kanban_sessions: bool = False,
    source_filter: str | None = None,
    sidebar_source: str | None = None,
    archived_limit: int | None = None,
    archived_offset: int = 0,
) -> tuple:
    normalized_archived_limit = None
    if archived_limit is not None:
        try:
            normalized_archived_limit = max(0, int(archived_limit))
        except (TypeError, ValueError):
            normalized_archived_limit = None
    try:
        normalized_archived_offset = max(0, int(archived_offset or 0))
    except (TypeError, ValueError):
        normalized_archived_offset = 0
    return (
        _session_list_cache_profile_scope(active_profile),
        bool(all_profiles),
        bool(show_cli_sessions),
        bool(show_previous_messaging_sessions),
        bool(show_cron_sessions),
        bool(include_archived),
        bool(exclude_hidden),
        bool(visible_only),
        bool(show_webhook_sessions),
        bool(show_kanban_sessions),
        source_filter,
        sidebar_source,
        normalized_archived_limit,
        normalized_archived_offset,
    )


def _session_list_cache_get(
    key: tuple,
    allow_stale: bool = False,
) -> tuple[dict | None, bool]:
    now = time.monotonic()
    if "warm_metadata" in key:
        entry = _eligible_warm_entry(key)
        if entry is None:
            return None, False
        with _SESSIONS_CACHE_LOCK:
            if key in _SESSIONS_CACHE_INFLIGHT:
                _WARM_COUNTS["served_during_refresh"] += 1
        return _session_list_cache_copy_payload(entry[2]), True
    current_stamp = _session_list_cache_resolved_source_stamp(key)
    with _SESSIONS_CACHE_LOCK:
        entry = _SESSIONS_CACHE.get(key)
        if not entry:
            return None, False
        ts, stamp, payload = entry
        if stamp != current_stamp:
            if allow_stale:
                _SESSIONS_CACHE.move_to_end(key)
                return _session_list_cache_copy_payload(payload), False
            _SESSIONS_CACHE.pop(key, None)
            return None, False
        # #4808: widen the freshness window while a turn is streaming so the fixed
        # streaming poll cadence doesn't force a full rebuild on every poll.
        ttl = _SESSIONS_CACHE_TTL_SECONDS
        if _session_list_cache_streaming_freeze_marker() is not None:
            ttl = _SESSIONS_CACHE_STREAMING_TTL_SECONDS
        fresh = key in _WARM_SCOPES or (now - ts) < ttl
        if fresh:
            _SESSIONS_CACHE.move_to_end(key)
            return _session_list_cache_copy_payload(payload), True
        if allow_stale:
            _SESSIONS_CACHE.move_to_end(key)
            return _session_list_cache_copy_payload(payload), False
        _SESSIONS_CACHE.pop(key, None)
        return None, False


def _session_list_cache_stale_reason(key: tuple) -> str | None:
    """Return why an existing cache entry is stale, if it is stale."""
    now = time.monotonic()
    current_stamp = _session_list_cache_resolved_source_stamp(key)
    with _SESSIONS_CACHE_LOCK:
        entry = _SESSIONS_CACHE.get(key)
        if not entry:
            return None
        ts, stamp, _payload = entry
        if stamp != current_stamp:
            return "source"
        ttl = _SESSIONS_CACHE_TTL_SECONDS
        if _session_list_cache_streaming_freeze_marker() is not None:
            ttl = _SESSIONS_CACHE_STREAMING_TTL_SECONDS
        if (now - ts) >= ttl:
            return "age"
        return None


def _session_list_cache_set(
    key: tuple,
    payload: dict,
    *,
    expected_invalidation_stamp: tuple[int, int] | None = None,
    warm_publication=None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    stamp = warm_publication[3] if warm_publication else _session_list_cache_resolved_source_stamp(key)
    bounded = _session_list_cache_bounded_payload(payload)
    with _SESSIONS_CACHE_LOCK:
        # Projection intentionally happens outside the lock so large source rows
        # cannot block cache hits. Re-check the caller's pre-build generation
        # atomically before insertion, otherwise a rename/archive/delete clear
        # that lands during projection can be undone by this stale write.
        if (
            expected_invalidation_stamp is not None
            and _session_list_cache_invalidation_stamp(key)
            != expected_invalidation_stamp
        ):
            return False
        if "warm_metadata" in key and key not in _WARM_SCOPES:
            return False
        if warm_publication:
            scope, safety, content, fingerprint, proofs = warm_publication
            if _WARM_SCOPES.get(key) is not scope or scope['safety'] != safety:
                return False
            scope.update(fingerprint=fingerprint, proofs=proofs, published_content=content)
            _WARM_COUNTS['publications'] += 1
        _SESSIONS_CACHE[key] = (time.monotonic(), stamp, bounded)
        _SESSIONS_CACHE.move_to_end(key)
        while len(_SESSIONS_CACHE) > _SESSIONS_CACHE_MAX_ENTRIES:
            victim = next((k for k in _SESSIONS_CACHE
                           if not _WARM_SCOPES.get(k, {}).get("startup")), None)
            if victim is None:
                break
            _SESSIONS_CACHE.pop(victim, None)
    return True


def apply_warm_change(profile, session_id, patch, *, remove=False):
    """Fence old candidates for a committed row edit; keep unrelated warm rows."""
    with _SESSIONS_CACHE_LOCK:
        _session_list_cache_clear(profile, keep_warm=True)
        for key, scope in _WARM_SCOPES.items():
            if profile and not key[1] and not _profiles_match(key[0], profile):
                continue
            scope['content'] += 1
            if patch is None and not remove:
                continue
            scope['safety'] += 1
            entry = _SESSIONS_CACHE.get(key)
            if entry is None:
                continue
            ts, stamp, payload = entry
            updated = dict(payload)
            for group in ('sessions', 'sidebar_reference_sessions'):
                if group not in payload:
                    continue
                rows = []
                for row in payload[group]:
                    if row.get('session_id') == session_id and _profiles_match(row.get('profile'), profile):
                        if remove:
                            continue
                        row = {**row, **patch}
                    rows.append(row)
                updated[group] = rows
            _SESSIONS_CACHE[key] = ts, stamp, updated


def _session_list_cache_clear(profile: str | None = None, *, keep_warm=False) -> None:
    normalized_profile = _session_list_cache_profile_scope(profile) if profile else None
    with _SESSIONS_CACHE_LOCK:
        global _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
        global _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION
        if not keep_warm:
            for key, scope in _WARM_SCOPES.items():
                if not profile or key[1] or _profiles_match(key[0], profile):
                    _refuse_scope(key, scope)
        if not profile:
            _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION += 1
            _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION += 1
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.clear()
            for key in list(_SESSIONS_CACHE):
                if not keep_warm or key not in _WARM_SCOPES:
                    _SESSIONS_CACHE.pop(key)
            return
        _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION += 1
        _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION[normalized_profile] = (
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.get(normalized_profile, 0) + 1
        )
        for cache_key in list(_SESSIONS_CACHE.keys()):
            if keep_warm and cache_key in _WARM_SCOPES:
                continue
            cache_profile, cache_all_profiles, *_rest = cache_key
            if cache_all_profiles:
                _SESSIONS_CACHE.pop(cache_key, None)
                continue
            if _profiles_match(cache_profile, normalized_profile):
                _SESSIONS_CACHE.pop(cache_key, None)


def _clear_session_list_cache(profile: str | None = None) -> None:
    _session_list_cache_clear(profile=profile)


def _session_list_cache_invalidation_stamp(key: tuple) -> tuple[int, int]:
    cache_profile, cache_all_profiles, *_rest = key
    with _SESSIONS_CACHE_LOCK:
        global_version = _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
        if cache_all_profiles:
            return (
                global_version,
                _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION,
            )
        return (
            global_version,
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.get(cache_profile, 0),
        )


def _session_list_cache_path_stamp(path: Path | None) -> tuple[int, int]:
    try:
        if path is None:
            return (0, 0)
        st = Path(path).stat()
        return (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))), int(st.st_size))
    except Exception:
        return (0, 0)


def _session_list_cache_streaming_freeze_marker():
    """Return a hold-down marker while any session is actively streaming, else None.

    During an active chat turn the gateway/CLI writes message rows to state.db
    continuously. Each write advances the WAL stat and the content fingerprint
    (``MAX(rowid)`` of ``messages``) that ``_session_list_cache_source_stamp``
    folds in, so the source stamp changes on essentially every ``/api/sessions``
    poll — popping the cache and forcing a full ``all_sessions()`` rebuild
    mid-stream. That rebuild then contends for the global ``LOCK`` the streaming
    worker holds while writing, which is what drags token output down to
    ~2 tok/s and produces the multi-second (and occasional ~15s) ``/api/sessions``
    latencies in issue #4672.

    The marker is keyed only on the *set* of active stream ids, not on any
    per-write state, so:
      * while the same turn(s) stream, the marker is constant → the cache holds
        steady and rebuilds are bounded to the TTL cadence (one per
        ``_SESSIONS_CACHE_TTL_SECONDS``) instead of one per poll;
      * the instant a stream starts or stops, the active set changes → the
        marker changes → the cache re-validates and the just-finished turn's
        final title/message_count is picked up immediately.

    Structural sidebar mutations (new/deleted/renamed/imported sessions,
    attention, cron completion) do NOT rely on this stamp — they invalidate the
    cache directly through the ``publish_session_list_changed`` listener — so the
    only thing that can lag under the hold-down is a streaming session's own
    title/message_count, which already tolerates a <=TTL refresh delay.
    """
    try:
        active = _session_list_cache_active_stream_ids()
    except Exception:
        return None
    if not active:
        return None
    try:
        return ("streaming", tuple(sorted(str(x) for x in active)))
    except Exception:
        return ("streaming",)


def _session_list_cache_state_db_fingerprint(state_db_path: Path | None):
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_state_db_fingerprint", None)
        if callable(override) and override is not _session_list_cache_state_db_fingerprint:
            return override(state_db_path)
    except Exception:
        pass
    return _session_list_cache_state_db_fingerprint_impl(state_db_path)


def _session_list_cache_state_db_fingerprint_impl(state_db_path: Path | None):
    if state_db_path is None:
        return None
    try:
        from api.models import _sqlite_content_fingerprint

        return _sqlite_content_fingerprint(state_db_path)
    except Exception:
        return None


def _session_list_cache_source_stamp(key: tuple) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int], object, int]:
    _cache_profile, _cache_all_profiles, _cache_show_cli_sessions, *_rest = key
    try:
        swv = _session_list_cache_settings_write_version()
    except Exception:
        swv = 0
    # WebUI-origin sessions can also receive settled rows in state.db when the
    # official Hermes Desktop App continues the same agent session.  The sidebar
    # therefore watches state.db even when the CLI/external-session tab is hidden.
    #
    # Streaming hold-down (#4672): while a turn is in flight, collapse the
    # volatile state.db-derived components (db/WAL stat, gateway metadata, index
    # stat, content fingerprint) to a marker that only changes when a stream
    # starts or stops. This stops per-token message writes from busting the
    # cache and triggering LOCK-contending rebuilds on every poll. The TTL still
    # forces a periodic rebuild so the streaming session's own count/title stay
    # fresh within the TTL window, and settings_file + the settings write
    # version stay live so user-initiated sidebar/setting toggles invalidate
    # immediately. Skipping the fingerprint's SQLite connect here also makes the
    # streaming-path stamp strictly cheaper than the idle path.
    streaming_marker = _session_list_cache_streaming_freeze_marker()
    if streaming_marker is not None:
        return (
            streaming_marker,
            streaming_marker,
            streaming_marker,
            streaming_marker,
            _session_list_cache_path_stamp(_session_list_cache_settings_file()),
            streaming_marker,
            swv,
        )
    try:
        state_db_path = Path(_session_list_cache_state_db_path())
    except Exception:
        state_db_path = None
    try:
        state_db_wal_path = state_db_path.with_name(f"{state_db_path.name}-wal") if state_db_path is not None else None
    except Exception:
        state_db_wal_path = None
    try:
        gateway_metadata_path = _session_list_cache_gateway_session_metadata_path()
    except Exception:
        gateway_metadata_path = None
    try:
        session_index_path = _session_list_cache_session_dir() / "_index.json"
    except Exception:
        session_index_path = None
    all_profiles_state_stamp = None
    if _cache_all_profiles:
        try:
            from api.models import _all_profiles_cli_contexts
            contexts, _ = _all_profiles_cli_contexts()
            all_profiles_state_stamp = tuple(
                (
                    _session_list_cache_path_stamp(ctx_db),
                    _session_list_cache_path_stamp(ctx_db.with_name(f"{ctx_db.name}-wal") if ctx_db else None),
                    _session_list_cache_state_db_fingerprint(ctx_db),
                )
                for _home, ctx_db, _prof in contexts
            )
        except Exception:
            all_profiles_state_stamp = None

    return (
        _session_list_cache_path_stamp(state_db_path),
        _session_list_cache_path_stamp(state_db_wal_path),
        _session_list_cache_path_stamp(gateway_metadata_path),
        _session_list_cache_path_stamp(session_index_path),
        _session_list_cache_path_stamp(_session_list_cache_settings_file()),
        # Commit-reliable content fingerprint of state.db — the file-stat stamps
        # above can collide under WAL-mode writes (same mtime_ns bucket + WAL
        # frame size), so without this a freshly-committed CLI/gateway session
        # could be served stale for the cache TTL. Mirrors the models-layer fix.
        _session_list_cache_state_db_fingerprint(state_db_path),
        all_profiles_state_stamp,
        swv,
    )


def _session_list_cache_settings_write_version() -> int:
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_settings_write_version", None)
        if callable(override) and override is not _session_list_cache_settings_write_version:
            return int(override())
    except Exception:
        pass
    try:
        from api.config import _SETTINGS_WRITE_VERSION

        return int(_SETTINGS_WRITE_VERSION)
    except Exception:
        return 0


def _session_list_cache_overlay_runtime_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    try:
        active_stream_ids = _session_list_cache_active_stream_ids()
    except Exception:
        active_stream_ids = set()
    try:
        running_cron_jobs = _session_list_cache_running_cron_jobs()
    except Exception:
        running_cron_jobs = {}
    cron_job_prefixes = [(jid, f"cron_{jid}_", started_at) for jid, started_at in running_cron_jobs.items()]
    session_ids = [
        str(row.get("session_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("session_id") or "").strip()
    ]
    live_sessions = {}
    if session_ids:
        with LOCK:
            for sid in session_ids:
                live = SESSIONS.get(sid)
                if live is not None:
                    live_sessions[sid] = live
    overlaid = []
    for row in rows:
        item = dict(row) if isinstance(row, dict) else {}
        sid = str(item.get("session_id") or "").strip()
        live = live_sessions.get(sid)
        if live is not None:
            live_stream_id = getattr(live, "active_stream_id", None)
            item["active_stream_id"] = live_stream_id or None
            item["has_pending_user_message"] = bool(
                getattr(live, "pending_user_message", None)
            )
            for key in ("pending_started_at", "updated_at", "last_message_at"):
                current = _session_list_row_numeric_value(item.get(key))
                raw_live_value = getattr(live, key, None)
                live_value = _session_list_row_numeric_value(raw_live_value)
                if live_value > current:
                    item[key] = raw_live_value
        stream_id = item.get("active_stream_id")
        item["is_streaming"] = bool(stream_id and stream_id in active_stream_ids)
        # #6728: a still-running cron job's session row must not look completed
        # in the sidebar. Cron liveness is only exposed via /api/crons/status,
        # which the sidebar never polls — stamp the flag here so the client can
        # defer its completion/unread transition until the job actually ends.
        # Session ids are cron_{job_id}_{run_timestamp}: only the run started at
        # (or after) the tracked start belongs to the live execution — older runs
        # of the same job stay completed.
        item["cron_running"] = _session_list_row_cron_running(
            sid, item, cron_job_prefixes
        )
        overlaid.append(item)
    overlaid.sort(key=_session_list_runtime_sort_key, reverse=True)
    return overlaid


def _session_list_row_cron_running(
    sid: str, row: dict, cron_job_prefixes: list[tuple[str, str, float]]
) -> bool:
    if not cron_job_prefixes or not sid:
        return False
    created_at = _session_list_row_numeric_value(row.get("created_at"))
    # Longest prefix first, no fall-through: job ids may nest (backup vs
    # backup_full), and the shorter prefix is a valid prefix of the longer one.
    # A session belongs to the longest matching job id — first-match in
    # insertion order, or falling through to a shorter prefix after a time-miss,
    # would let a running shorter-prefix job claim a completed longer-prefix
    # session. Mirrors the max(matches, key=len) convention in
    # api.routes._latest_cron_session_info_for_jobs.
    #
    # #6728 (gate fix): prefixes are built ONLY from RUNNING jobs, so if the
    # true longer owner (backup_full) is not running, longest-prefix sorting
    # never sees it and a running `backup` would otherwise swallow a
    # `cron_backup_full_YYYYMMDD_HHMMSS` session (the leftover `full_...` still
    # starts with nothing it should match). Require the text AFTER the prefix to
    # be exactly a run-timestamp (YYYYMMDD_HHMMSS) so a shorter job id cannot
    # claim a longer job id's session regardless of which jobs are running.
    for _jid, prefix, started_at in sorted(
        cron_job_prefixes, key=lambda item: len(item[1]), reverse=True
    ):
        if sid.startswith(prefix) and _CRON_RUN_TS_RE.fullmatch(sid[len(prefix):]):
            return created_at >= started_at
    return False


def _session_list_row_numeric_value(value) -> float:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0 else 0.0


def _session_list_row_timestamp(row: dict) -> float:
    if not isinstance(row, dict):
        return 0.0
    # Match the frontend `_sessionSortTimestampMs` semantics exactly (#4688 review):
    # the idle base is the FIRST truthy of last_message_at -> updated_at -> created_at
    # (NOT a flat max over all of them — a renamed/metadata-touched idle chat bumps
    # updated_at without new messages and must not outrank a newer chatted session),
    # then pending_started_at is overlaid only as the runtime promotion.
    base = 0.0
    for key in ("last_message_at", "updated_at", "created_at"):
        base = _session_list_row_numeric_value(row.get(key))
        if base > 0:
            break
    pending = _session_list_row_numeric_value(row.get("pending_started_at"))
    return max(base, pending)


def _session_list_row_is_runtime_active(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("is_streaming"):
        return True
    return bool(row.get("active_stream_id") and row.get("has_pending_user_message"))


def _session_list_runtime_sort_key(row: dict) -> tuple[int, float]:
    return (
        1 if _session_list_row_is_runtime_active(row) else 0,
        _session_list_row_timestamp(row),
    )


def _session_list_cache_claim_rebuild(key: tuple) -> tuple[threading.Event, bool]:
    with _SESSIONS_CACHE_LOCK:
        current = _SESSIONS_CACHE_INFLIGHT.get(key)
        if current is not None:
            return current, False
        event = threading.Event()
        _SESSIONS_CACHE_INFLIGHT[key] = event
        return event, True


def _session_list_cache_done(key: tuple, event: threading.Event | None) -> None:
    with _SESSIONS_CACHE_LOCK:
        if event is None:
            return
        if _SESSIONS_CACHE_INFLIGHT.get(key) is event:
            _SESSIONS_CACHE_INFLIGHT.pop(key, None)
    if event is not None:
        event.set()
