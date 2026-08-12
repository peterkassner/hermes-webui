"""Tests for the launch-time zsh workspace synchronizer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_zsh_workspaces.py"
_SPEC = importlib.util.spec_from_file_location("sync_zsh_workspaces", _SCRIPT)
assert _SPEC and _SPEC.loader
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)


def test_exported_names_reads_only_explicit_export_assignments(tmp_path: Path) -> None:
    env_file = tmp_path / "envVars.zsh"
    env_file.write_text(
        "export FIRST=/tmp/first\nSECOND=/tmp/second\nexport FIRST=/tmp/override\n",
        encoding="utf-8",
    )

    assert sync.exported_names(env_file) == ["FIRST"]


def test_directory_exports_evaluates_references_and_filters_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "child").mkdir()
    (root / "file.txt").write_text("not a workspace", encoding="utf-8")
    env_file = tmp_path / "envVars.zsh"
    env_file.write_text(
        f'export ROOT="{root}"\nexport CHILD="$ROOT/child"\nexport FILE="$ROOT/file.txt"\n',
        encoding="utf-8",
    )

    assert sync.directory_exports(env_file) == {
        "ROOT": root.resolve(),
        "CHILD": (root / "child").resolve(),
    }


def test_merge_adds_canonical_paths_and_groups_aliases(tmp_path: Path) -> None:
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    projects = tmp_path / "Projects"
    projects.mkdir()
    existing = [{"path": str(projects), "name": "Manual projects label"}]

    merged, added = sync.merge_workspaces(
        existing,
        {"ONEDRIVE": onedrive, "ONED": onedrive, "PROJECTS": projects},
    )

    assert added == 1
    assert merged == [
        {"path": str(projects), "name": "Manual projects label"},
        {"path": str(onedrive), "name": "$ONED / $ONEDRIVE"},
    ]


def test_atomic_write_preserves_existing_file_mode(tmp_path: Path) -> None:
    workspace_file = tmp_path / "workspaces.json"
    workspace_file.write_text("[]\n", encoding="utf-8")
    workspace_file.chmod(0o640)
    payload = [{"path": "/tmp/workspace", "name": "$WORKSPACE"}]

    sync.atomic_write_json(workspace_file, payload)

    assert json.loads(workspace_file.read_text(encoding="utf-8")) == payload
    assert workspace_file.stat().st_mode & 0o777 == 0o640


def test_load_workspace_list_rejects_non_list_json(tmp_path: Path) -> None:
    workspace_file = tmp_path / "workspaces.json"
    workspace_file.write_text('{"path": "/tmp"}', encoding="utf-8")

    try:
        sync.load_workspace_list(workspace_file)
    except ValueError as exc:
        assert "JSON list" in str(exc)
    else:
        raise AssertionError("expected invalid workspace JSON to be rejected")
