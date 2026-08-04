"""Regression tests for standards-based Web Push support."""
import json
from pathlib import Path

import pytest

from api import web_push

ROOT = Path(__file__).resolve().parents[1]
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


@pytest.fixture
def configured_store(tmp_path, monkeypatch):
    path = tmp_path / "push_subscriptions.json"
    monkeypatch.setenv("HERMES_WEBUI_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("HERMES_WEBUI_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(web_push, "_subscriptions_path", lambda: path)
    return path


def valid_subscription(endpoint="https://push.example.test/subscription"):
    return {"endpoint": endpoint, "keys": {"p256dh": "public", "auth": "secret"}}


def test_subscription_store_deduplicates_endpoint(configured_store):
    web_push.save_subscription(valid_subscription())
    web_push.save_subscription(valid_subscription())
    rows = json.loads(configured_store.read_text())
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "https://push.example.test/subscription"
    assert rows[0]["keys"] == {"p256dh": "public", "auth": "secret"}


def test_subscription_rejects_non_https_or_missing_keys(configured_store):
    with pytest.raises(ValueError, match="HTTPS"):
        web_push.save_subscription(valid_subscription("http://not-secure.test"))
    with pytest.raises(ValueError, match="p256dh"):
        web_push.save_subscription({"endpoint": "https://push.example.test", "keys": {}})


def test_delivery_removes_expired_endpoints(configured_store, monkeypatch):
    live = valid_subscription("https://push.example.test/live")
    expired = valid_subscription("https://push.example.test/expired")
    web_push.save_subscription(live)
    web_push.save_subscription(expired)
    monkeypatch.setattr(web_push, "_send", lambda row, payload: 410 if row["endpoint"] == expired["endpoint"] else 201)
    result = web_push.send_notification(title="Done", body="Finished", url="./")
    assert result == {"sent": 1, "expired": 1, "failed": 0}
    assert [row["endpoint"] for row in json.loads(configured_store.read_text())] == [live["endpoint"]]


def test_client_and_worker_use_standards_based_visible_push():
    assert "function enableWebPush" in MESSAGES_JS
    assert "pushManager.subscribe({userVisibleOnly:true" in MESSAGES_JS
    assert "api/web-push/subscribe" in MESSAGES_JS
    assert "self.addEventListener('push'" in SW_JS
    assert "event.waitUntil(self.registration.showNotification" in SW_JS


def test_routes_expose_only_public_vapid_key_and_subscription_mutations():
    assert '"/api/web-push/capability"' in ROUTES
    assert '"/api/web-push/subscribe"' in ROUTES
    assert '"/api/web-push/unsubscribe"' in ROUTES
    assert "HERMES_WEBUI_VAPID_PRIVATE_KEY" not in ROUTES


def test_terminal_stream_completion_dispatches_web_push_only_when_enabled():
    assert "if load_settings().get('notifications_enabled'):" in STREAMING
    assert "from api.web_push import send_notification" in STREAMING
    assert "session_title = str(getattr(s, 'title', '') or '').strip()" in STREAMING
    assert "title=session_title or 'Response complete'" in STREAMING
    assert "Web Push wakes installed iOS PWAs" in STREAMING
