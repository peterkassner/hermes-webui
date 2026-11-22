"""Execute production view guards and query intent without network access."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(scope='session', autouse=True)
def test_server():
    """Node fixtures do not need a running WebUI."""


def _run(script):
    if not shutil.which('node'):
        pytest.skip('Node required')
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_background_views_do_not_load_when_hidden():
    text = (ROOT/'static/panels.js').read_text()
    functions = '\n'.join(extract_function(text, name, 'async function') for name in ('loadCrons', 'loadKanban'))
    assert _run(functions + """
let _currentPanel='chat';
let calls=0;
const api=()=>{calls++;throw Error('hidden fetch');};
(async()=>{await loadCrons();await loadKanban();console.log(JSON.stringify({calls}));})()
.catch(e=>{console.error(e);process.exit(1)});
""") == {'calls': 0}


@pytest.mark.parametrize('project,intent', [(None, None), ('normal', None), ('cron', 'cron'), ('webhooks', 'webhook')])
def test_only_activated_background_project_requests_its_history(project, intent):
    source = extract_function((ROOT/'static/sessions.js').read_text(), '_sessionListQueryString')
    result = _run(source + f"""
const _allProjects=[{{project_id:'normal',name:'User project'}},{{project_id:'cron',name:'Cron Jobs'}},{{project_id:'webhooks',name:'Webhooks'}}];
const _activeProject={json.dumps(project)},_showAllProfiles=true,_showArchived=false;
const _requestedSessionSidebarSource=()=> 'webui';
const _sessionListExcludeHiddenEnabled=()=>true;
console.log(JSON.stringify(Object.fromEntries(new URLSearchParams(_sessionListQueryString().slice(1)))));
""")
    assert result.get('inventory') == intent
    assert result['all_profiles'] == '1'


def test_leaving_kanban_during_first_load_does_not_start_more_requests():
    source = extract_function((ROOT/'static/panels.js').read_text(), 'loadKanban', 'async function')
    result = _run(source + """
let _currentPanel='kanban',_kanbanInventoryGeneration=0;
const S={activeProfile:'default'},$=()=>null;
const calls=[];
let release;
const boards=new Promise(resolve=>release=resolve);
const loadKanbanBoards=()=>{calls.push('boards');return boards;};
const api=()=>{calls.push('unexpected request');throw Error('late fetch');};
(async()=>{const pending=loadKanban();_currentPanel='chat';release();await pending;console.log(JSON.stringify(calls));})()
.catch(e=>{console.error(e);process.exit(1)});
""")
    assert result == ['boards']
