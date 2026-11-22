"""Activity regressions using isolated stores and the normal interactive routes."""
import json
import threading

from api import routes
from tests.test_warm_session_inventory import _warm_store, isolated, test_server  # noqa: F401


def test_changed_title_remains_available_during_blocked_refresh(monkeypatch, tmp_path):
    path, _, _ = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    options = routes._interactive_inventory_options()
    row = json.loads(path.read_text())
    row['title'] = 'After'
    path.write_text(json.dumps(row))
    entered, release = threading.Event(), threading.Event()
    original = routes.all_sessions

    def blocked(**kwargs):
        rows = original(**kwargs)
        entered.set()
        assert release.wait(5)
        return rows

    monkeypatch.setattr(routes, 'all_sessions', blocked)
    worker = threading.Thread(target=routes._reconcile_warm_session_inventories)
    worker.start()
    try:
        assert entered.wait(3)
        row['title'] = 'Later'
        path.write_text(json.dumps(row))
        for _ in range(4):
            assert routes._get_shared_session_list_payload(**options)['sessions'][0]['title'] == 'Before'
    finally:
        release.set()
        worker.join(5)
    assert routes._get_shared_session_list_payload(**options)['sessions'][0]['title'] == 'After'
    routes._reconcile_warm_session_inventories()
    assert routes._get_shared_session_list_payload(**options)['sessions'][0]['title'] == 'Later'


def test_atomic_home_heartbeat_does_not_rebuild(monkeypatch, tmp_path):
    _, calls, _ = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    for _ in range(3):
        heartbeat = tmp_path / 'gateway_state.tmp'
        heartbeat.write_text('{}')
        heartbeat.replace(tmp_path / 'gateway_state.json')
        routes._reconcile_warm_session_inventories()
    assert len(calls) == 1


def test_new_candidate_deleted_before_swap_in_wal(monkeypatch, tmp_path):
    import sqlite3
    from api import agent_sessions
    from contextlib import closing

    path, _, _ = _warm_store(monkeypatch, tmp_path)
    db = tmp_path / 'state.db'
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)')
        routes._reconcile_warm_session_inventories(startup=True)
        writer.execute("INSERT INTO sessions VALUES ('new', 'cli')")
        writer.commit()
        captured, resume = threading.Event(), threading.Event()
        original = routes.all_sessions
        borrowed = []

        def load(**kwargs):
            rows = original(**kwargs)
            with agent_sessions.inventory_connection(db) as conn:
                borrowed.append(conn)
                if conn.execute("SELECT id FROM sessions WHERE id='new'").fetchone():
                    rows.append(dict(rows[0], session_id='new', title='New'))
                captured.set()
                assert resume.wait(5)
                # The writer committed, but this builder still reads its pinned view.
                assert conn.execute("SELECT id FROM sessions WHERE id='new'").fetchone()
            return rows

        monkeypatch.setattr(routes, 'all_sessions', load)
        closed = threading.Event()

        def reconcile():
            routes._reconcile_warm_session_inventories()
            try:
                borrowed[0].execute('SELECT 1')
            except sqlite3.ProgrammingError as exc:
                if 'closed' in str(exc):
                    closed.set()

        worker = threading.Thread(target=reconcile)
        worker.start()
        try:
            assert captured.wait(3)
            writer.execute("DELETE FROM sessions WHERE id='new'")
            writer.commit()
        finally:
            resume.set()
            worker.join(5)
        result = routes._get_shared_session_list_payload(**routes._interactive_inventory_options())
        assert [r['session_id'] for r in result['sessions']] == ['one']
        assert closed.is_set()


def test_replacement_refused_by_all_warm_consumers_before_tick(monkeypatch, tmp_path):
    import sqlite3
    from contextlib import closing

    _warm_store(monkeypatch, tmp_path)
    db = tmp_path / 'state.db'
    for name in (db, tmp_path / 'replacement'):
        with closing(sqlite3.connect(name)) as conn:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)')
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    assert cache.lookup_warm_session_row('one')
    (tmp_path / 'replacement').replace(db)
    assert cache.lookup_warm_session_row('one') == {}
    assert cache.warm_gateway_snapshot('default', tmp_path) == ()
    key = next(iter(cache._WARM_SCOPES))
    assert routes._session_list_cache_get(key, allow_stale=True) == (None, False)


def test_external_archive_removes_only_affected_row_during_rebuild(monkeypatch, tmp_path):
    path, _, _ = _warm_store(monkeypatch, tmp_path)
    row = json.loads(path.read_text())
    (path.parent / 'two.json').write_text(json.dumps(dict(row, session_id='two')))
    routes._reconcile_warm_session_inventories(startup=True)
    path.write_text(json.dumps(dict(row, archived=True)))
    cache = routes._route_session_list_cache
    key = next(iter(cache._WARM_SCOPES))
    assert cache.warm_scope_needs_refresh(key)
    assert cache.lookup_warm_session_row('two')
    assert not cache.lookup_warm_session_row('one')
    routes._reconcile_warm_session_inventories()
    options = routes._interactive_inventory_options()
    assert {r['session_id'] for r in routes._get_shared_session_list_payload(**options)['sessions']} == {'two'}
    assert cache.lookup_warm_session_row('one')['archived']


def test_eviction_keeps_startup_and_fences_readmitted_key(monkeypatch, tmp_path):
    _warm_store(monkeypatch, tmp_path)
    cache = routes._route_session_list_cache
    routes._reconcile_warm_session_inventories(startup=True)
    startup = next(iter(cache._WARM_SCOPES))
    options = routes._interactive_inventory_options()
    key = startup + ('alternate',)
    cache.admit_warm_scope(key, options, str(tmp_path))
    old_scope = cache._WARM_SCOPES[key]
    pending, owner = routes._session_list_cache_claim_rebuild(key)
    assert owner
    for i in range(cache._WARM_SCOPE_LIMIT):
        cache.admit_warm_scope(startup + (i,), options, str(tmp_path))
    assert pending.is_set()
    assert cache.lookup_warm_session_row('one')
    cache.admit_warm_scope(key, options, str(tmp_path))
    assert not routes._session_list_cache_set(key, {'sessions': []},
        warm_publication=(old_scope, 0, 0, None, {}))
    routes._session_list_cache_done(key, pending)


def test_safety_check_error_refuses_stale_rows(monkeypatch, tmp_path):
    import pytest
    path, _, _ = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    key = next(iter(cache._WARM_SCOPES))
    path.write_text('{"broken":')
    with pytest.raises(ValueError):
        cache.warm_scope_needs_refresh(key)
    assert cache.lookup_warm_session_row('one') == {}


def test_committed_change_wins_both_publication_orders(monkeypatch, tmp_path):
    import pytest

    _warm_store(monkeypatch, tmp_path)
    cache = routes._route_session_list_cache
    routes._reconcile_warm_session_inventories(startup=True)
    key = next(iter(cache._WARM_SCOPES))
    scope = cache._WARM_SCOPES[key]
    payload = cache._SESSIONS_CACHE[key][2]
    kwargs = {'warm_publication': (scope, scope['safety'], scope['content'],
                                   scope['fingerprint'], scope['proofs'])}
    assert routes._session_list_cache_set(key, payload, **kwargs)
    cache.apply_warm_change('default', 'one', {'title': 'Committed'})
    assert not routes._session_list_cache_set(key, payload, **kwargs)
    assert cache.lookup_warm_session_row('one')['title'] == 'Committed'
    routes._on_session_list_changed('default', reason='session_delete', session_id='one')
    assert cache.lookup_warm_session_row('one') == {}
    assert not routes._session_list_cache_set(key, payload, **kwargs)
    routes._on_session_list_changed('default', reason='unknown-restriction')
    assert cache.warm_gateway_snapshot('default', tmp_path) == ()
    monkeypatch.setattr(routes, 'all_sessions', lambda **kw: (_ for _ in ()).throw(OSError()))
    with pytest.raises(routes.SessionInventoryUnavailable):
        routes._get_shared_session_list_payload(**routes._interactive_inventory_options())


def test_unrelated_blocked_scope_allows_second_safety_check(monkeypatch, tmp_path):
    from api import profiles

    path, _, _ = _warm_store(monkeypatch, tmp_path)
    cache = routes._route_session_list_cache
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setattr(profiles, 'get_active_profile_name',
                        lambda: getattr(profiles._tls, 'profile', None) or 'default')
    def home(profile):
        return work if profile == 'work' else tmp_path
    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: home(profiles.get_active_profile_name()))
    monkeypatch.setattr(profiles, 'get_hermes_home_for_profile', home)
    routes._reconcile_warm_session_inventories(startup=True)
    profiles.set_request_profile('work')
    try:
        options = routes._interactive_inventory_options()
        key = routes._shared_inventory_key(options)
        cache.admit_warm_scope(key, options, str(work))
    finally:
        profiles.clear_request_profile()
    routes._reconcile_warm_session_inventories()
    entered, release = threading.Event(), threading.Event()
    original = routes.all_sessions

    def load(**kwargs):
        if profiles.get_active_profile_name() == 'default':
            entered.set()
            assert release.wait(4)
        return original(**kwargs)

    monkeypatch.setattr(routes, 'all_sessions', load)
    cache.apply_warm_change('default', None, None)
    routes._reconcile_warm_session_inventories(background=True)
    try:
        assert entered.wait(2)
        path.unlink()
        routes._reconcile_warm_session_inventories(background=True)
        assert cache.warm_gateway_snapshot('work', work) in ([], ())
        assert cache._SESSIONS_CACHE[key][2]['sessions'] == []
    finally:
        release.set()
        with cache._SESSIONS_CACHE_LOCK:
            pending = list(cache._SESSIONS_CACHE_INFLIGHT.values())
        assert all(event.wait(3) for event in pending)


def test_resident_and_stale_index_cannot_restore_external_changes(monkeypatch, tmp_path):
    from collections import OrderedDict
    from api import models

    path, _, _ = _warm_store(monkeypatch, tmp_path)
    monkeypatch.setattr(models, 'SESSION_DIR', path.parent)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', path.parent / '_index.json')
    monkeypatch.setattr(models, 'SESSIONS', OrderedDict())
    monkeypatch.setattr(routes, 'all_sessions', models.all_sessions)
    row = json.loads(path.read_text())
    row.update(messages=[dict(role='user', content='fixture')], created_at=1,
               last_message_at=1, archived=False, pinned=False)
    path.write_text(json.dumps(row))
    models.SESSIONS['one'] = models.Session.load('one')
    models.SESSION_INDEX_FILE.write_text(json.dumps([models.SESSIONS['one'].compact()]))
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    assert cache.lookup_warm_session_row('one')['title'] == 'Before'
    path.write_text(json.dumps(dict(row, title='External', archived=True)))
    routes._reconcile_warm_session_inventories()
    observed = cache.lookup_warm_session_row('one')
    assert (observed['archived'], observed['title']) == (True, 'External')
    path.unlink()
    routes._reconcile_warm_session_inventories()
    routes._reconcile_warm_session_inventories()
    assert not cache.lookup_warm_session_row('one')
    assert 'one' in models.SESSIONS  # Sidebar projection leaves run ownership alone.


def test_same_id_safety_proofs_are_scoped_to_owner():
    from api.route_session_list_cache import WarmFingerprint, _unsafe_rows
    empty = WarmFingerprint((), (), (), {}, {}, {}, ())
    rows = [{'session_id': 'same', 'profile': p} for p in ('default', 'work')]
    before = {'default': {'same': ('cli',)}, 'work': {'same': ('cli',)}}
    after = {'default': {}, 'work': {'same': ('cli',)}}
    assert _unsafe_rows(rows, empty, empty, before, after) == {('default', 'same')}
