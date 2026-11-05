"""Controller-side allowlist snapshots and patch delivery. No model calls.

This is an input/delivery boundary, not a per-file OS sandbox. The Codex host
sandbox is responsible for denying writes outside the execution workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


NOTICE = ("Only authorized ordinary files are staged in this snapshot. Scope is "
          "checked again against actual changes before patch export. This is not "
          "per-file OS isolation: access to system files and paths outside the "
          "snapshot depends on the host Codex sandbox. No patch is applied to the source workspace.")
POLICY = json.loads((Path(__file__).resolve().parents[3] / "shared" / "path-policy.json").read_text(encoding="utf-8"))
DEPENDENCY_NAMES = set(POLICY["dependency_directory_names"])


class SnapshotError(Exception):
    pass


def is_link(path):
    """Also reject Windows junctions/reparse points; never follow them."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def sensitive(relative):
    parts = [part.casefold() for part in Path(relative).parts]
    return any(part in POLICY["denied_path_parts"] or part in POLICY["denied_exact_names"]
               or any(part.startswith(prefix) for prefix in POLICY["denied_name_prefixes"])
               or any(part.endswith(extension) for extension in POLICY["denied_extensions"])
               for part in parts)


def dependency_path(relative):
    return any(part.casefold() in DEPENDENCY_NAMES for part in Path(relative).parts)


def contains(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ordinary_path(path, root):
    """Inspect components before resolve(), which would conceal a symlink."""
    if not contains(path, root):
        raise SnapshotError("Scope path escapes the source workspace.")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if is_link(current):
                raise SnapshotError(f"Symbolic links and reparse points cannot be staged: {path.relative_to(root).as_posix()}")


def inventory(root):
    files = {}
    hazards = []
    def visit(directory):
        for item in sorted(directory.iterdir()):
            relative = item.relative_to(root).as_posix()
            if is_link(item):
                hazards.append(relative)
            elif item.is_dir():
                visit(item)
            elif item.is_file():
                files[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
            else:
                hazards.append(relative)
    visit(root)
    return files, hazards


def directory_inventory(root):
    return sorted((Path(base) / name).relative_to(root).as_posix()
                  for base, dirs, _ in os.walk(root, followlinks=False) for name in dirs)


def git_command(workspace, git_dir, *args):
    git = shutil.which("git.exe") or shutil.which("git")
    if not git:
        raise SnapshotError("Git is required to produce a reviewable snapshot patch; install Git before creating a new attempt.")
    env = dict(os.environ)
    # User Git aliases, hooks, diff drivers and credential helpers are not needed.
    for key in list(env):
        if key.upper().startswith("GIT_"):
            env.pop(key)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    command = [git, "-c", "core.autocrlf=false", "-c", "core.filemode=false", "-c", "core.hooksPath=" + str(git_dir / "no-hooks"),
               "-c", "commit.gpgsign=false", "-c", "user.name=Scoped Worker", "-c", "user.email=worker@localhost",
               "--git-dir=" + str(git_dir), "--work-tree=" + str(workspace), *args]
    result = subprocess.run(command, cwd=workspace, env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    if result.returncode:
        raise SnapshotError("Snapshot Git operation failed: " + result.stderr.decode("utf-8", "replace").strip()[:600])
    return result.stdout


def prepare(source, scopes, run_dir, raw_scopes=(), evidence_files=None, context_paths=(), dependency_paths=(), verification_output_paths=()):
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=False)
    entries, omitted = [], []
    # Store the baseline outside the model's cwd; no source .git is exposed.
    git_dir = run_dir / "baseline.git"
    template = run_dir / "empty-git-template"
    template.mkdir()
    for index, scope in enumerate(scopes):
        relative = scope.relative_to(source)
        if str(relative) == ".":
            raise SnapshotError("Root scope '.' is not allowed; list the authorized files or subdirectories explicitly.")
        ordinary_path(scope, source)
        if sensitive(relative) or dependency_path(relative):
            raise SnapshotError(f"Sensitive or runtime scope cannot be staged: {relative.as_posix()}")
        if contains(scope, run_dir) or contains(run_dir, scope):
            raise SnapshotError("Runtime job storage cannot be included in an authorized scope.")
        raw = next((value for value in raw_scopes if Path(os.path.abspath(Path(value).expanduser() if Path(value).expanduser().is_absolute() else source / value)) == scope), "")
        directory = scope.is_dir() or (not scope.exists() and str(raw).endswith(("/", "\\")))
        entries.append({"path": relative.as_posix(), "type": "directory" if directory else "file"})
        if directory:
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        def stage(item):
            rel = item.relative_to(source)
            ordinary_path(item, source)
            if sensitive(rel) or dependency_path(rel) or contains(item, run_dir):
                omitted.append(rel.as_posix())
                return
            target = workspace / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                for child in sorted(item.iterdir()):
                    stage(child)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target, follow_symlinks=False)
            elif item.exists():
                raise SnapshotError(f"Only ordinary files can be staged: {rel.as_posix()}")
        if scope.exists():
            stage(scope)
    readonly = set()
    dependency_stats = {"files": 0, "bytes": 0, "strategy": "ordinary-file-copy; links rejected"}
    implicit = set()
    for entry in entries:
        parent = Path(entry["path"]) if entry["type"] == "directory" else Path(entry["path"]).parent
        while True:
            for name in ("AGENTS.md", "CLAUDE.md"):
                rel = parent / name
                if (source / rel).is_file() and not any(entry["type"] == "file" and entry["path"] == rel.as_posix() for entry in entries):
                    implicit.add(rel.as_posix())
            if parent == Path("."):
                break
            parent = parent.parent
    # Directory authorization never implicitly authorizes rewriting project
    # instructions already copied within that directory.
    for rel in inventory(workspace)[0]:
        if Path(rel).name in {"AGENTS.md", "CLAUDE.md"} and not any(entry["type"] == "file" and entry["path"] == rel for entry in entries):
            implicit.add(rel)

    def stage_readonly(item, dependency=False):
        ordinary_path(item, source)
        rel = item.relative_to(source)
        if sensitive(rel) or contains(item, run_dir):
            raise SnapshotError("Sensitive context/dependency input cannot be staged: " + rel.as_posix())
        if dependency_path(rel) and not dependency:
            raise SnapshotError("Dependency directories require explicit dependency_paths: " + rel.as_posix())
        if item.is_dir():
            (workspace / rel).mkdir(parents=True, exist_ok=True)
            for child in sorted(item.iterdir()):
                stage_readonly(child, dependency)
        elif item.is_file():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target, follow_symlinks=False)
            readonly.add(rel.as_posix())
            if dependency:
                dependency_stats["files"] += 1
                dependency_stats["bytes"] += item.stat().st_size
        else:
            raise SnapshotError("Missing or nonordinary read-only input: " + rel.as_posix())

    readonly_entries = []
    for value in list(context_paths) + list(dependency_paths):
        rel = Path(value)
        item = source / rel
        ordinary_path(item, source)
        if item == source:
            raise SnapshotError("Root read-only input is not allowed; list explicit project inputs.")
        descriptor = {"path": rel.as_posix(), "type": "directory" if item.is_dir() else "file"}
        if any(authorized(rel, [entry]) or authorized(entry["path"], [descriptor]) for entry in entries):
            raise SnapshotError("Explicit read-only inputs must not overlap write scope: " + str(value))
        readonly_entries.append(descriptor)
    for value in list(context_paths) + sorted(implicit):
        stage_readonly(source / value)
    for value in dependency_paths:
        stage_readonly(source / value, True)
    output_entries = []
    for value in verification_output_paths:
        rel = Path(value)
        target = source / rel
        ordinary_path(target, source)
        output = {"path": rel.as_posix(), "type": "directory" if str(value).endswith(("/", "\\")) else "file"}
        if sensitive(rel) or target.exists() or any(authorized(key, [output]) for key in readonly):
            raise SnapshotError("Verification outputs must be new nonsensitive paths separate from read-only inputs: " + str(value))
        if any(authorized(rel, [entry]) or contains(Path(entry["path"]), rel) for entry in entries):
            raise SnapshotError("Verification outputs must be separate from write scope: " + str(value))
        output_entries.append(output)
    evidence_paths = []
    for relative, original in (evidence_files or {}).items():
        target = workspace / relative
        if not contains(target, workspace) or target.exists():
            raise SnapshotError("Controller review evidence path conflicts with the authorized snapshot.")
        if is_link(original) or not original.is_file():
            raise SnapshotError("Controller review evidence must be an ordinary file.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target, follow_symlinks=False)
        evidence_paths.append(Path(relative).as_posix())
    baseline, hazards = inventory(workspace)
    if hazards:
        raise SnapshotError("Snapshot contains unsafe paths: " + ", ".join(hazards))
    git_command(workspace, git_dir, "init", "--quiet", "--template=" + str(template))
    git_command(workspace, git_dir, "add", "--all", "--force", "--", ".")
    git_command(workspace, git_dir, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", "Snapshot of authorized current inputs")
    commit = git_command(workspace, git_dir, "rev-parse", "HEAD").decode().strip()
    return {"path": str(workspace), "strategy": "scope_snapshot", "source_workspace": str(source),
            "scope_paths": [entry["path"] for entry in entries], "scope_entries": entries,
            "omitted_paths": omitted, "notice": NOTICE, "baseline": baseline, "baseline_commit": commit,
            "baseline_directories": directory_inventory(workspace), "git_dir": str(git_dir),
            "read_only_evidence_paths": evidence_paths, "read_only_paths": sorted(readonly | set(evidence_paths)),
            "context_paths": list(context_paths), "implicit_context_paths": sorted(implicit),
            "dependency_paths": list(dependency_paths), "dependency_copy": dependency_stats,
            "read_only_entries": readonly_entries,
            "verification_output_entries": output_entries}


def authorized(relative, entries):
    path = Path(relative)
    return any(path == Path(entry["path"]) or (entry["type"] == "directory" and contains(path, Path(entry["path"])))
               for entry in entries)


def inspect_changes(snapshot):
    current, hazards = inventory(Path(snapshot["path"]))
    baseline = snapshot["baseline"]
    changed = sorted(key for key in set(baseline) | set(current) if baseline.get(key) != current.get(key))
    entries = snapshot["scope_entries"] + snapshot.get("verification_output_entries", [])
    readonly = set(snapshot.get("read_only_paths", []))
    rejected = sorted(set(hazards) | {key for key in changed if key in readonly or authorized(key, snapshot.get("read_only_entries", [])) or not authorized(key, entries) or sensitive(key)})
    directory_delta = set(snapshot["baseline_directories"]) ^ set(directory_inventory(Path(snapshot["path"])))
    for key in directory_delta:
        scaffold = any(contains(Path(entry["path"]), Path(key)) for entry in entries)
        if sensitive(key) or not (authorized(key, entries) or scaffold):
            rejected.append(key + "/")
    changes = [{"path": key, "change": "added" if key not in baseline else "deleted" if key not in current else "modified",
                "sha256_before": baseline.get(key), "sha256_after": current.get(key)}
               for key in changed if not authorized(key, snapshot.get("verification_output_entries", []))]
    return changes, sorted(set(rejected))


def export(snapshot, run_dir, changes):
    """Call only after actual delta and model evidence passed validation."""
    workspace, git_dir = Path(snapshot["path"]), Path(snapshot["git_dir"])
    actual, rejected = inspect_changes(snapshot)
    if rejected or actual != changes:
        raise SnapshotError("Snapshot changed after validation; no patch was exported.")
    paths = [entry["path"] for entry in changes]
    if not paths or any(not authorized(path, snapshot["scope_entries"]) or sensitive(path) for path in paths):
        raise SnapshotError("Patch export requires an exact validated authorized change list.")
    # Rebuild the index from the immutable baseline, then stage only literal
    # validated paths. A late background write elsewhere can never enter it.
    git_command(workspace, git_dir, "read-tree", snapshot["baseline_commit"])
    git_command(workspace, git_dir, "--literal-pathspecs", "add", "--all", "--force", "--", *paths)
    indexed_paths = git_command(workspace, git_dir, "diff", "--cached", "--name-only", "-z", "--no-renames", snapshot["baseline_commit"], "--").decode("utf-8").split("\0")
    indexed_paths = {path for path in indexed_paths if path}
    if indexed_paths != set(paths):
        raise SnapshotError("Final patch index differs from the validated change list; no patch was exported.")
    index = {}
    for record in git_command(workspace, git_dir, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            header, path = record.split(b"\t", 1)
            mode, object_id, stage = header.decode().split()
            index[path.decode("utf-8")] = (mode, object_id, stage)
    for entry in changes:
        if entry["change"] == "deleted":
            if entry["path"] in index:
                raise SnapshotError("A deleted path unexpectedly remains in the patch index.")
            continue
        mode, object_id, stage = index[entry["path"]]
        if mode not in {"100644", "100755"} or stage != "0":
            raise SnapshotError("Patch index contains an unsafe file type; no patch was exported.")
        blob = git_command(workspace, git_dir, "cat-file", "blob", object_id)
        if hashlib.sha256(blob).hexdigest() != entry["sha256_after"]:
            raise SnapshotError("File contents changed while freezing the patch; no patch was exported.")
    final, rejected = inspect_changes(snapshot)
    if rejected or final != changes:
        raise SnapshotError("Snapshot changed while freezing the patch; no patch was exported.")
    patch = git_command(workspace, git_dir, "diff", "--cached", "--binary", "--full-index", "--no-renames", "--no-ext-diff", "--no-textconv", snapshot["baseline_commit"], "--")
    patch_path = run_dir / "changes.patch"
    patch_path.write_bytes(patch)
    return patch_path
