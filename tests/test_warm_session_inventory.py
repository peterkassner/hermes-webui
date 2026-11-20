"""Warm inventory contracts; all state and blocked workers are disposable."""
import threading

import pytest

from api import models, profiles, routes


@pytest.fixture(scope='session', autouse=True)
def test_server():
    """These unit tests never start the application server."""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: str(tmp_path))
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(routes, '_session_list_cache_source_stamp', lambda key: ('stable',))
    models.clear_cli_sessions_cache()
    routes._route_session_list_cache._WARM_SCOPES.clear()
    routes._session_list_cache_clear()
    yield
    routes._route_session_list_cache.stop_warm_scopes()
    models.clear_cli_sessions_cache()
    routes._session_list_cache_clear()


def test_known_id_never_enumerates(monkeypatch):
    monkeypatch.setattr(routes, 'lookup_cli_session_row', lambda *a, **k: {})
    monkeypatch.setattr(routes, 'get_cli_sessions', lambda **k: pytest.fail('global enumeration'))
    monkeypatch.setattr(routes, 'get_cli_session_by_id', lambda sid, *a, **k: {'session_id': sid})
    assert routes._lookup_cli_session_metadata('outside-window')['session_id'] == 'outside-window'


def test_exact_id_disables_all_other_inventory_passes(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(models, '_resolve_cli_sessions_context', lambda *a, **k: (tmp_path, tmp_path/'state.db', 'default', ('identity',)))
    def load(*args, **kwargs):
        captured.append(kwargs)
        return [{'session_id': 'old'}]
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', load)
    assert models.get_cli_session_by_id('old', 'cli')['session_id'] == 'old'
    assert captured[0]['session_ids'] == ('old',)
    for name in ('project_assigned_limit', 'cron_project_limit', 'webhook_project_limit', 'kanban_project_limit'):
        assert captured[0].get(name) is False, name


@pytest.mark.parametrize('layer', ['route', 'cli'])
def test_cold_owner_deadline_never_starts_duplicate(monkeypatch, layer, tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    def build(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        return {'sessions': []} if layer == 'route' else []
    if layer == 'route':
        monkeypatch.setattr(routes, '_SESSIONS_CACHE_WAIT_SECONDS', 0)
        key = routes._session_list_cache_key('default', False, False, False, False)
        read = lambda: routes._get_cached_session_list_payload(key=key, builder=build)
    else:
        monkeypatch.setattr(models, '_CLI_SESSIONS_CACHE_WAIT_SECONDS', 0)
        monkeypatch.setattr(models, '_load_cli_sessions_uncached', build)
        read = models.get_cli_sessions
    errors = []
    def owner():
        try:
            read()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=owner)
    worker.start()
    try:
        assert started.wait(2)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                read()
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors


def test_failed_cli_read_is_not_successful_empty_inventory(monkeypatch):
    def broken(*a, **k):
        raise OSError('synthetic unavailable store')
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', broken)
    with pytest.raises(RuntimeError):
        models.get_cli_sessions(require_complete=True)
    assert not models._CLI_SESSIONS_CACHE


@pytest.mark.parametrize('all_profiles', [False, True])
def test_interactive_discovery_disables_background_passes(monkeypatch, tmp_path, all_profiles):
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: ([(tmp_path, tmp_path/'state.db', 'default')], ('profiles',)))
    captured = []
    def load(*args, **kwargs):
        captured.append(kwargs)
        return []
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', load)
    models.get_cli_sessions(all_profiles=all_profiles, include_claude_code=False, discovery_intent='interactive')
    for kwargs in captured:
        for name in ('cron_project_limit', 'webhook_project_limit', 'kanban_project_limit'):
            assert kwargs[name] is False


def _warm_store(monkeypatch, tmp_path):
    import json
    cache = routes._route_session_list_cache
    folder = tmp_path / 'sessions'
    folder.mkdir()
    monkeypatch.setattr(cache, '_session_list_cache_session_dir', lambda: folder)
    monkeypatch.setattr(cache, '_session_list_cache_settings_file', lambda: tmp_path/'settings.json')
    monkeypatch.setattr(cache, '_session_list_cache_gateway_session_metadata_path', lambda: tmp_path/'gateway.json')
    monkeypatch.setattr(profiles, 'get_hermes_home_for_profile', lambda name: tmp_path)
    settings = {'show_cli_sessions': True, 'show_claude_code_sessions': False}
    monkeypatch.setattr(routes, 'load_settings', lambda: dict(settings))
    path = folder/'one.json'
    path.write_text(json.dumps({'session_id': 'one', 'title': 'Before', 'profile': 'default',
                                'message_count': 2, 'source_tag': 'webui', 'updated_at': 1}))
    calls = []
    def load(**kwargs):
        calls.append(1)
        return [json.loads(p.read_text()) for p in folder.glob('*.json')]
    monkeypatch.setattr(routes, 'all_sessions', load)
    monkeypatch.setattr(routes, 'get_cli_sessions', lambda **kw: [])
    monkeypatch.setattr(routes, '_reconcile_stale_stream_state_for_session_rows', lambda rows: False)
    monkeypatch.setattr(routes, '_prune_orphaned_desktop_sidecar_sessions', lambda rows, **kw: rows)
    monkeypatch.setattr(routes, '_prune_orphaned_webui_zero_message_sessions', lambda rows, **kw: rows)
    monkeypatch.setattr(routes, '_enrich_sidebar_lineage_metadata', lambda rows: None)
    return path, calls, settings


def test_no_client_warm_reuses_one_snapshot_for_filtered_reads(monkeypatch, tmp_path):
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    assert len(calls) == 1
    options = routes._interactive_inventory_options()
    for i in range(10):
        result = routes._get_shared_session_list_payload(**options, include_archived=bool(i % 2),
                    sidebar_source='webui' if i % 2 else None, visible_only=True)
        assert result['sessions'][0]['title'] == 'Before'
        result['sessions'][0]['title'] = 'client copy'
    routes._reconcile_warm_session_inventories()
    assert len(calls) == 1


def test_no_client_sidecar_edit_delete_settings_and_replacement(monkeypatch, tmp_path):
    import json
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    row = json.loads(path.read_text())
    row['title'], row['project_id'] = 'After', 'project-two'
    path.write_text(json.dumps(row))
    routes._reconcile_warm_session_inventories()
    options = routes._interactive_inventory_options()
    result = routes._get_shared_session_list_payload(**options)
    assert result['sessions'][0]['title'] == 'After'
    assert result['sessions'][0]['project_id'] == 'project-two'
    settings['show_previous_messaging_sessions'] = True
    routes._reconcile_warm_session_inventories()
    assert len(routes._route_session_list_cache._WARM_SCOPES) == 1
    path.unlink()
    routes._reconcile_warm_session_inventories()
    result = routes._get_shared_session_list_payload(**routes._interactive_inventory_options())
    assert result['sessions'] == []
    assert len(calls) == 4


def test_warm_snapshot_survives_age_and_blocked_refresh(monkeypatch, tmp_path):
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    key = next(iter(cache._WARM_SCOPES))
    ts, stamp, payload = cache._SESSIONS_CACHE[key]
    cache._SESSIONS_CACHE[key] = (ts-1000, stamp, payload)
    event, owner = routes._session_list_cache_claim_rebuild(key)
    assert owner
    try:
        assert routes._get_shared_session_list_payload(**routes._interactive_inventory_options())['sessions']
        assert len(calls) == 1
    finally:
        routes._session_list_cache_done(key, event)


def test_retired_scope_cannot_publish_and_shutdown_releases_waiters(monkeypatch, tmp_path):
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    key = next(iter(cache._WARM_SCOPES))
    generation = routes._session_list_cache_invalidation_stamp(key)
    event, owner = routes._session_list_cache_claim_rebuild(key)
    cache.stop_warm_scopes()
    assert event.is_set()
    assert not cache._WARM_SCOPES
    assert not routes._session_list_cache_set(key, {'sessions': []}, expected_invalidation_stamp=generation)


def test_exact_id_real_sqlite_reader_does_not_run_other_passes(monkeypatch, tmp_path):
    from tests.test_pr7555_cli_session_availability import _make_state_db, _configure_real_single_profile
    db = tmp_path/'state.db'
    _make_state_db(db, sessions=25, messages_per_session=1)
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    original = models.read_importable_agent_session_rows
    calls = []
    def reader(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(models, 'read_importable_agent_session_rows', reader)
    row = models.get_cli_session_by_id('cli_perf_0000')
    assert row['session_id'] == 'cli_perf_0000'
    assert len(calls) == 1
    assert calls[0]['session_ids'] == ('cli_perf_0000',)


def test_identical_ids_in_two_profiles_preserve_context_priority(monkeypatch, tmp_path):
    from tests.test_pr7555_cli_session_availability import _make_state_db, _configure_real_single_profile
    homes = [tmp_path/'a', tmp_path/'b']
    for home in homes:
        home.mkdir()
        _make_state_db(home/'state.db', sessions=1, messages_per_session=1)
    _configure_real_single_profile(monkeypatch, homes[0], homes[0]/'state.db')
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: (
        [(home, home/'state.db', name) for home, name in zip(homes, ['default', 'work'], strict=True)], ('owners',)))
    assert models.get_cli_session_by_id('cli_perf_0000', all_profiles=True)['profile'] == 'default'


def test_watcher_uses_shared_rows_without_another_database_scan(monkeypatch, tmp_path):
    from api import gateway_watcher as gw
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    monkeypatch.setattr(gw, '_get_agent_sessions_from_db', lambda *a: pytest.fail('duplicate discovery'))
    monkeypatch.setattr(gw, '_cheap_change_fingerprint', lambda *a: pytest.fail('duplicate scan'))
    watcher = gw.GatewayWatcher(profile_name='default', hermes_home=tmp_path)
    q = watcher.subscribe()
    assert watcher._poll_once()
    assert q.get_nowait()['type'] == 'sessions_changed'
    assert not watcher._poll_once()
    watcher.unsubscribe(q)
    watcher.stop()


def test_strict_expired_cli_snapshot_returns_before_refresh_finishes(monkeypatch, tmp_path):
    import time
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', lambda *a, **k: [{'session_id': 'before'}])
    assert models.get_cli_sessions(require_complete=True) == [{'session_id': 'before'}]
    with models._CLI_SESSIONS_CACHE_LOCK:
        key = next(iter(models._CLI_SESSIONS_CACHE))
        _, stamp, rows = models._CLI_SESSIONS_CACHE[key]
        models._CLI_SESSIONS_CACHE[key] = (time.monotonic()-100, stamp, rows)
    def load(*a, **k):
        entered.set()
        assert release.wait(3)
        return [{'session_id': 'after'}]
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', load)
    original_done = models._cli_sessions_cache_done
    def finish(*a):
        original_done(*a)
        done.set()
    monkeypatch.setattr(models, '_cli_sessions_cache_done', finish)
    try:
        assert models.get_cli_sessions(require_complete=True) == [{'session_id': 'before'}]
        assert entered.wait(1)
        assert models.get_cli_sessions(require_complete=True) == [{'session_id': 'before'}]
    finally:
        release.set()
        assert done.wait(3)
    assert models.get_cli_sessions(require_complete=True) == [{'session_id': 'after'}]


def test_no_client_watcher_owns_reconciliation_and_stops(monkeypatch, tmp_path):
    from api import gateway_watcher as gw
    entered = threading.Event()
    watcher = gw.GatewayWatcher(profile_name='default', hermes_home=tmp_path)
    watcher.warm_inventory = True
    def reconcile(*, startup=False):
        assert startup
        entered.set()
    monkeypatch.setattr(routes, '_reconcile_warm_session_inventories', reconcile)
    watcher.start()
    try:
        assert entered.wait(2)
        assert not watcher._has_subscribers()
    finally:
        watcher.stop()
    assert not watcher.is_alive()


def test_failed_warm_build_keeps_eligible_prior_snapshot(monkeypatch, tmp_path):
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    key = next(iter(routes._route_session_list_cache._WARM_SCOPES))
    def broken(**kwargs):
        raise OSError('synthetic read failure')
    monkeypatch.setattr(routes, 'all_sessions', broken)
    routes._refresh_shared_inventory(key, routes._interactive_inventory_options())
    assert routes._get_shared_session_list_payload(**routes._interactive_inventory_options())['sessions'][0]['title'] == 'Before'
    assert key not in routes._SESSIONS_CACHE_INFLIGHT


def test_database_replacement_changes_warm_fingerprint(monkeypatch, tmp_path):
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    routes._reconcile_warm_session_inventories(startup=True)
    cache = routes._route_session_list_cache
    key = next(iter(cache._WARM_SCOPES))
    db = tmp_path/'state.db'
    db.write_bytes(b'first')
    first = cache.warm_scope_fingerprint(key)
    replacement = tmp_path/'new.db'
    replacement.write_bytes(b'other')
    replacement.replace(db)
    assert cache.warm_scope_fingerprint(key) != first


def test_cli_owner_survives_revision_changes_without_duplicate(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    revision = [1]
    calls=[]
    def context(source=None, **kw):
        return tmp_path, tmp_path/'state.db', 'default', (
            str(tmp_path),'default',str(tmp_path/'state.db'),source or '', revision[0],False,None,None,revision[0])
    monkeypatch.setattr(models, '_resolve_cli_sessions_context', context)
    monkeypatch.setattr(models, '_CLI_SESSIONS_CACHE_WAIT_SECONDS', 0)
    def load(*a, **kw):
        calls.append(1);entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(models, '_load_cli_sessions_uncached', load)
    worker=threading.Thread(target=models.get_cli_sessions)
    worker.start()
    try:
        assert entered.wait(1)
        revision[0]=2
        with pytest.raises(models.SessionInventoryUnavailable):
            models.get_cli_sessions()
        assert len(calls)==1
    finally:
        release.set();worker.join(3)


def test_archived_rows_do_not_consume_visible_lineage_allowance(monkeypatch, tmp_path):
    import json
    path, calls, settings = _warm_store(monkeypatch, tmp_path)
    row=json.loads(path.read_text())
    for i in range(2):
        (path.parent/f'archived-{i}.json').write_text(json.dumps(dict(row,
            session_id=f'archived-{i}',archived=True,updated_at=20+i)))
    monkeypatch.setenv('HERMES_WEBUI_LINEAGE_TOP_N','2')
    def enrich(rows):
        for row in rows[:2]:
            row['_lineage_root_id']=row['session_id']
    monkeypatch.setattr(routes,'_enrich_sidebar_lineage_metadata',enrich)
    routes._reconcile_warm_session_inventories(startup=True)
    result=routes._get_shared_session_list_payload(**routes._interactive_inventory_options(),include_archived=False)
    assert result['sessions'][0]['_lineage_root_id']=='one'


def test_complete_refresh_sees_external_delete_during_stream_freeze(monkeypatch, tmp_path):
    import sqlite3
    from tests.test_pr7555_cli_session_availability import _make_state_db, _configure_real_single_profile
    db=tmp_path/'state.db'
    _make_state_db(db,sessions=2,messages_per_session=1)
    _configure_real_single_profile(monkeypatch,tmp_path,db)
    monkeypatch.setattr(models,'_cli_sessions_streaming_freeze_marker',lambda: ('streaming',('run',)))
    assert any(r['session_id']=='cli_perf_0000' for r in models.get_cli_sessions(require_complete=True))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM messages WHERE session_id='cli_perf_0000'")
        conn.execute("DELETE FROM sessions WHERE id='cli_perf_0000'")
    assert not any(r['session_id']=='cli_perf_0000' for r in models.get_cli_sessions(require_complete=True))


def test_reconciliation_refreshes_changed_sidecar_inputs_even_with_same_sql_revision(monkeypatch):
    title=['Before'];calls=[]
    def load(*a, **k):
        calls.append(1)
        return [{'session_id':'one','title':title[0]}]
    monkeypatch.setattr(models,'_load_cli_sessions_uncached',load)
    assert models.get_cli_sessions(require_complete=True)[0]['title']=='Before'
    title[0]='After'
    assert models.get_cli_sessions(require_complete=True)[0]['title']=='Before'
    assert models.get_cli_sessions(require_complete=True,force_refresh=True)[0]['title']=='After'
    assert len(calls)==2


def test_background_view_refresh_preserves_request_profile(monkeypatch):
    import time
    key=routes._session_list_cache_key('work',False,False,False,False)
    routes._session_list_cache_set(key,{'sessions':[]})
    cache=routes._route_session_list_cache
    _,stamp,payload=cache._SESSIONS_CACHE[key]
    cache._SESSIONS_CACHE[key]=(time.monotonic()-100,stamp,payload)
    finished=threading.Event();seen=[]
    original_done=routes._session_list_cache_done
    def done(*args):
        original_done(*args);finished.set()
    monkeypatch.setattr(routes,'_session_list_cache_done',done)
    def build():
        seen.append(getattr(profiles._tls,'profile',None))
        return {'sessions':[]}
    assert routes._get_cached_session_list_payload(key=key,builder=build)=={'sessions':[]}
    assert finished.wait(2)
    assert seen==['work']
