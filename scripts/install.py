#!/usr/bin/env python3
"""Install or remove Savvy Codex, with a local, inference-free readiness check.

Only the three documented plugin flags are changed in Claude settings. Plugin
preferences belong to the host's userConfig storage and are never synthesized
or overwritten by this installer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


CORE = (".claude-plugin", "skills", "agents", "hooks", "types", "shared", "testfixtures", "LICENSE", "NOTICE")
START = "<!-- BEGIN SAVVY-CODEX-DELEGATION -->"
END = "<!-- END SAVVY-CODEX-DELEGATION -->"
IDENTITY = "savvy-codex@skills-dir"
RECEIPT_NAME = ".savvy-codex-install.json"
RECEIPT_OWNER = "savvy-codex-installer"
PLUGIN_FLAGS = {IDENTITY: True, "savvy-flow@claude-kit": False, "savvy-progress@claude-kit": False}


class NotReadyError(ValueError):
    """Readiness failed before any installation changes were made."""

    def __init__(self, message: str, doctor: dict | None = None):
        super().__init__(message)
        self.doctor = doctor or {}
        self.installed = False


def absolute(path: Path) -> Path:
    # resolve() would conceal a symlink/junction supplied by the caller.
    return Path(os.path.abspath(path.expanduser()))


def check_path(path: Path) -> None:
    """Reject links and Windows junctions in a target and every ancestor."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"Refusing a symlink or junction path: {part}")


def check_tree(path: Path) -> None:
    check_path(path)
    if path.is_dir():
        for parent, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                check_path(Path(parent) / name)


def read_regular(path: Path) -> bytes | None:
    check_path(path)
    if path.exists() and not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    return path.read_bytes() if path.exists() else None


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_object(path: Path) -> tuple[bytes | None, dict]:
    original = read_regular(path)
    value = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return original, value


def without_workflow(original: bytes | None) -> bytes | None:
    """Remove only our former global instruction block, preserving other bytes."""
    if original is None:
        return None
    value = original.decode("utf-8")
    if START not in value and END not in value:
        return original
    if value.count(START) != 1 or value.count(END) != 1 or value.index(END) < value.index(START):
        raise ValueError("CLAUDE.md has incomplete, duplicate, or reversed Savvy workflow markers; no files changed")
    return (value[:value.index(START)] + value[value.index(END) + len(END):]).encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    check_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".savvy-codex-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_file() and path.name != ".install-info.json" and "__pycache__" not in relative.parts and path.suffix != ".pyc":
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def same_tree(left: Path, right: Path) -> bool:
    return right.is_dir() and file_hashes(left) == file_hashes(right) and read_regular(left / ".install-info.json") == read_regular(right / ".install-info.json")


def read_receipt(path: Path) -> tuple[bytes | None, dict | None]:
    original, receipt = read_object(path)
    if original is None:
        return None, None
    if receipt.get("owner") != RECEIPT_OWNER or receipt.get("schema_version") != 2 or receipt.get("plugin_identity") != IDENTITY:
        raise ValueError("Unrecognized installation receipt; refusing to guess which settings are owned")
    if receipt.get("status") not in ("installed", "uninstalled"):
        raise ValueError("Installation receipt has an invalid status")
    flags = receipt.get("plugin_flags", {})
    if not isinstance(flags, dict) or any(key not in PLUGIN_FLAGS for key in flags):
        raise ValueError("Installation receipt has invalid owned plugin flags")
    for key, value in flags.items():
        if not isinstance(value, dict) or type(value.get("before_exists")) is not bool or value.get("after") is not PLUGIN_FLAGS[key]:
            raise ValueError("Installation receipt has invalid plugin restoration data")
        if value["before_exists"] and "before" not in value:
            raise ValueError("Installation receipt is missing a previous plugin flag value")
    hashes = receipt.get("package_hashes", {})
    if not isinstance(hashes, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in hashes.items()):
        raise ValueError("Installation receipt has invalid package hashes")
    return original, receipt


def doctor_preflight(source: Path) -> dict:
    if sys.version_info < (3, 10):
        raise NotReadyError("Python 3.10 or later is required")
    helper = source / "skills" / "codex-worker" / "scripts" / "codex_delegate.py"
    if not helper.is_file():
        raise NotReadyError(f"Bundled doctor is missing: {helper}")
    try:
        process = subprocess.run([sys.executable, str(helper), "doctor", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotReadyError(f"Readiness check could not finish: {error}") from error
    try:
        result = json.loads(process.stdout)
    except ValueError as error:
        raise NotReadyError("Bundled doctor returned invalid JSON; installation was not changed") from error
    if not isinstance(result, dict) or result.get("ready") is not True or process.returncode != 0:
        detail = (result.get("next_action") or result.get("error")) if isinstance(result, dict) else None
        raise NotReadyError(str(detail or "Check official Codex CLI, ChatGPT login, version, features, and model catalog."), result if isinstance(result, dict) else None)
    return result


def paths_for(claude_home: Path) -> tuple[Path, Path, Path, Path]:
    destination = claude_home / "skills" / "savvy-codex"
    paths = (destination, claude_home / "settings.json", claude_home / "CLAUDE.md", claude_home / RECEIPT_NAME)
    for path in (*paths, claude_home / "backups" / "savvy-codex"):
        check_path(path)
    if destination.exists() and not destination.is_dir():
        raise ValueError("Installed package path exists and is not a directory")
    check_tree(destination)
    return paths


def settings_plugins(settings: dict) -> dict:
    plugins = settings.get("enabledPlugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("enabledPlugins must contain an object")
    return plugins


def make_backup(claude_home: Path, files: dict[str, bytes | None], destination: Path, package: bool) -> Path:
    backup_root = claude_home / "backups" / "savvy-codex"
    check_path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=backup_root))
    for name, data in files.items():
        if data is not None:
            (backup / name).write_bytes(data)
    if package and destination.exists():
        shutil.copytree(destination, backup / "savvy-codex")
    return backup


BACKUP_MARKER = ".savvy-backup.json"


def backup_metadata(backup: Path, claude_home: Path, *, successful: bool) -> None:
    atomic_write(backup / BACKUP_MARKER, json_bytes({
        "owner": RECEIPT_OWNER, "plugin_identity": IDENTITY, "schema_version": 1,
        "backup_path": str(absolute(backup)), "claude_home": str(absolute(claude_home)),
        "successful": successful,
    }))


def prune_backups(claude_home: Path, keep: int) -> None:
    """Only remove successful backups that this installer marked and owns."""
    root = absolute(claude_home) / "backups" / "savvy-codex"
    check_path(root)
    candidates = []
    for entry in root.iterdir():
        try:
            check_tree(entry)
            if (not entry.is_dir() or entry.parent != root
                    or re.fullmatch(r"\d{8}T\d{6}Z-[a-z0-9_]{8}", entry.name) is None):
                continue
            _, info = read_object(entry / BACKUP_MARKER)
            if (info.get("owner") == RECEIPT_OWNER and info.get("plugin_identity") == IDENTITY
                    and info.get("schema_version") == 1 and info.get("successful") is True
                    and info.get("backup_path") == str(absolute(entry))
                    and info.get("claude_home") == str(absolute(claude_home))):
                candidates.append(entry)
        except (OSError, ValueError):
            continue
    candidates.sort(key=lambda entry: (entry.stat().st_mtime_ns, entry.name), reverse=True)
    for entry in candidates[keep:]:
        check_tree(entry)
        shutil.rmtree(entry)


def commit_transaction(destination: Path, staged: Path | None, package_changed: bool,
                       mutations: list[tuple[Path, bytes | None, bytes | None]]) -> list[str]:
    """Keep the old package by sibling rename until every file commit succeeds."""
    previous = None
    placed = False
    attempted = []
    try:
        if package_changed:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                previous = Path(tempfile.mkdtemp(prefix=".savvy-codex-rollback-", dir=destination.parent))
                previous.rmdir()
                os.replace(destination, previous)
            if staged is not None:
                placed = True
                shutil.move(str(staged), str(destination))
        for path, before, after in mutations:
            attempted.append((path, before))
            if after is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, after)
    except BaseException as failure:
        errors = []
        for path, before in reversed(attempted):
            try:
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, before)
            except (OSError, ValueError) as error:
                errors.append(str(error))
        try:
            if placed and destination.exists():
                check_tree(destination)
                shutil.rmtree(destination)
            if previous is not None and previous.exists():
                os.replace(previous, destination)
        except (OSError, ValueError) as error:
            errors.append(str(error))
        if errors:
            raise OSError("Transaction failed; recover from preserved backup/sibling: " + "; ".join(errors)) from failure
        raise
    warnings = []
    if previous is not None:
        try:
            check_tree(previous)
            shutil.rmtree(previous)
        except (OSError, ValueError) as error:
            warnings.append(f"Changes committed; old package cleanup could not finish ({type(error).__name__}). Recovery copy retained at {previous}")
    return warnings


def finish_backup(backup: Path, claude_home: Path, keep_backups: int) -> list[str]:
    """Post-commit housekeeping must not turn a committed install into failure."""
    try:
        backup_metadata(backup, claude_home, successful=True)
    except (OSError, ValueError) as error:
        return [f"Changes committed; backup completion marker could not be saved ({type(error).__name__}). Backup retained at {backup}; retention was skipped."]
    try:
        prune_backups(claude_home, keep_backups)
    except (OSError, ValueError) as error:
        return [f"Changes committed; backup retention could not finish ({type(error).__name__}). Remaining backups were preserved under {backup.parent}"]
    return []


def install(source: Path, claude_home: Path, enable_default_workflow: bool = False, *, skip_doctor: bool = False, keep_backups: int = 5) -> dict:
    if type(keep_backups) is not int or keep_backups < 0:
        raise ValueError("keep_backups must be a nonnegative integer")
    source, claude_home = absolute(source), absolute(claude_home)
    check_path(source)
    destination, settings_path, claude_path, receipt_path = paths_for(claude_home)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source checkout and installed package must be separate directories")
    for name in CORE:
        if not (source / name).exists():
            raise ValueError(f"Source is missing required component: {name}")
        check_tree(source / name)
    _, manifest = read_object(source / ".claude-plugin" / "plugin.json")
    if manifest.get("name") != "savvy-codex" or not isinstance(manifest.get("version"), str):
        raise ValueError("Source manifest must identify savvy-codex and its version")
    old_settings, settings = read_object(settings_path)
    plugins = settings_plugins(settings)
    old_receipt, previous = read_receipt(receipt_path)
    old_claude = read_regular(claude_path)
    new_claude = without_workflow(old_claude)
    claude_changed = old_claude != new_claude
    # Complete every read/validation before staging, backups, settings or mkdir.
    try:
        doctor = None if skip_doctor else doctor_preflight(source)
    except NotReadyError as error:
        error.installed = destination.exists()
        raise
    warnings = []
    if skip_doctor:
        warnings.append("Doctor explicitly skipped; installed but readiness is unverified. Run the bundled doctor before delegation.")
    if enable_default_workflow:
        warnings.append("--enable-default-workflow is deprecated. Choose auto in plugin settings if wanted; the default mode is manual. No global workflow is added.")
    if claude_changed:
        warnings.append("Removed the previous global Savvy workflow block; choose manual/auto/off in plugin settings.")

    owned = {}
    previous_flags = previous.get("plugin_flags", {}) if previous and previous["status"] == "installed" else {}
    for key, desired in PLUGIN_FLAGS.items():
        prior = previous_flags.get(key)
        if prior and key in plugins and plugins[key] is prior["after"]:
            owned[key] = prior
        elif key not in plugins or plugins[key] is not desired:
            owned[key] = {"before_exists": key in plugins, "after": desired}
            if key in plugins:
                owned[key]["before"] = plugins[key]
    settings_changed = any(key not in plugins or plugins[key] is not value for key, value in PLUGIN_FLAGS.items())
    if settings_changed:
        settings["enabledPlugins"] = {**plugins, **PLUGIN_FLAGS}
    new_settings = json_bytes(settings)
    custom_files = []
    if destination.exists():
        actual_hashes = file_hashes(destination)
        expected_hashes = previous.get("package_hashes") if previous and previous["status"] == "installed" else None
        if expected_hashes is None:
            warnings.append("Existing package has no managed hash receipt; its complete contents will be backed up before replacement.")
            custom_files = sorted(actual_hashes)
        else:
            custom_files = sorted(key for key in actual_hashes.keys() | expected_hashes.keys() if actual_hashes.get(key) != expected_hashes.get(key))
        if custom_files:
            warnings.append("Local package changes detected and backed up before update: " + ", ".join(custom_files))

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
        hashes = file_hashes(staged)
        receipt = {"owner": RECEIPT_OWNER, "schema_version": 2, "status": "installed", "source": str(source), "destination": str(destination), "installer_python": sys.executable, "plugin_identity": IDENTITY, "plugin_version": manifest["version"], "plugin_flags": owned, "package_hashes": hashes}
        atomic_write(staged / ".install-info.json", json_bytes(receipt))
        new_receipt = json_bytes(receipt)
        package_changed = not same_tree(staged, destination)
        receipt_changed = old_receipt != new_receipt
        changes = package_changed or settings_changed or claude_changed or receipt_changed
        if changes:
            backup = make_backup(claude_home, {"settings.json": old_settings, "CLAUDE.md": old_claude if claude_changed else None, RECEIPT_NAME: old_receipt}, destination, package_changed)
            backup_metadata(backup, claude_home, successful=False)
            mutations = []
            if settings_changed:
                mutations.append((settings_path, old_settings, new_settings))
            if claude_changed:
                mutations.append((claude_path, old_claude, new_claude))
            if receipt_changed:
                mutations.append((receipt_path, old_receipt, new_receipt))
            warnings.extend(commit_transaction(destination, staged, package_changed, mutations))
            warnings.extend(finish_backup(backup, claude_home, keep_backups))
    return {"destination": str(destination), "backup": str(backup) if backup else None, "changed": bool(changes), "installer_python": sys.executable, "ready": not skip_doctor, "status": "ready" if not skip_doctor else "installed_but_not_ready", "doctor": doctor, "warnings": warnings, "local_changes": custom_files}


def package_is_ours(destination: Path) -> bool:
    if not destination.exists():
        return False
    _, info = read_object(destination / ".install-info.json")
    if info.get("plugin_identity") == IDENTITY:
        return True
    _, manifest = read_object(destination / ".claude-plugin" / "plugin.json")
    return manifest.get("name") == "savvy-codex"


def uninstall(claude_home: Path, *, keep_backups: int = 5) -> dict:
    if type(keep_backups) is not int or keep_backups < 0:
        raise ValueError("keep_backups must be a nonnegative integer")
    claude_home = absolute(claude_home)
    destination, settings_path, claude_path, receipt_path = paths_for(claude_home)
    old_settings, settings = read_object(settings_path)
    plugins = settings_plugins(settings)
    old_receipt, receipt = read_receipt(receipt_path)
    old_claude = read_regular(claude_path)
    new_claude = without_workflow(old_claude)
    warnings, restored, skipped = [], [], []
    remove_package = package_is_ours(destination)
    if destination.exists() and not remove_package:
        warnings.append("Package path is not identified as Savvy Codex; preserved it. Disable/remove any custom installation manually.")
    previous_flags = receipt.get("plugin_flags", {}) if receipt and receipt["status"] == "installed" else {}
    updated_plugins = dict(plugins)
    for key, owned in previous_flags.items():
        if key not in plugins or plugins[key] is not owned["after"]:
            skipped.append(key)
            continue
        if owned["before_exists"]:
            updated_plugins[key] = owned["before"]
        else:
            updated_plugins.pop(key, None)
        restored.append(key)
    # Without owned history, only disable this plugin. Never guess the old
    # Savvy flag values or overwrite a later user edit after an uninstall.
    if not receipt or receipt["status"] == "installed" and IDENTITY not in previous_flags:
        if plugins.get(IDENTITY) is True:
            updated_plugins[IDENTITY] = False
        warnings.append("No owned previous value for this plugin flag; conservatively disabled Savvy Codex. Re-enable the original Savvy plugins manually if wanted.")
    if skipped:
        warnings.append("Preserved plugin flags changed since installation: " + ", ".join(skipped))
    # JSON distinguishes 0/false and 1/true, whereas Python dict equality does
    # not. Preserve even unusual pre-install values exactly when restoring.
    settings_changed = json_bytes(updated_plugins) != json_bytes(plugins)
    if settings_changed:
        settings["enabledPlugins"] = updated_plugins
    claude_changed = old_claude != new_claude
    completed = {**receipt, "status": "uninstalled"} if receipt else None
    new_receipt = json_bytes(completed) if completed else None
    receipt_changed = old_receipt != new_receipt
    changes = remove_package or settings_changed or claude_changed or receipt_changed
    backup = None
    if changes:
        claude_home.mkdir(parents=True, exist_ok=True)
        backup = make_backup(claude_home, {"settings.json": old_settings, "CLAUDE.md": old_claude if claude_changed else None, RECEIPT_NAME: old_receipt}, destination, remove_package)
        backup_metadata(backup, claude_home, successful=False)
        mutations = []
        if settings_changed:
            mutations.append((settings_path, old_settings, json_bytes(settings)))
        if claude_changed:
            mutations.append((claude_path, old_claude, new_claude))
        if receipt_changed:
            mutations.append((receipt_path, old_receipt, new_receipt))
        warnings.extend(commit_transaction(destination, None, remove_package, mutations))
        warnings.extend(finish_backup(backup, claude_home, keep_backups))
    return {"destination": str(destination), "backup": str(backup) if backup else None, "changed": bool(changes), "status": "uninstalled", "warnings": warnings, "restored_flags": restored, "preserved_flags": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).absolute().parents[1], help="Local plugin checkout (default: this repository)")
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude", help="Claude configuration directory")
    parser.add_argument("--enable-default-workflow", action="store_true", help="Deprecated compatibility option; removes old global workflow block. Select auto in plugin settings.")
    parser.add_argument("--skip-doctor", action="store_true", help="Explicit offline/test installation; readiness stays unverified. Never used by default.")
    parser.add_argument("--uninstall", action="store_true", help="Back up and remove this package, restore owned plugin flags, keep run records/preferences")
    parser.add_argument("--keep-backups", type=int, default=5, help="Keep this many successful managed backups (default: 5); failed and unknown backups remain")
    args = parser.parse_args(argv)
    try:
        result = uninstall(args.claude_home, keep_backups=args.keep_backups) if args.uninstall else install(args.source, args.claude_home, args.enable_default_workflow, skip_doctor=args.skip_doctor, keep_backups=args.keep_backups)
    except NotReadyError as error:
        status = "Installed but not ready; update refused" if error.installed else "Not installed: not ready"
        print(f"{status}. {error}", file=sys.stderr)
        print("No installation or global settings changes were made. Doctor does not run a model.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"{'Uninstall' if args.uninstall else 'Installation'} failed: {error}", file=sys.stderr)
        return 1
    if args.uninstall:
        print(("Uninstalled: " if result["changed"] else "Already uninstalled: ") + result["destination"])
    elif result["ready"]:
        print(("Installed and ready: " if result["changed"] else "Already up to date and ready: ") + result["destination"])
    else:
        print("Installed but not ready (doctor skipped): " + result["destination"])
    if result["backup"]:
        print("Original files backed up: " + result["backup"])
    for warning in result["warnings"]:
        print("Note: " + warning)
    if not args.uninstall:
        print("Installer Python: " + result["installer_python"])
        print("Restart Claude Code to load savvy-codex@skills-dir. Plugin mode defaults to manual; preferences are controlled by plugin settings.")
    else:
        print("Run records, user preferences, and backups were preserved. Restart Claude Code to unload the plugin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
