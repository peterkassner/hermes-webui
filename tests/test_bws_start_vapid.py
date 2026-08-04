"""Regression coverage for optional Web Push startup wiring."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bws_start.sh"


def test_launcher_dry_run_starts_without_optional_vapid_secret_ids(tmp_path):
    """A missing optional push configuration must not make WebUI unavailable."""
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "HERMES_WEBUI_DRY_RUN": "1",
            "HERMES_WEBUI_PW": "test-password-not-a-secret",
        }
    )
    for key in (
        "HERMES_WEBUI_PASSWORD",
        "BWS_ACCESS_TOKEN",
        "HERMES_WEBUI_VAPID_PUBLIC_KEY",
        "HERMES_WEBUI_VAPID_PRIVATE_KEY",
        "HERMES_WEBUI_VAPID_PUBLIC_KEY_SECRET_ID",
        "HERMES_WEBUI_VAPID_PRIVATE_KEY_SECRET_ID",
    ):
        env.pop(key, None)
    result = subprocess.run(
        ["bash", str(LAUNCHER)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
