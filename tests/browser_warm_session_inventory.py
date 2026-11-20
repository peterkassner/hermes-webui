"""Disposable four-browser inventory/latency probe, with real SQLite and HTTP.

Run: uv run --no-project --with playwright python tests/browser_warm_session_inventory.py
Requires an already-installed Chromium. Installs no application dependencies.
Use --revision COMMIT for a disposable checkpoint comparison. Never uses live state.
"""
import argparse
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = r'''
import collections, json, resource, sys, threading, time
sys.path.insert(0, sys.argv[1])
import server
from api import models, routes, gateway_watcher
counts = collections.Counter()
lock = threading.Lock()
def wrap(obj, name, label):
    original = getattr(obj, name)
    def measured(*args, **kwargs):
        with lock:
            measured_label = ('point_' + label) if kwargs.get('session_ids') else label
            counts[measured_label] += 1
            if label == 'reader':
                sources = kwargs.get('include_sources') or ()
                for source in ('cron', 'webhook', 'kanban'):
                    if source in sources and not kwargs.get('session_ids'):
                        counts['history_' + source] += 1
        return original(*args, **kwargs)
    setattr(obj, name, measured)
wrap(models, '_load_cli_sessions_uncached', 'cli_builds')
wrap(models, 'read_importable_agent_session_rows', 'reader')
wrap(gateway_watcher, 'read_importable_agent_session_rows', 'watcher_reader')
class ProbeHandler(server.Handler):
    def do_GET(self):
        if self.path == '/__fixture/metrics':
            with lock:
                data = dict(counts)
            data.update(cpu_seconds=time.process_time(), block_inputs=resource.getrusage(resource.RUSAGE_SELF).ru_inblock)
            return routes.j(self, data)
        return super().do_GET()
server.Handler = ProbeHandler
server.main()
'''


def seed(home, state):
    home.mkdir(); state.mkdir()
    (home/'config.yaml').write_text('model: openai/synthetic\n')
    (state/'settings.json').write_text(json.dumps(dict(onboarding_completed=True,
        show_cli_sessions=True, show_claude_code_sessions=False, api_redact_enabled=False)))
    sessions = state/'sessions'; sessions.mkdir()
    db = sqlite3.connect(home/'state.db')
    db.executescript('''
CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT,
model TEXT, started_at REAL, message_count INTEGER, archived INTEGER DEFAULT 0,
project_id TEXT, parent_session_id TEXT, ended_at REAL, end_reason TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
CREATE INDEX idx_sessions_started ON sessions(started_at);
CREATE INDEX idx_messages_session_timestamp ON messages(session_id,timestamp);
''')
    now = time.time(); index = []
    for source, total in [('webui', 80), ('cli', 120), ('cron', 300), ('webhook', 300), ('kanban', 300)]:
        for i in range(total):
            sid = f'{source}-synthetic-{i:04d}'
            title = f'{source.title()} synthetic conversation {i}'
            started = now + i
            db.execute('INSERT INTO sessions(id,source,session_source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?,?)',
                       (sid,source,source,title,'synthetic',started,30))
            messages = [dict(role='user' if j%2==0 else 'assistant',content=f'Synthetic fixture message {j}',timestamp=started+j/100) for j in range(30)]
            db.executemany('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                          [(sid,m['role'],m['content'],m['timestamp']) for m in messages])
            if source == 'webui':
                row = dict(session_id=sid,title=title,workspace=str(home/'workspace'),model='synthetic',
                    model_provider='openai',message_count=30,created_at=started,updated_at=started,
                    profile='default',source_tag='webui',session_source='webui')
                index.append(row)
                (sessions/f'{sid}.json').write_text(json.dumps(dict(row,messages=messages,tool_calls=[])))
    db.commit(); db.close()
    (sessions/'_index.json').write_text(json.dumps(index))
    (state/'projects.json').write_text(json.dumps([
        dict(project_id='user-project',name='Synthetic project',profile='default'),
        dict(project_id='cron-project',name='Cron Jobs',profile='default'),
        dict(project_id='webhook-project',name='Webhooks',profile='default')]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--revision')
    parser.add_argument('--screenshots-only', action='store_true')
    parser.add_argument('--output', default='/tmp/hermes-warm-browser-evidence')
    args=parser.parse_args()
    output=Path(args.output); output.mkdir(parents=True,exist_ok=True)
    scratch=Path(tempfile.mkdtemp(prefix='webui-inventory-browser-'))
    repo=ROOT
    if args.revision:
        repo=scratch/'checkpoint';repo.mkdir()
        archive=subprocess.check_output(['git','archive',args.revision],cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(repo,filter='data')
    home,state=scratch/'agent-home',scratch/'webui'
    seed(home,state)
    launcher=scratch/'serve.py';launcher.write_text(LAUNCHER)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    password=secrets.token_urlsafe(24)
    env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','TMPDIR','LANG')}
    env.update(HOME=str(scratch),HERMES_HOME=str(home),HERMES_BASE_HOME=str(home),
        HERMES_CONFIG_PATH=str(home/'config.yaml'),HERMES_WEBUI_STATE_DIR=str(state),
        HERMES_WEBUI_TEST_STATE_DIR=str(state),HERMES_DISABLE_LAZY_INSTALLS='1',
        HERMES_WEBUI_TEST_NETWORK_BLOCK='1',HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_PORT=str(port),
        HERMES_WEBUI_PASSWORD=password,HERMES_WEBUI_AGENT_DIR=str(Path.home()/'.hermes/hermes-agent'),
        HERMES_WEBUI_DEFAULT_WORKSPACE=str(home/'workspace'),AWS_EC2_METADATA_DISABLED='true')
    from playwright.sync_api import sync_playwright
    log=(output/'server.log').open('w')
    proc=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(launcher),str(repo)],cwd=repo,env=env,stdout=log,stderr=subprocess.STDOUT)
    result={'revision':args.revision or 'working-tree','state':str(scratch),'requests':[]}
    def metrics():
        with urllib.request.urlopen(origin+'/__fixture/metrics',timeout=10) as response:
            return json.load(response)
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(origin+'/health',timeout=1): break
            except Exception:
                if proc.poll() is not None: raise RuntimeError('Disposable server exited; inspect server.log') from None
                time.sleep(.25)
        else: raise RuntimeError('Disposable server startup timed out')
        with sync_playwright() as pw:
            installed = sorted((Path.home()/'Library/Caches/ms-playwright').glob(
                'chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'))
            executable = os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or (str(installed[-1]) if installed else None)
            browser=pw.chromium.launch(headless=True, executable_path=executable)
            contexts=[];pages=[]
            for width,height in [(1280,900),(390,844),(1024,768),(768,1024)]:
                context=browser.new_context(viewport=dict(width=width,height=height))
                context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                assert context.request.post(origin+'/api/auth/login',data={'password':password}).ok
                page=context.new_page()
                page.on('request',lambda request: result['requests'].append(request.url.removeprefix(origin)))
                page.goto(origin,wait_until='domcontentloaded')
                page.wait_for_function("typeof renderSessionList === 'function'",timeout=30000)
                page.evaluate('renderSessionList({deferWhileInteracting:false})')
                contexts.append(context);pages.append(page)
            pages[0].screenshot(path=str(output/'desktop.png'),full_page=True,animations='disabled')
            pages[1].evaluate('toggleMobileSidebar()')
            pages[1].screenshot(path=str(output/'narrow.png'),full_page=True,animations='disabled')
            pages[1].evaluate('closeMobileSidebar()')
            result['after_boot']=metrics()
            if args.screenshots_only:
                for context in contexts: context.close()
                browser.close()
                (output/'results.json').write_text(json.dumps(result,indent=2))
                print('Captured settled desktop and narrow screenshots')
                return
            result['warm_ms']={}
            paths={
                'sidebar':'/api/sessions?sidebar_source=webui&exclude_hidden=1',
                'metadata':'/api/session?session_id=webui-synthetic-0000&messages=0&resolve_model=0',
                'transcript':'/api/session?session_id=webui-synthetic-0000&limit=30&resolve_model=0',
            }
            for label,path in paths.items():
                timings=[];statuses=[]
                for i in range(20):
                    start=time.perf_counter()
                    response=contexts[i%4].request.get(origin+path)
                    timings.append((time.perf_counter()-start)*1000);statuses.append(response.status)
                result['warm_ms'][label]=dict(median=sorted(timings)[10],p95=sorted(timings)[18],statuses=sorted(set(statuses)))
                result['after_'+label]=metrics()
                assert set(statuses) == {200}
            result['after_warm_reads']=metrics()
            # Open an old exact-ID conversation absent from the recent inventory.
            response=contexts[0].request.get(origin+'/api/session?session_id=cli-synthetic-0000&messages=0&resolve_model=0')
            result['direct_old_cli_status']=response.status
            # First activation of background history must request only its source.
            pages[0].evaluate("_setActiveProjectFilter('cron-project')")
            pages[0].evaluate('renderSessionList({deferWhileInteracting:false})')
            result['after_cron_view']=metrics()
            result['cron_rows']=pages[0].evaluate("_allSessions.filter(s=>s.project_id==='cron-project').length")
            pages[0].evaluate("_setActiveProjectFilter('webhook-project')")
            pages[0].evaluate('renderSessionList({deferWhileInteracting:false})')
            result['after_webhook_view']=metrics()
            result['webhook_rows']=pages[0].evaluate("_allSessions.filter(s=>s.project_id==='webhook-project').length")
            pages[0].evaluate("switchPanel('kanban')")
            result['kanban_panel_requests']=[p for p in result['requests'] if p.startswith('/api/kanban')]
            pages[0].evaluate("switchPanel('chat')")
            for context in contexts: context.close()
            # Allow view initialization/direct-open writes to reconcile first;
            # the measured interval must represent an unchanged idle store.
            time.sleep(6)
            before=metrics();time.sleep(11);after=metrics()
            result['no_clients_11s']={k:after.get(k,0)-before.get(k,0) for k in set(before)|set(after)}
            # External edits occur with no browser attached, then reconcile.
            with sqlite3.connect(home/'state.db') as db:
                db.execute("UPDATE sessions SET title='Externally renamed synthetic session' WHERE id='cli-synthetic-0119'")
                inserted = time.time()+10000
                db.execute("INSERT INTO sessions(id,source,session_source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?,?)",
                    ('cli-external-add','cli','cli','Externally added synthetic session','synthetic',inserted,1))
                db.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
                    ('cli-external-add','user','Synthetic external addition',inserted))
            time.sleep(6)
            result['after_external_edit']=metrics()
            context=browser.new_context()
            assert context.request.post(origin+'/api/auth/login',data={'password':password}).ok
            response=context.request.get(origin+'/api/sessions?sidebar_source=cli')
            result['reconnect_status']=response.status
            result['external_edit_visible']=any(row.get('title')=='Externally renamed synthetic session' for row in response.json().get('sessions',[]))
            result['external_add_visible']=any(row.get('session_id')=='cli-external-add' for row in response.json().get('sessions',[]))
            context.close();browser.close()
        result['background_history_before_views']={k:result['after_warm_reads'].get(k,0) for k in ('history_cron','history_webhook','history_kanban')}
        (output/'results.json').write_text(json.dumps(result,indent=2))
        if not args.revision:
            assert not any(result['background_history_before_views'].values())
            assert result['after_sidebar'].get('cli_builds',0) == result['after_boot'].get('cli_builds',0)
            assert result['no_clients_11s'].get('cli_builds',0) == 0
            assert result['no_clients_11s'].get('history_cron',0) == 0
            assert result['no_clients_11s'].get('history_webhook',0) == 0
            assert result['direct_old_cli_status'] == 200 and result['external_edit_visible'] and result['external_add_visible']
            assert result['cron_rows'] > 0 and result['webhook_rows'] > 0
        (output/'results.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in ('requests','state')},indent=2))
    finally:
        proc.terminate()
        try: proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=5)
        log.close()


if __name__=='__main__':
    main()
