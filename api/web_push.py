"""Standards-based Web Push subscriptions and delivery for Hermes WebUI.

Private VAPID material is read only from environment variables.  Browser
subscriptions are profile-scoped because WebUI profiles are the local security
boundary; no subscription endpoints or encryption keys are placed in settings.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_SUBSCRIPTIONS = 32
_MAX_ENDPOINT_LENGTH = 2048


def _profile_name() -> str:
    try:
        from api.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default")
    except Exception:
        return "default"


def _subscriptions_path() -> Path:
    """Use profile-aware WebUI state without putting push endpoints in settings."""
    try:
        from api.profiles import get_active_hermes_home

        profile = _profile_name()
        if profile != "default":
            state_dir = get_active_hermes_home() / "webui_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            return state_dir / "push_subscriptions.json"
    except Exception:
        pass
    return STATE_DIR / "push_subscriptions.json"


def public_key() -> str:
    return os.getenv("HERMES_WEBUI_VAPID_PUBLIC_KEY", "").strip()


def private_key() -> str:
    return os.getenv("HERMES_WEBUI_VAPID_PRIVATE_KEY", "").strip()


def vapid_claims() -> dict[str, str]:
    subject = os.getenv("HERMES_WEBUI_VAPID_SUBJECT", "mailto:admin@localhost").strip()
    return {"sub": subject or "mailto:admin@localhost"}


def is_configured() -> bool:
    return bool(public_key() and private_key())


def capability() -> dict[str, Any]:
    """Safe capability response. Never expose VAPID private material."""
    return {"enabled": is_configured(), "public_key": public_key() if is_configured() else ""}


def _read_rows() -> list[dict[str, Any]]:
    path = _subscriptions_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("Ignoring unreadable Web Push subscription store at %s", path)
        return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _write_rows(rows: list[dict[str, Any]]) -> None:
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _validate_subscription(subscription: Any) -> dict[str, Any]:
    if not isinstance(subscription, dict):
        raise ValueError("subscription must be an object")
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys")
    if not endpoint.startswith("https://") or len(endpoint) > _MAX_ENDPOINT_LENGTH:
        raise ValueError("subscription endpoint must be a valid HTTPS URL")
    if not isinstance(keys, dict) or not str(keys.get("p256dh") or "") or not str(keys.get("auth") or ""):
        raise ValueError("subscription must include p256dh and auth keys")
    return {
        "endpoint": endpoint,
        "expirationTime": subscription.get("expirationTime"),
        "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
    }


def save_subscription(subscription: Any) -> dict[str, Any]:
    if not is_configured():
        raise RuntimeError("Web Push is not configured on this server")
    clean = _validate_subscription(subscription)
    with _LOCK:
        rows = [row for row in _read_rows() if row.get("endpoint") != clean["endpoint"]]
        clean["created_at"] = time.time()
        rows.append(clean)
        _write_rows(rows[-_MAX_SUBSCRIPTIONS:])
    return {"ok": True}


def remove_subscription(endpoint: Any) -> dict[str, Any]:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint required")
    with _LOCK:
        rows = _read_rows()
        retained = [row for row in rows if row.get("endpoint") != endpoint]
        if len(retained) != len(rows):
            _write_rows(retained)
    return {"ok": True}


def _send(subscription: dict[str, Any], payload: str) -> int | None:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise RuntimeError("Web Push dependency unavailable; install pywebpush") from exc
    try:
        response = webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=private_key(),
            vapid_claims=vapid_claims(),
            ttl=300,
            headers={"Urgency": "normal", "Topic": "hermes-webui"},
        )
        return int(getattr(response, "status_code", 201))
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in (404, 410):
            return int(status)
        raise


def send_notification(*, title: str, body: str, url: str, profile: str | None = None) -> dict[str, int]:
    """Send a visible Web Push notification and remove expired subscriptions.

    Callers should use this only for terminal events; Safari revokes subscriptions
    that receive invisible push events.
    """
    if profile and profile != _profile_name():
        return {"sent": 0, "expired": 0, "failed": 0}
    if not is_configured():
        return {"sent": 0, "expired": 0, "failed": 0}
    payload = json.dumps({"title": title[:120], "body": body[:512], "url": url}, ensure_ascii=False)
    with _LOCK:
        rows = _read_rows()
    expired: set[str] = set()
    sent = failed = 0
    for row in rows:
        try:
            status = _send(row, payload)
            if status in (404, 410):
                expired.add(str(row.get("endpoint")))
            else:
                sent += 1
        except Exception:
            failed += 1
            logger.warning("Web Push delivery failed", exc_info=True)
    if expired:
        with _LOCK:
            _write_rows([row for row in _read_rows() if str(row.get("endpoint")) not in expired])
    return {"sent": sent, "expired": len(expired), "failed": failed}
