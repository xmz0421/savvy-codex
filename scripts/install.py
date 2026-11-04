#!/usr/bin/env python3
"""Install a local checkout into Claude Code's verified skills-dir layout.

Uses only Python's standard library. Never invokes Claude, Codex, or a model.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


CORE = (".claude-plugin", "skills", "agents", "hooks", "types", "LICENSE", "NOTICE")
START = "<!-- BEGIN SAVVY-CODEX-DELEGATION -->"
END = "<!-- END SAVVY-CODEX-DELEGATION -->"
WORKFLOW = "\n".join((
    START,
    "## Savvy Codex default workflow",
    "Load the savvy-codex:savvy-flow skill as the default workflow for project tasks.",
    "Claude leads context, planning, integration, and acceptance. Use Claude and/or",
    "Codex according to the task; simple tasks may be completed directly. Delegate",
    "independent work only when useful, with clear ownership. Select and explain",
    "the model and effort for each delegation based on the task and available models.",
    "This preference does not require calling both models for every task. Honor",
    "the user's current instructions and authorization; never change provider,",
    "credentials, or global model settings to implement this workflow.",
    END,
))


def workflow_text(original: str) -> str:
    """Replace our marked paragraph, preserving all surrounding user content."""
    if START in original or END in original:
        if original.count(START) != 1 or original.count(END) != 1:
            raise ValueError("CLAUDE.md has incomplete or duplicate workflow markers")
        start = original.index(START)
        end = original.index(END) + len(END)
        if original.index(END) < start:
            raise ValueError("CLAUDE.md has reversed workflow markers")
        return original[:start] + WORKFLOW + original[end:]
    return original + ("\n\n" if original and not original.endswith("\n") else "\n" if original else "") + WORKFLOW + "\n"


def read_regular(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite a symlink: {path}")
    return path.read_bytes() if path.exists() else None


def same_tree(left: Path, right: Path) -> bool:
    if not right.is_dir():
        return False
    def files(root: Path) -> dict[str, bytes | None]:
        result = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                return {"<symlink>": None}
            result[str(path.relative_to(root))] = None if path.is_dir() else path.read_bytes()
        return result
    return files(left) == files(right)


def atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".savvy-codex-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(source: Path, claude_home: Path, enable_default_workflow: bool = False) -> dict:
    source = source.expanduser().resolve()
    claude_home = claude_home.expanduser().resolve()
    destination = claude_home / "skills" / "savvy-codex"
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source checkout and installed package must be separate directories")
    for name in CORE:
        if not (source / name).exists():
            raise ValueError(f"Source is missing required component: {name}")
    # Reject links before copying, rather than following a checkout outside its root.
    for name in CORE:
        component = source / name
        if component.is_symlink() or (component.is_dir() and any(p.is_symlink() for p in component.rglob("*"))):
            raise ValueError(f"Source component contains a symlink: {name}")
    if destination.is_symlink() or destination.parent.is_symlink():
        raise ValueError("Installed package and skills directory must not be symlinks")
    if destination.exists() and not destination.is_dir():
        raise ValueError("Installed package path exists and is not a directory")

    settings_path = claude_home / "settings.json"
    old_settings = read_regular(settings_path)
    settings = json.loads(old_settings.decode("utf-8-sig")) if old_settings is not None else {}
    if not isinstance(settings, dict):
        raise ValueError("settings.json must contain an object")
    plugins = settings.setdefault("enabledPlugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("enabledPlugins must contain an object")
    desired = {"savvy-codex@skills-dir": True, "savvy-flow@claude-kit": False, "savvy-progress@claude-kit": False}
    settings_changed = old_settings is None or any(plugins.get(k) is not v for k, v in desired.items())
    plugins.update(desired)
    new_settings = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    claude_path = claude_home / "CLAUDE.md"
    old_claude = read_regular(claude_path) if enable_default_workflow else None
    new_claude = workflow_text(old_claude.decode("utf-8-sig") if old_claude is not None else "").encode("utf-8") if enable_default_workflow else None
    claude_changed = enable_default_workflow and old_claude != new_claude

    claude_home.mkdir(parents=True, exist_ok=True)
    backup = None
    with tempfile.TemporaryDirectory(prefix=".savvy-codex-stage-", dir=claude_home) as temporary:
        staged = Path(temporary) / "savvy-codex"
        staged.mkdir()
        for name in CORE:
            component = source / name
            if component.is_dir():
                ignored = ("__pycache__", "*.pyc", "marketplace.json") if name == ".claude-plugin" else ("__pycache__", "*.pyc")
                shutil.copytree(component, staged / name, ignore=shutil.ignore_patterns(*ignored))
            else:
                shutil.copy2(component, staged / name)
        receipt = {"source": str(source), "installer_python": sys.executable, "plugin_identity": "savvy-codex@skills-dir"}
        (staged / ".install-info.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        package_changed = not same_tree(staged, destination)
        changes = package_changed or settings_changed or claude_changed
        if changes:
            backup_root = claude_home / "backups" / "savvy-codex"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=backup_root))
            # Preserve exact original bytes and the complete old package before writing.
            if settings_changed and old_settings is not None:
                (backup / "settings.json").write_bytes(old_settings)
            if claude_changed and old_claude is not None:
                (backup / "CLAUDE.md").write_bytes(old_claude)
            if package_changed and destination.exists():
                shutil.copytree(destination, backup / "savvy-codex", symlinks=True)
            if package_changed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(staged), str(destination))
            if settings_changed:
                atomic_write(settings_path, new_settings)
            if claude_changed:
                atomic_write(claude_path, new_claude)
    return {"destination": str(destination), "backup": str(backup) if backup else None, "changed": bool(changes), "installer_python": sys.executable}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1], help="Local plugin checkout (default: this repository)")
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude", help="Claude configuration directory")
    parser.add_argument("--enable-default-workflow", action="store_true", help="Add the marked default workflow paragraph to global CLAUDE.md")
    args = parser.parse_args()
    try:
        result = install(args.source, args.claude_home, args.enable_default_workflow)
    except (OSError, ValueError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    print("Installed: " + result["destination"] if result["changed"] else "Already up to date: " + result["destination"])
    if result["backup"]:
        print("Original files backed up: " + result["backup"])
    print("Installer Python: " + result["installer_python"])
    print("This interpreter is recorded in .install-info.json; runtime Python uses PATH or explicit plugin configuration.")
    print("Restart Claude Code to load savvy-codex@skills-dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
