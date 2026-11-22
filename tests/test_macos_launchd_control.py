import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / "scripts" / "macos_launchd_control.sh"


def _run_control(tmp_path: Path, action: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path)
    return subprocess.run(
        ["/bin/bash", str(CONTROL), action],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_control_script_arms_and_disarms_persistent_service_marker(tmp_path):
    marker = tmp_path / "service-enabled"

    armed = _run_control(tmp_path, "arm")
    assert armed.returncode == 0, armed.stderr
    assert marker.read_text(encoding="utf-8") == "enabled\n"

    disarmed = _run_control(tmp_path, "disarm")
    assert disarmed.returncode == 0, disarmed.stderr
    assert not marker.exists()


def test_control_script_rejects_unknown_actions_without_mutating_marker(tmp_path):
    result = _run_control(tmp_path, "not-an-action")

    assert result.returncode == 64
    assert "Usage:" in result.stderr
    assert not (tmp_path / "service-enabled").exists()
