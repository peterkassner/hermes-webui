#!/usr/bin/env python3
"""Merge directory-valued exports from ~/.zsh/envVars.zsh into WebUI workspaces.

The WebUI launcher is not an interactive shell. This script deliberately sources
only the user's path-definition file in a clean zsh process, never ~/.zshrc:
loading an interactive shell during service startup would run unrelated hooks and
expose WebUI startup to avoidable failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def exported_names(env_file: Path) -> list[str]:
    """Return names explicitly exported by the path-definition file, in file order."""
    text = env_file.read_text(encoding="utf-8")
    return list(dict.fromkeys(_EXPORT_RE.findall(text)))


def directory_exports(env_file: Path, zsh: str = "zsh") -> dict[str, Path]:
    """Evaluate explicit exports and retain only values that are directories.

    Values are emitted by zsh only after the directory check, so tokens and other
    non-path exports from the file never enter this process's output.
    """
    names = exported_names(env_file)
    if not names:
        return {}
    program = r'''
source "$1" || exit $?
shift
for name in "$@"; do
  value=${(P)name}
  [[ -d "$value" ]] && print -r -- "$name"$'\t'"$value"
done
exit 0
'''
    completed = subprocess.run(
        [zsh, "-dfc", program, "zsh", str(env_file), *names],
        check=True,
        capture_output=True,
        text=True,
    )
    result: dict[str, Path] = {}
    for line in completed.stdout.splitlines():
        try:
            name, raw_path = line.split("\t", 1)
        except ValueError:
            continue
        if name in names:
            path = Path(raw_path).expanduser().resolve()
            if path.is_dir():
                result[name] = path
    return result


def grouped_paths(exports: dict[str, Path]) -> list[tuple[Path, list[str]]]:
    """Group aliases resolving to the same directory with stable display order."""
    groups: dict[Path, list[str]] = defaultdict(list)
    for name, path in exports.items():
        groups[path].append(name)
    return [
        (path, sorted(names, key=lambda name: (len(name), name)))
        for path, names in sorted(groups.items(), key=lambda item: str(item[0]).casefold())
    ]


def display_name(names: Iterable[str]) -> str:
    return " / ".join(f"${name}" for name in names)


def load_workspace_list(workspace_file: Path) -> list[dict[str, str]]:
    if not workspace_file.exists():
        return []
    try:
        data = json.loads(workspace_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read workspace state {workspace_file}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Workspace state {workspace_file} must contain a JSON list")
    return [item for item in data if isinstance(item, dict) and item.get("path")]


def atomic_write_json(path: Path, data: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def merge_workspaces(existing: list[dict[str, str]], exports: dict[str, Path]) -> tuple[list[dict[str, str]], int]:
    """Add configured paths without renaming or removing existing picker choices."""
    known_paths: set[Path] = set()
    for item in existing:
        try:
            known_paths.add(Path(str(item["path"])).expanduser().resolve())
        except (OSError, RuntimeError):
            continue

    merged = list(existing)
    added = 0
    for path, names in grouped_paths(exports):
        if path in known_paths:
            continue
        merged.append({"path": str(path), "name": display_name(names)})
        known_paths.add(path)
        added += 1
    return merged, added


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / ".zsh" / "envVars.zsh",
        help="zsh file containing exported path definitions",
    )
    parser.add_argument(
        "--workspace-file",
        type=Path,
        default=Path(os.environ.get("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui")))/"workspaces.json",
        help="WebUI workspace JSON file",
    )
    parser.add_argument("--zsh", default="zsh", help="zsh executable")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_file = args.env_file.expanduser()
    workspace_file = args.workspace_file.expanduser()
    if not env_file.is_file():
        print(f"zsh workspace sync skipped: missing {env_file}", file=sys.stderr)
        return 0

    exports = directory_exports(env_file, args.zsh)
    existing = load_workspace_list(workspace_file)
    merged, added = merge_workspaces(existing, exports)
    if added:
        atomic_write_json(workspace_file, merged)
    print(f"zsh workspace sync: {len(exports)} directory exports, {added} added")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"zsh workspace sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
