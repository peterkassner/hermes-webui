"""Regression coverage for the active-sidebar Hermes session-ID chip."""

from pathlib import Path


ROOT = Path(__file__).parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_active_session_row_exposes_a_copyable_hermes_session_id():
    """The active conversation must expose its durable Hermes ID, not just a title."""
    sessions = _read("static/sessions.js")
    stylesheet = _read("static/style.css")

    assert "session-id-chip" in sessions
    assert "if(s.session_id===activeSidForSidebar)" in sessions
    assert "copyHermesSessionId" in sessions
    assert "navigator.clipboard.writeText" in sessions
    assert "session-id-chip" in stylesheet


def test_session_action_menu_can_copy_the_exact_hermes_session_id():
    """Right-click actions must expose the same durable ID as the active-row chip."""
    sessions = _read("static/sessions.js")

    assert "function _appendSessionCopyIdAction(menu, session)" in sessions
    assert "_appendSessionCopyIdAction(menu, session);" in sessions
    assert "ICONS.copy" in sessions
    assert "await _copyTextToClipboard(sid);" in sessions
