#!/usr/bin/env python3
"""Delegate one bounded task to the installed official Codex CLI.

Standard library only. No authentication material is read or copied. The installed
CLI retains its own configuration, login, Windows sandbox and execpolicy rules.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import scope_snapshot


VERSION = 2
TERMINAL_STATUSES = {"complete", "blocked", "failed", "cancelled", "timeout"}
READ_KINDS = {"inspect", "review", "plan"}
DISABLED_FEATURES = (
    "plugins", "apps", "hooks", "multi_agent", "multi_agent_v2", "skill_search",
)
REQUIRED_FEATURES = set(DISABLED_FEATURES) | {"skip_host_skill_discovery", "shell_tool"}
RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["complete", "blocked"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "changed_files", "checks", "findings", "limitations"],
}
DENIED = re.compile(
    r"permission denied|access (?:is )?denied|operation not permitted|拒绝访问|"
    r"(?:approval|permission|sandbox|tool)[^\n]{0,60}(?:denied|rejected|blocked|unavailable)|"
    r"(?:denied|rejected|blocked)[^\n]{0,60}(?:approval|permission|sandbox|tool)",
    re.IGNORECASE,
)


class WorkerError(Exception):
    def __init__(self, message, failure_kind=None):
        super().__init__(message)
        self.failure_kind = failure_kind


def failure_fields(message, kind=None):
    """Actionable categories; retryable never means an automatic retry."""
    value = str(message)
    rules = [
        ("quota", r"quota|rate.?limit|usage limit|insufficient_quota|credits|429"),
        ("unsupported_model", r"unsupported.*(?:model|effort)|(?:model|effort).*unsupported|absent from.*(?:cache|catalog)|model_not_found"),
        ("auth", r"\bauth(?:entication)?\b|\boauth\b|log(?:ged)?[ -]?in|ChatGPT.*(?:verified|verify)|401|403"),
        ("timeout", r"timed? out|timeout"),
        ("permission", r"permission|access.*denied|sandbox|approval|拒绝访问"),
        ("validation", r"scope|schema|packet|invalid|symlink|symbolic link|reparse|sensitive|evidence|could not be verified|final.*response"),
        ("unsupported_cli", r"missing required|CLI.*(?:not found|support)|official.*(?:CLI|program)|required guarded"),
        ("interrupted", r"interrupt|controller exited|cancel"),
    ]
    kind = kind or next((name for name, pattern in rules if re.search(pattern, value, re.I)), "execution")
    actions = {
        "quota": "Wait for account quota to reset, then explicitly create a new attempt with parent_job; do not switch models silently.",
        "unsupported_model": "Refresh the account catalog and explicitly select an available model and effort for a new attempt.",
        "auth": "Verify official Codex ChatGPT login in the primary session, then create a new attempt.",
        "timeout": "Inspect saved evidence and choose an explicit new attempt or timeout; the worker will not retry automatically.",
        "permission": "Inspect the exact denial and use already authorized host tools or obtain the required permission before a new attempt.",
        "validation": "Inspect the task scope and saved evidence, correct the cause, then explicitly create a new attempt.",
        "unsupported_cli": "Update or repair the official Codex CLI and run doctor again; no model was substituted.",
        "interrupted": "Inspect saved logs and process status before explicitly starting another attempt.",
        "execution": "Inspect saved logs and correct the failure before explicitly starting a new attempt.",
    }
    return {"failure_kind": kind, "next_action": actions.get(kind, actions["execution"]), "retryable": kind in {"quota", "timeout", "execution"}}


def clear_failure():
    return {"failure_kind": None, "next_action": None, "retryable": False}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def codex_home():
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def child_environment(environ=None, proxies=None):
    """Remove provider overrides; retain HOME/CODEX_HOME and system networking."""
    env = dict(os.environ if environ is None else environ)
    prefixes = (
        "OPENAI_", "AZURE_OPENAI_", "ANTHROPIC_", "CODEX_API_", "CODEX_MODEL_",
        "CODEX_OSS_", "OPENROUTER_", "LITELLM_", "DEEPSEEK_", "LMSTUDIO_",
    )
    exact = {"CODEX_BASE_URL", "CODEX_MODEL", "CODEX_REASONING_EFFORT", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OLLAMA_HOST"}
    for key in list(env):
        upper = key.upper()
        credential_variable = (upper.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY", "_ACCESS_KEY", "_CREDENTIALS"))
                               or upper in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "SSH_AUTH_SOCK"})
        if upper in exact or upper.startswith(prefixes) or credential_variable:
            del env[key]
    proxy_values = urllib.request.getproxies() if proxies is None else proxies
    for key, value in proxy_values.items():
        name = {"http": "HTTP_PROXY", "https": "HTTPS_PROXY", "all": "ALL_PROXY", "no": "NO_PROXY"}.get(key.lower())
        if name and value and not any(k.upper() == name for k in env):
            env[name] = value
    env["CLAUDE_CODEX_WORKER"] = "1"
    return env


def discover_cli():
    """Honor the user's PATH entry; never execute .cmd/.ps1 through a shell.

    A desktop-bundled codex.exe later on PATH must not override the user's
    explicitly installed global @openai/codex shim. The official shim also
    retains its own package/runtime discovery when launching its native binary.
    """
    native = shutil.which("codex.exe")
    shim = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.ps1")
    if not shim:
        if native:
            return [str(Path(native).resolve())]
        raise WorkerError("Official Codex CLI was not found on PATH; install/sign in separately.")
    path = Path(shim).resolve()
    if path.suffix.lower() == ".exe":
        return [str(path)]
    candidates = [
        path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
        path.parent / ".." / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
    ]
    if path.name == "codex.js":
        candidates.insert(0, path)
    node = shutil.which("node.exe") or shutil.which("node")
    for candidate in candidates:
        if candidate.is_file() and node:
            return [str(Path(node).resolve()), str(candidate.resolve())]
    if os.name != "nt" and path.suffix.lower() not in {".cmd", ".ps1", ".bat"}:
        return [str(path)]
    if native:
        return [str(Path(native).resolve())]
    raise WorkerError("Codex shim found, but its official @openai/codex JavaScript entry or Node is missing.")


def capture(argv, env, cwd=None, timeout=30):
    process = None
    try:
        process = subprocess.Popen(
            argv, shell=False, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", start_new_session=os.name != "nt",
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        if process:
            kill_child_tree(process)
        raise WorkerError("CLI preflight command timed out.") from exc
    except OSError as exc:
        raise WorkerError(f"Could not start official Codex CLI: {exc}") from exc
    except BaseException:
        if process:
            kill_child_tree(process)
        raise


def check_auth(cli, env):
    result = capture(cli + ["login", "status"], env)
    message = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or not re.search(r"logged in using chatgpt\b", message, re.I):
        raise WorkerError("Codex login status did not verify 'Logged in using ChatGPT'; no task was submitted.")
    return {"verified": True, "method": "ChatGPT", "checked_at": now()}


def capabilities(cli, env):
    result = capture(cli + ["--version"], env)
    version = result.stdout.strip()
    if result.returncode != 0 or not re.match(r"codex(?:-cli)?\s+\d", version, re.I):
        raise WorkerError("The discovered program did not identify itself as the official Codex CLI.")
    result = capture(cli + ["features", "list"], env)
    if result.returncode != 0:
        raise WorkerError("Codex feature discovery failed; preserve the existing config and inspect doctor output.")
    available = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
    missing = REQUIRED_FEATURES - available
    if missing:
        raise WorkerError("Installed Codex is missing required isolation features: " + ", ".join(sorted(missing)))
    result = capture(cli + ["exec", "--help"], env)
    required = ("--approve-for-me", "--output-schema", "--json", "--output-last-message")
    if result.returncode != 0 or any(flag not in result.stdout for flag in required):
        raise WorkerError("Installed Codex exec does not support the required guarded worker options.")
    return {"version": version, "features_verified": sorted(REQUIRED_FEATURES)}


def overrides():
    args = ["-c", 'model_provider="openai"']
    for feature in DISABLED_FEATURES:
        args += ["--disable", feature]
    args += ["--enable", "skip_host_skill_discovery"]
    # shell_tool is deliberately retained. Unknown omit_skill_descriptions is
    # deliberately not set; it is absent from the verified installed CLI.
    return args


def normalize_catalog(raw, source):
    records = raw.get("models", raw.get("data", [])) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise WorkerError("Official model catalog has an unsupported shape.")
    models = []
    for record in records:
        if not isinstance(record, dict):
            continue
        model_id = record.get("slug") or record.get("id")
        levels = record.get("supported_reasoning_levels", record.get("supported_reasoning_efforts", []))
        efforts = [x.get("effort") if isinstance(x, dict) else x for x in levels]
        efforts = [x for x in efforts if isinstance(x, str) and x]
        if isinstance(model_id, str) and model_id and efforts:
            model = {"id": model_id, "efforts": efforts, "default_effort": record.get("default_reasoning_level")}
            for field in ("display_name", "description"):
                if isinstance(record.get(field), str) and record[field].strip():
                    model[field] = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", record[field]).strip()[:1000]
            models.append(model)
    if not models:
        raise WorkerError("Official model catalog contains no model/effort combinations.")
    return {
        "source": source, "fetched_at": raw.get("fetched_at") if isinstance(raw, dict) else None,
        "models": models,
    }


def cached_catalog():
    path = codex_home() / "models_cache.json"
    try:
        result = normalize_catalog(read_json(path), "local_account_cache")
    except (OSError, ValueError) as exc:
        raise WorkerError("No readable account model cache; explicitly run catalog --refresh first.") from exc
    result["path"] = str(path)
    result["note"] = "Cached availability; use catalog --refresh for an explicit network refresh."
    return result


def refresh_catalog(cli, env):
    check_auth(cli, env)
    result = capture(cli + overrides() + ["debug", "models"], env, timeout=90)
    if result.returncode != 0:
        raise WorkerError("Official model catalog refresh failed; no model call was submitted.")
    try:
        catalog = normalize_catalog(json.loads(result.stdout), "official_cli_refresh")
    except ValueError as exc:
        raise WorkerError("Official CLI returned an invalid model catalog.") from exc
    if re.search(r"(?:failed|unable|could not)[^\n]{0,60}(?:refresh|fetch)|using (?:the )?(?:cached|bundled)", result.stderr, re.I):
        catalog["source"] = "official_cli_fallback"
        catalog["note"] = "The CLI reported a refresh limitation; availability may be cached."
    return catalog


def verify_model(catalog, model, effort):
    match = next((m for m in catalog["models"] if m["id"] == model), None)
    if not match:
        raise WorkerError(f"Model {model!r} is absent from the official account cache; explicitly refresh the catalog.")
    if effort not in match["efforts"]:
        raise WorkerError(f"Effort {effort!r} is unsupported by {model!r}; supported: {', '.join(match['efforts'])}.")


def workspace_path(value):
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise WorkerError("Workspace must be an existing directory.")
    return path


def within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def normalize_scope(workspace, values):
    if not isinstance(values, list) or any(not isinstance(p, str) or not p.strip() for p in values):
        raise WorkerError("scope_paths must be a list of nonempty literal paths, without glob patterns.")
    paths = []
    for value in values:
        if any(character in value for character in "*?\x00"):
            raise WorkerError("scope_paths must contain literal paths, without glob patterns or NUL.")
        path = Path(value).expanduser()
        path = Path(os.path.abspath(path if path.is_absolute() else workspace / path))
        if not within(path, workspace):
            raise WorkerError(f"Scope path escapes the workspace: {value}")
        if path == workspace:
            raise WorkerError("Root scope '.' is not allowed; authorize explicit files or subdirectories.", "validation")
        try:
            scope_snapshot.ordinary_path(path, workspace)
        except scope_snapshot.SnapshotError as exc:
            raise WorkerError(str(exc), "validation") from exc
        if scope_snapshot.sensitive(path.relative_to(workspace)):
            raise WorkerError(f"Sensitive or runtime scope is not allowed: {value}", "validation")
        if path not in paths:
            paths.append(path)
    return paths


def validate_packet(packet, workspace, permission=None, approval=None):
    if not isinstance(packet, dict) or packet.get("version", 1) != 1:
        raise WorkerError("Task packet must be a JSON object with version 1.")
    for name in ("goal", "reason"):
        if not isinstance(packet.get(name), str) or not packet[name].strip():
            raise WorkerError(f"Task packet requires a nonempty {name} string.")
    kind = packet.get("kind")
    if kind not in READ_KINDS | {"implement"}:
        raise WorkerError("kind must be inspect, review, plan, or implement.")
    acceptance = packet.get("acceptance")
    if isinstance(acceptance, str):
        valid_acceptance = bool(acceptance.strip())
    else:
        valid_acceptance = isinstance(acceptance, list) and bool(acceptance) and all(isinstance(x, str) and x.strip() for x in acceptance)
    if not valid_acceptance:
        raise WorkerError("acceptance must be a nonempty string or list of strings.")
    scopes = normalize_scope(workspace, packet.get("scope_paths"))
    if not scopes:
        raise WorkerError("Task requires at least one explicit authorized scope path.", "validation")
    for field in ("parent_job", "review_of"):
        if packet.get(field) is not None and (not isinstance(packet[field], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", packet[field])):
            raise WorkerError(f"{field} must be a saved job id.", "validation")
    if packet.get("review_of") and kind != "review":
        raise WorkerError("review_of requires kind=review and read-only execution.", "validation")
    if packet.get("author_provider", "codex") not in {"codex", "claude"}:
        raise WorkerError("author_provider must be codex or claude.", "validation")
    if "attempt" in packet and (not isinstance(packet["attempt"], int) or isinstance(packet["attempt"], bool) or packet["attempt"] < 1):
        raise WorkerError("attempt must be a positive integer.", "validation")
    for name in ("tests", "required_tools"):
        if name in packet and (not isinstance(packet[name], list) or any(not isinstance(x, str) or not x.strip() for x in packet[name])):
            raise WorkerError(f"{name} must be a list of nonempty strings.")
    if kind == "implement":
        authorization = packet.get("authorization", {})
        if not isinstance(authorization, dict) or authorization.get("write_intent") is not True or not scopes:
            raise WorkerError("implement requires scope_paths and authorization.write_intent=true from the human's task.")
        effective = "workspace-write"
    else:
        effective = "read-only"
    if permission and permission != effective:
        raise WorkerError(f"{kind} requires --permission {effective}.")
    expected_approval = "auto-review" if effective == "workspace-write" else "never"
    if approval and approval != expected_approval:
        raise WorkerError(f"{effective} requires --approval {expected_approval}; permission escalation will not be attempted.")
    return scopes, effective, expected_approval


class WorkspaceLock:
    """One OS lock per normalized workspace, released even after process failure."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            # A second Windows owner cannot even read a byte locked by the
            # first owner. fstat observes size without touching that byte.
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise WorkerError("Another worker owns this workspace lock; use status instead of resubmitting.") from exc
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def state_directory(workspace, state_root=None):
    root = Path(state_root).expanduser().resolve() if state_root else Path.home() / ".claude" / "codex-worker-runs"
    digest = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()[:20]
    return root / digest


def new_job_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


def initialize_run(args, queued=False):
    """Create a run once; a background run may claim its queued directory once."""
    workspace = Path(args.workspace).expanduser().resolve()
    directory = state_directory(workspace, args.state_root)
    job_id = getattr(args, "job_id", None) or new_job_id()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise WorkerError("Invalid job id.")
    run_dir = directory / job_id
    if run_dir.exists():
        if queued or not getattr(args, "job_id", None):
            raise WorkerError("Job id already exists; existing artifacts were preserved.")
        saved = read_json(run_dir / "state.json")
        if saved.get("status") != "queued" or saved.get("workspace") != str(workspace) or saved.get("job_id") != job_id:
            raise WorkerError("Job id is already claimed or does not match this workspace.")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
    if not queued:
        try:
            with (run_dir / "run.claim").open("x", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except FileExistsError as exc:
            raise WorkerError("Job id is already claimed; existing artifacts were preserved.") from exc
    names = {"events": "stdout.jsonl", "stderr": "stderr.log", "packet": "packet.json", "schema": "response.schema.json",
             "model_response": "model-response.json", "final": "final.json", "state": "state.json", "report": "report.md",
             "worker_stdout": "worker-stdout.log", "worker_stderr": "worker-stderr.log"}
    artifacts = {key: str(run_dir / value) for key, value in names.items()}
    artifacts.update(patch=None, patch_path=None, changes=None, changes_path=None)
    preview = {}
    try:
        preview = read_json(args.packet)
        if not isinstance(preview, dict):
            preview = {}
    except (OSError, ValueError):
        pass
    metadata = {"kind": preview.get("kind"), "author_provider": preview.get("author_provider", "codex"), "execution_provider": "codex",
                "parent_job": getattr(args, "parent_job", None) or preview.get("parent_job"),
                "review_of": getattr(args, "review_of", None) or preview.get("review_of"),
                "attempt": getattr(args, "attempt", None) or preview.get("attempt", 1),
                "scope_paths": preview.get("scope_paths", []), "execution_workspace": None,
                "task_prompt": preview.get("goal"),
                "task_packet": {key: preview[key] for key in ("goal", "reason", "context", "acceptance", "tests", "scope_paths", "kind", "author_provider", "review_of") if key in preview},
                **clear_failure()}
    state = {"version": VERSION, "id": job_id, "job_id": job_id, "workspace": str(workspace), "run_dir": str(run_dir),
             "status": "queued" if queued else "preparing", "started_at": now(), "finished_at": None,
             "controller_pid": None if queued else os.getpid(), "goal": preview.get("goal"), "task": preview.get("goal"),
             "model": args.model, "effort": args.effort, "selected_model": args.model, "selected_effort": args.effort,
             "reason": preview.get("reason"), "changed_files": [], "result_path": artifacts["final"],
             "summary": "Queued for the local worker." if queued else "Checking task, CLI and authentication.",
             "last_tool": None, "last_message": None, "progress": "queued" if queued else "preflight",
             "authentication": {"verified": False, "method": "ChatGPT"}, "artifacts": artifacts, **metadata}
    write_json(artifacts["state"], state)
    for key in ("events", "stderr"):
        Path(artifacts[key]).touch()
    return workspace, directory, state


def start_task(args):
    """Queue one detached Python worker; auth/model checks occur inside run."""
    workspace, directory, state = initialize_run(args, queued=True)
    artifacts = state["artifacts"]
    try:
        if os.environ.get("CLAUDE_CODEX_WORKER") == "1" or os.environ.get("MANIFOLD_COLLAB_WORKER") == "1":
            raise WorkerError("Recursive delegation is disabled in an existing worker session.")
        command = [sys.executable, str(Path(__file__).resolve()), "run", "--workspace", str(workspace),
                   "--packet", str(Path(args.packet).expanduser().resolve()), "--model", args.model,
                   "--effort", args.effort, "--timeout", str(args.timeout), "--job-id", state["job_id"]]
        if args.state_root:
            command += ["--state-root", str(Path(args.state_root).expanduser().resolve())]
        if args.permission:
            command += ["--permission", args.permission]
        if args.approval:
            command += ["--approval", args.approval]
        for flag in ("parent_job", "review_of", "attempt"):
            if getattr(args, flag, None) is not None:
                command += ["--" + flag.replace("_", "-"), str(getattr(args, flag))]
        with Path(artifacts["worker_stdout"]).open("wb") as stdout, Path(artifacts["worker_stderr"]).open("wb") as stderr:
            launch_env = dict(os.environ)
            launch_env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            process = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       env=launch_env, cwd=state["run_dir"],
                                       close_fds=True, start_new_session=os.name != "nt",
                                       creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0)
            # Sidecar avoids racing the child's own state.json updates.
            write_json(Path(state["run_dir"]) / "launch.json", {"pid": process.pid, "started_at": now()})
        # Do not rewrite queued state here: the child may already have advanced it.
        return {"status": "queued", "id": state["job_id"], "job_id": state["job_id"], "run_dir": state["run_dir"],
                "state_path": artifacts["state"], "report_path": artifacts["report"], "result_path": artifacts["final"], "model": args.model,
                "effort": args.effort, "goal": state["goal"], **{key: state.get(key) for key in metadata_fields()}}
    except (WorkerError, OSError) as exc:
        result = blocked_result(f"Could not launch background worker: {exc}", getattr(exc, "failure_kind", None))
        attach_result_metadata(state, result)
        state.update(status="blocked", finished_at=now(), updated_at=now(), result=result, summary=result["summary"], progress="blocked")
        write_json(artifacts["final"], result)
        write_json(artifacts["state"], state)
        write_report(state, result)
        return state


def build_command(cli, workspace, model, effort, permission, artifacts):
    command = cli + overrides() + ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    if permission == "read-only":
        command += ["-a", "never", "-s", "read-only"]
    # --approve-for-me conflicts with -a/-s, so it must stand alone.
    else:
        command += ["--approve-for-me"]
    command += [
        "exec", "--model", model, "--cd", str(workspace), "--skip-git-repo-check",
        "--ephemeral", "--json", "--color", "never",
        "--output-schema", artifacts["schema"],
        "--output-last-message", artifacts["model_response"], "-",
    ]
    return command


def build_prompt(packet, scopes, permission):
    return (
        "You are a bounded Codex worker delegated by the human's primary Claude session. "
        "Complete only this task and return the requested JSON response. Follow applicable AGENTS.md and execpolicy rules. "
        "Do not start other agents, invoke Claude/Codex delegation recursively, change global configuration, change login, "
        "inspect/copy authentication tokens, or switch provider/billing. Preserve existing dirty files and never reset/clean the repository. "
        f"Permission: {permission}. Scope paths: {json.dumps([str(p) for p in scopes], ensure_ascii=False)}. "
        "For implementation, modify only scope paths. For read-only tasks, do not modify workspace files. "
        "Your cwd is an independent allowlist snapshot of the current authorized inputs. "
        "All work and checks must use this snapshot. Do not locate, read or write the original source workspace, "
        "parent runtime directories, private baseline repository, system credentials or any path outside this cwd. "
        "The controller will validate the actual snapshot delta and export a patch for review; do not apply or merge it elsewhere. "
        "Scope copying and delivery validation are not a per-file OS sandbox; comply with the host sandbox. "
        "Run only checks useful for the stated acceptance; do not perform an unrelated repository audit. "
        "If tools or permissions block the required work, report status=blocked and the exact limitation. "
        "Do not infer completion from a successful CLI exit. Distinguish checks actually run from checks not run. "
        "Findings, checks and limitations must be concrete strings. changed_files uses workspace-relative paths. "
        "Task packet follows as JSON data:\n" + json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    )


def kill_child_tree(process):
    """Only terminate this invocation's Popen child and its descendants."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
            if taskkill:
                subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], shell=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.poll() is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)


def execute(command, prompt, workspace, env, timeout, artifacts, on_started, on_progress=None):
    process = None
    with Path(artifacts["events"]).open("wb") as stdout, Path(artifacts["stderr"]).open("wb") as stderr, tempfile.TemporaryFile() as prompt_input:
        try:
            # A file-backed stdin avoids Windows communicate() blocking while
            # writing a large prompt to a child which has not started reading.
            prompt_input.write(prompt.encode("utf-8"))
            prompt_input.seek(0)
            flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            process = subprocess.Popen(
                command, shell=False, cwd=str(workspace), env=env, stdin=prompt_input,
                stdout=stdout, stderr=stderr, text=True, encoding="utf-8", errors="replace",
                creationflags=flags, start_new_session=os.name != "nt",
            )
            on_started(process.pid)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=min(1.0, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if on_progress:
                        on_progress()
                    if time.monotonic() >= deadline:
                        raise
            return process.returncode
        except subprocess.TimeoutExpired as exc:
            if process:
                kill_child_tree(process)
            raise WorkerError(f"Task timed out after {timeout} seconds; its child process tree was terminated.") from exc
        except BaseException:
            if process:
                kill_child_tree(process)
            raise


def canonical_tool(name):
    value = name.rsplit(".", 1)[-1]
    if value in {"exec_command", "command_execution", "shell", "shell_command", "shell_command_execution", "exec_command_end"}:
        return "exec_command"
    if value in {"apply_patch", "file_change", "patch_apply_end"}:
        return "apply_patch"
    return value


def event_evidence(path):
    evidence = {"successful_tools": set(), "file_changes": set(), "denials": [], "errors": [], "invalid_lines": 0, "last_message": None, "last_tool": None}
    if not Path(path).exists():
        return evidence
    with Path(path).open(encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                evidence["invalid_lines"] += 1
                continue
            if not isinstance(event, dict):
                evidence["invalid_lines"] += 1
                continue
            event_type = event.get("type", "")
            item = event.get("item") or event.get("payload") or event
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            completed = event_type in {"item.completed", "tool.completed"} or item_type == "exec_command_end"
            if completed and item_type == "agent_message":
                evidence["last_message"] = item.get("text")
            if event_type in {"error", "turn.failed"}:
                evidence["errors"].append(str(event.get("message") or event.get("error") or "Codex turn failed")[:800])
            tool_name = item.get("tool") or item.get("name") or item_type
            if item_type in {"command_execution", "file_change", "mcp_tool_call", "tool_call"} or item.get("tool"):
                evidence["last_tool"] = canonical_tool(tool_name)
            output = str(item.get("aggregated_output") or item.get("output") or item.get("error") or "")
            denied = DENIED.search(output)
            if denied:
                evidence["denials"].append(denied.group(0)[:200])
            successful = completed and item.get("status", "completed") in {"completed", "success", "succeeded"}
            if canonical_tool(tool_name) == "exec_command":
                successful = successful and item.get("exit_code") == 0
            else:
                successful = successful and not item.get("error") and not (isinstance(item.get("result"), dict) and item["result"].get("isError"))
            if successful and not denied and item_type not in {"agent_message", "reasoning", "todo_list", "error", "warning"}:
                evidence["successful_tools"].add(canonical_tool(tool_name))
            if item_type in {"file_change", "patch_apply_end"}:
                changes = item.get("changes", [])
                if isinstance(changes, dict):
                    changes = [{"path": path} for path in changes]
                for change in changes:
                    if isinstance(change, dict) and isinstance(change.get("path"), str):
                        evidence["file_changes"].add(change["path"])
    return evidence


def validate_response(response):
    if not isinstance(response, dict) or set(response) != set(RESPONSE_SCHEMA["required"]):
        raise WorkerError("Codex final response did not match the required result schema.")
    if response["status"] not in {"complete", "blocked"} or not isinstance(response["summary"], str) or not response["summary"].strip():
        raise WorkerError("Codex returned an invalid status or empty summary.")
    for name in ("changed_files", "checks", "findings", "limitations"):
        if not isinstance(response[name], list) or any(not isinstance(x, str) or not x.strip() for x in response[name]):
            raise WorkerError(f"Codex final response has invalid {name}.")
    return response


def assess_result(response, evidence, packet, workspace, scopes, permission, returncode, stderr, scope_entries=None):
    response = validate_response(response)
    problems = []
    if returncode != 0:
        problems.append(f"Codex exited with status {returncode}.")
    if evidence["errors"]:
        problems.extend(evidence["errors"])
    if evidence["invalid_lines"]:
        problems.append(f"CLI stdout contained {evidence['invalid_lines']} invalid JSONL event(s).")
    denials = evidence["denials"] + [m.group(0)[:200] for m in DENIED.finditer(stderr)]
    if denials:
        problems.append("A permission/tool denial was recorded: " + "; ".join(dict.fromkeys(denials)))
    missing_tools = [x for x in packet.get("required_tools", []) if canonical_tool(x) not in evidence["successful_tools"]]
    if missing_tools:
        problems.append("No successful JSONL tool evidence for required_tools: " + ", ".join(missing_tools))
    if packet.get("kind") != "plan" and scopes and not evidence["successful_tools"]:
        problems.append("No successful tool evidence supports completion of the scoped task.")
    touched = set(response["changed_files"]) | evidence["file_changes"]
    if permission == "read-only" and touched:
        problems.append("A read-only task reported workspace file changes.")
    for value in sorted(touched):
        try:
            path = Path(value)
            path = (path if path.is_absolute() else workspace / path).resolve()
            allowed = scope_snapshot.authorized(path.relative_to(workspace), scope_entries) if scope_entries and within(path, workspace) else any(path == scope or (scope.is_dir() and within(path, scope)) for scope in scopes)
            if not within(path, workspace) or not allowed:
                problems.append(f"Changed path falls outside authorized scope: {value}")
        except (OSError, ValueError):
            problems.append(f"Invalid changed path: {value}")
    vague = response["summary"].strip().strip(".!。！").casefold() in {"done", "complete", "completed", "success", "finished", "完成", "已完成", "任务完成", "任务已完成"}
    if response["status"] == "complete" and (vague or not any(response[x] for x in ("checks", "findings", "changed_files"))):
        problems.append("The final message contains no concrete completion evidence.")
    if problems:
        response = dict(response)
        if response["status"] == "complete":
            response["summary"] = "Completion could not be verified: " + problems[0]
        response["status"] = "blocked"
        response["limitations"] = list(dict.fromkeys(response["limitations"] + problems))
    response.update(failure_fields("\n".join([response["summary"], *response["limitations"], stderr])) if response["status"] == "blocked" else clear_failure())
    return response


def write_report(state, result):
    lines = ["# Codex worker report", "", f"Status: {state['status']}", "",
             f"Model: {state['selected_model']}", f"Effort: {state['selected_effort']}",
             f"Selection reason: {state.get('reason') or 'not available'}",
             f"Authentication verified: {state.get('authentication', {}).get('verified', False)} (ChatGPT)",
             f"Workspace: {state['workspace']}", "", result["summary"], ""]
    if state.get("execution_workspace"):
        execution = state["execution_workspace"]
        lines += ["Execution snapshot: " + execution["path"], "", execution["notice"], ""]
    if result.get("failure_kind"):
        lines += ["Failure kind: " + result["failure_kind"], "Next action: " + result["next_action"], ""]
    for name in ("changed_files", "checks", "findings", "limitations"):
        if result[name]:
            lines += ["## " + name.replace("_", " ").capitalize(), ""]
            lines += ["- " + value for value in result[name]] + [""]
    lines += ["## Artifacts", ""] + [f"- {key}: {value}" for key, value in state["artifacts"].items()]
    Path(state["artifacts"]["report"]).write_text("\n".join(lines) + "\n", encoding="utf-8")


def metadata_fields():
    return ("kind", "review_of", "parent_job", "author_provider", "execution_provider", "attempt", "scope_paths", "execution_workspace", "review_source_job", "failure_kind", "next_action", "retryable")


def attach_result_metadata(state, result):
    for field in ("failure_kind", "next_action", "retryable"):
        state[field] = result.get(field)
    result.update({key: state.get(key) for key in metadata_fields()})
    result["artifacts"] = state["artifacts"]


def blocked_result(message, failure_kind=None):
    return {"status": "blocked", "summary": message, "changed_files": [], "checks": [], "findings": [], "limitations": [message],
            **failure_fields(message, failure_kind)}


def run_task(args):
    workspace, directory, state = initialize_run(args)
    artifacts = state["artifacts"]
    result = None
    try:
        with WorkspaceLock(directory / "workspace.lock"):
            if not workspace.is_dir():
                raise WorkerError("Workspace must be an existing directory.")
            if os.environ.get("CLAUDE_CODEX_WORKER") == "1" or os.environ.get("MANIFOLD_COLLAB_WORKER") == "1":
                raise WorkerError("Recursive delegation is disabled in an existing worker session.")
            if args.timeout <= 0:
                raise WorkerError("--timeout must be positive.")
            packet = read_json(args.packet)
            for field in ("parent_job", "review_of", "attempt"):
                supplied = getattr(args, field, None)
                if supplied is not None:
                    if field in packet and packet[field] != supplied:
                        raise WorkerError(f"CLI {field} does not match the task packet.", "validation")
                    packet[field] = supplied
            write_json(artifacts["packet"], packet)
            scopes, permission, approval = validate_packet(packet, workspace, args.permission, args.approval)
            snapshot_source, snapshot_scopes, evidence_files = workspace, scopes, {}
            for field in ("parent_job", "review_of"):
                if packet.get(field):
                    if field == "review_of" and packet.get("author_provider", "codex") == "claude":
                        # The primary caller owns the real Claude agent/session list.
                        # An external reference is not a local Codex job or a reason to launch Claude.
                        state["review_reference"] = {"provider": "claude", "id": packet[field], "local_state_verified": False,
                                                     "validation": "External Claude reference supplied by the primary caller; not independently verified by this controller."}
                        continue
                    prior_path = directory / packet[field] / "state.json"
                    if not prior_path.is_file() or scope_snapshot.is_link(prior_path.parent):
                        raise WorkerError(f"{field} must identify an existing saved job for this workspace.", "validation")
                    prior = read_json(prior_path)
                    if prior.get("workspace") != str(workspace) or prior.get("job_id", prior.get("id")) != packet[field]:
                        raise WorkerError(f"{field} state does not match this workspace.", "validation")
                    if field == "review_of":
                        if prior.get("status") != "complete":
                            raise WorkerError("A Codex review requires a completed prior candidate; inspect the prior blocked result before requesting review.", "validation")
                        prior_execution = prior.get("execution_workspace") or {}
                        if not isinstance(prior_execution, dict) or prior_execution.get("strategy") != "scope_snapshot" or not prior_execution.get("path"):
                            raise WorkerError("Prior job has no verifiable candidate snapshot; create an explicit candidate before review.", "validation")
                        candidate = Path(os.path.abspath(prior_execution["path"]))
                        prior_run = prior_path.parent
                        if not within(candidate, prior_run):
                            raise WorkerError("Prior review candidate escapes its saved job directory.", "validation")
                        scope_snapshot.ordinary_path(candidate, Path(candidate.anchor))
                        if not candidate.is_dir():
                            raise WorkerError("Prior review candidate snapshot is missing.", "validation")
                        snapshot_source = candidate
                        snapshot_scopes = [candidate / scope.relative_to(workspace) for scope in scopes]
                        for scope in snapshot_scopes:
                            scope_snapshot.ordinary_path(scope, candidate)
                            if not scope.exists():
                                raise WorkerError("Missing review candidate input: " + scope.relative_to(candidate).as_posix() + ". The original workspace will not be used as a fallback.", "validation")
                        prior_patch = prior_run / "changes.patch"
                        if prior_patch.exists():
                            scope_snapshot.ordinary_path(prior_patch, Path(prior_patch.anchor))
                            evidence_files[".savvy-review-evidence/changes.patch"] = prior_patch
                            evidence_copy = Path(state["run_dir"]) / "review-source.patch"
                            shutil.copy2(prior_patch, evidence_copy, follow_symlinks=False)
                            artifacts["review_source_patch"] = str(evidence_copy)
                        state["review_source_job"] = packet[field]
                        state["review_reference"] = {"provider": "codex", "id": packet[field], "local_state_verified": True,
                                                     "candidate": str(candidate), "validation": "Completed local candidate snapshot; no original-workspace fallback."}
                        packet["review_evidence"] = {"source_job": packet[field], "patch_paths": list(evidence_files),
                                                     "instruction": "Review the candidate files in this cwd and the supplied patch; do not read the original workspace."}
                    if field == "parent_job" and "attempt" not in packet:
                        packet["attempt"] = int(prior.get("attempt", 1)) + 1
            state.update(reason=packet["reason"], goal=packet["goal"], task=packet["goal"], permission=permission, approval=approval,
                         kind=packet["kind"], scope_paths=[scope.relative_to(workspace).as_posix() for scope in scopes],
                         parent_job=packet.get("parent_job"), review_of=packet.get("review_of"), attempt=packet.get("attempt", 1),
                         author_provider=packet.get("author_provider", "codex"))
            snapshot = scope_snapshot.prepare(snapshot_source, snapshot_scopes, Path(state["run_dir"]), packet["scope_paths"], evidence_files)
            execution = Path(snapshot["path"])
            state["execution_workspace"] = {key: snapshot[key] for key in ("path", "strategy", "source_workspace", "scope_paths", "scope_entries", "omitted_paths", "read_only_evidence_paths", "notice")}
            write_json(artifacts["packet"], packet)
            write_json(artifacts["state"], state)
            env = child_environment()
            cli = discover_cli()
            state["cli"] = cli
            state["capabilities"] = capabilities(cli, env)
            state["authentication"] = check_auth(cli, env)
            catalog = cached_catalog()
            verify_model(catalog, args.model, args.effort)
            state["catalog"] = {key: value for key, value in catalog.items() if key != "models"}
            write_json(artifacts["schema"], RESPONSE_SCHEMA)
            command = build_command(cli, execution, args.model, args.effort, permission, artifacts)
            state["command"] = command
            def started(pid):
                state.update(status="running", child_pid=pid, updated_at=now(), progress="working", summary="Codex is working on the task.")
                write_json(artifacts["state"], state)
            def progress():
                evidence = event_evidence(artifacts["events"])
                fields = {"last_tool": evidence["last_tool"], "last_message": evidence["last_message"], "changed_files": sorted(evidence["file_changes"])}
                if any(state.get(key) != value for key, value in fields.items()):
                    state.update(fields, updated_at=now())
                    write_json(artifacts["state"], state)
            execution_scopes = [execution / entry["path"] for entry in snapshot["scope_entries"]]
            prompt_scopes = execution_scopes + [execution / path for path in snapshot["read_only_evidence_paths"]]
            returncode = execute(command, build_prompt(packet, prompt_scopes, permission), execution, env, args.timeout, artifacts, started, progress)
            state["cli_exit_code"] = returncode
            evidence = event_evidence(artifacts["events"])
            state["observed_tools"] = sorted(evidence["successful_tools"])
            state.update(last_tool=evidence["last_tool"], last_message=evidence["last_message"])
            try:
                response = read_json(artifacts["model_response"])
            except FileNotFoundError:
                if not evidence["last_message"]:
                    raise WorkerError("Codex produced no final structured response.")
                response = json.loads(evidence["last_message"])
                write_json(artifacts["model_response"], response)
            changes, rejected = scope_snapshot.inspect_changes(snapshot)
            state["actual_changed_files"] = [change["path"] for change in changes]
            if rejected:
                raise WorkerError("Actual snapshot changes fall outside authorized scope or contain unsafe paths: " + ", ".join(rejected) + ". No patch was exported.", "validation")
            if permission == "read-only" and changes:
                raise WorkerError("A read-only task actually modified snapshot files. No patch was exported.", "validation")
            response = validate_response(response)
            response["changed_files"] = sorted(set(response["changed_files"]) | set(state["actual_changed_files"]))
            result = assess_result(response, evidence, packet, execution, execution_scopes, permission, returncode,
                                   Path(artifacts["stderr"]).read_text(encoding="utf-8", errors="replace"), snapshot["scope_entries"])
            if result["status"] == "complete":
                changes_path = Path(state["run_dir"]) / "changes.json"
                if changes:
                    patch_path = scope_snapshot.export(snapshot, Path(state["run_dir"]), changes)
                    artifacts.update(patch=str(patch_path), patch_path=str(patch_path))
                write_json(changes_path, {"source_workspace": str(workspace), "execution_workspace": str(execution),
                                          "scope_paths": state["scope_paths"], "changes": changes,
                                          "patch_base": "authorized current source contents at attempt start; source may contain uncommitted changes",
                                          "source_modified_by_controller": False, "notice": scope_snapshot.NOTICE})
                artifacts.update(changes=str(changes_path), changes_path=str(changes_path))
    except (WorkerError, scope_snapshot.SnapshotError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        result = blocked_result(str(exc), getattr(exc, "failure_kind", None))
    except KeyboardInterrupt:
        result = blocked_result("Worker interrupted; its own child process tree was terminated.")
    except Exception as exc:
        result = blocked_result(f"Unexpected worker failure ({type(exc).__name__}): {exc}")
    finally:
        if result is None:
            result = blocked_result("Worker did not reach a verified final result.")
        attach_result_metadata(state, result)
        state.update(status=result["status"], finished_at=now(), updated_at=now(), result=result, changed_files=result["changed_files"],
                     summary=result["summary"], progress=result["status"])
        write_json(artifacts["final"], result)
        write_json(artifacts["state"], state)
        write_report(state, result)
    return state


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # Windows os.kill is a termination API for most signal values. Query a
        # handle instead; status must never signal or terminate any process.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def status(args):
    if args.run_dir:
        path = Path(args.run_dir).expanduser().resolve() / "state.json"
        directory = path.parent.parent
    else:
        directory = state_directory(workspace_path(args.workspace), args.state_root)
        path = None
    if path:
        pass
    elif args.job_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.job_id):
            raise WorkerError("Invalid job id.")
        path = directory / args.job_id / "state.json"
    else:
        candidates = sorted(directory.glob("*/state.json"))
        if not candidates:
            raise WorkerError("No saved worker runs for this workspace.")
        path = candidates[-1]
    saved = read_json(path)
    lost_controller = saved.get("status") in {"preparing", "running"} and not process_alive(saved.get("controller_pid"))
    launch_path = path.parent / "launch.json"
    failed_launch = saved.get("status") == "queued" and launch_path.exists() and not process_alive(read_json(launch_path).get("pid"))
    if lost_controller or failed_launch:
        result = blocked_result("The worker controller exited before recording a final result; inspect saved logs.")
        if process_alive(saved.get("child_pid")):
            result["limitations"].append("A recorded child PID may still be alive; status does not terminate processes.")
        attach_result_metadata(saved, result)
        saved.update(status="blocked", result=result, finished_at=now(), updated_at=now(), summary=result["summary"], progress="blocked")
        write_json(path, saved)
        write_json(saved["artifacts"]["final"], result)
        write_report(saved, result)
    return saved


def wait_for_run(args):
    if args.timeout < 0:
        raise WorkerError("--timeout must be nonnegative.")
    query = argparse.Namespace(run_dir=args.run_dir, workspace=None, state_root=None, job_id=None)
    deadline = time.monotonic() + args.timeout
    while True:
        saved = status(query)
        terminal = saved.get("status") in TERMINAL_STATUSES
        if terminal or time.monotonic() >= deadline:
            artifacts = saved.get("artifacts", {})
            return {"status": saved.get("status"), "id": saved.get("job_id"), "job_id": saved.get("job_id"),
                    "summary": saved.get("summary") or saved.get("result", {}).get("summary"),
                    "model": saved.get("model"), "effort": saved.get("effort"), "goal": saved.get("goal"),
                    "run_dir": saved.get("run_dir"), "state_path": artifacts.get("state"),
                    "report_path": artifacts.get("report"), "result_path": artifacts.get("final"),
                    "result": saved.get("result"), "wait_timed_out": not terminal,
                    **{key: saved.get(key) for key in metadata_fields()}, "artifacts": artifacts}
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def doctor():
    """Collect local preflight facts even if one check fails. Never infer readiness."""
    env = child_environment()
    result = {"status": "blocked", "ready": False, "cli": None, "capabilities": None,
              "authentication": {"verified": False, "method": "ChatGPT"}, "checks": {}, "models": [],
              "python": {"executable": sys.executable, "version": sys.version.split()[0]},
              "config_preserved": True, "provider": "openai", "billing": "ChatGPT subscription; no per-token dollar estimate",
              "proxy_environment_names": sorted(key for key in env if key.upper().endswith("_PROXY")), "model_calls": 0}
    failures = []
    cli = None
    try:
        if os.environ.get("CLAUDE_CODEX_WORKER") == "1" or os.environ.get("MANIFOLD_COLLAB_WORKER") == "1":
            raise WorkerError("This is already a worker session; launch doctor from the primary session.", "validation")
        cli = discover_cli()
        result["cli"] = cli
        result["checks"]["official_cli"] = {"ok": True, "detail": "CLI entry discovered; identity and features checked separately."}
    except (WorkerError, OSError) as exc:
        failures.append(exc)
        result["checks"]["official_cli"] = {"ok": False, "detail": str(exc)}
    if cli:
        try:
            result["capabilities"] = capabilities(cli, env)
            result["checks"]["version"] = {"ok": True, "detail": result["capabilities"]["version"]}
            result["checks"]["features"] = {"ok": True, "detail": "Required worker features and exec flags verified."}
        except (WorkerError, OSError) as exc:
            failures.append(exc)
            for name in ("version", "features"):
                result["checks"][name] = {"ok": False, "detail": str(exc)}
        try:
            result["authentication"] = check_auth(cli, env)
            result["checks"]["authentication"] = {"ok": True, "detail": "Official CLI verified ChatGPT login."}
        except (WorkerError, OSError) as exc:
            failures.append(exc)
            result["checks"]["authentication"] = {"ok": False, "detail": str(exc)}
    else:
        for name in ("version", "features", "authentication"):
            result["checks"][name] = {"ok": False, "detail": "CLI discovery must pass before this check."}
    try:
        catalog = cached_catalog()
        result["catalog"] = {key: value for key, value in catalog.items() if key != "models"}
        result["models"] = catalog["models"]
        result["checks"]["catalog"] = {"ok": True, "detail": "Account cache read; no refresh or inference requested.", "source": catalog["source"]}
    except (WorkerError, OSError, ValueError) as exc:
        failures.append(exc)
        result["checks"]["catalog"] = {"ok": False, "detail": str(exc)}
    try:
        git = shutil.which("git.exe") or shutil.which("git")
        if not git:
            raise WorkerError("Git is required to create the independent scoped snapshot baseline.", "unsupported_cli")
        git_version = capture([git, "--version"], env)
        if git_version.returncode != 0 or not git_version.stdout.strip().startswith("git version "):
            raise WorkerError("Git version verification failed.", "unsupported_cli")
        result["checks"]["git"] = {"ok": True, "detail": git_version.stdout.strip(), "path": git}
    except (WorkerError, OSError) as exc:
        failures.append(exc)
        result["checks"]["git"] = {"ok": False, "detail": str(exc)}
    result["ready"] = not failures
    result["status"] = "complete" if result["ready"] else "blocked"
    if failures:
        result.update(summary=str(failures[0]), limitation=str(failures[0]),
                      **failure_fields(str(failures[0]), getattr(failures[0], "failure_kind", None)))
    else:
        result.update(summary="Official CLI, required features, ChatGPT login and account catalog are ready.", **clear_failure())
    return result


def cleanup(args):
    """Delete only old, terminal, self-contained job directories. No CLI/model use."""
    if args.days < 1:
        raise WorkerError("--days must be at least 1.", "validation")
    workspace = workspace_path(args.workspace)
    declared_root = Path(os.path.abspath(Path(args.state_root).expanduser())) if args.state_root else Path.home() / ".claude" / "codex-worker-runs"
    digest = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()[:20]
    lexical_directory = declared_root / digest
    try:
        scope_snapshot.ordinary_path(lexical_directory, Path(lexical_directory.anchor))
    except scope_snapshot.SnapshotError as exc:
        raise WorkerError("Cleanup refuses linked runtime path components: " + str(exc), "validation") from exc
    directory = lexical_directory.resolve()
    result = {"status": "complete", "days": args.days, "dry_run": bool(args.dry_run), "runtime_root": str(directory),
              "deleted": [], "would_delete": [], "skipped": [], "errors": [], "model_calls": 0}
    if not directory.exists():
        return result
    if scope_snapshot.is_link(directory):
        raise WorkerError("Cleanup refuses a linked runtime root.", "validation")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    latest_ids, unknown_latest = set(), False
    latest_path = directory / "latest.json"
    if latest_path.exists():
        try:
            if scope_snapshot.is_link(latest_path):
                raise ValueError("linked latest.json")
            latest = read_json(latest_path)
            if isinstance(latest, str):
                latest_ids.add(Path(latest).parent.name if Path(latest).name == "state.json" else Path(latest).name)
            elif isinstance(latest, dict):
                for field in ("job_id", "id", "run_dir", "state_path"):
                    if latest.get(field):
                        value = Path(str(latest[field]))
                        latest_ids.add(value.parent.name if field == "state_path" else value.name)
            if not latest_ids:
                unknown_latest = True
        except (OSError, ValueError, TypeError):
            unknown_latest = True
    for candidate in sorted(directory.iterdir()):
        if not candidate.is_dir():
            continue
        reason = None
        deletion_attempted = False
        try:
            resolved = candidate.resolve()
            if not within(resolved, directory) or resolved == directory or scope_snapshot.is_link(candidate):
                reason = "invalid_or_linked_path"
            elif unknown_latest or candidate.name in latest_ids:
                reason = "protected_latest_reference"
            elif not re.fullmatch(r"[A-Za-z0-9_-]+", candidate.name):
                reason = "invalid_job_id"
            else:
                if scope_snapshot.is_link(candidate / "state.json"):
                    raise ValueError("linked state.json")
                saved = read_json(candidate / "state.json")
                launch = None
                if (candidate / "launch.json").exists():
                    if scope_snapshot.is_link(candidate / "launch.json"):
                        raise ValueError("linked launch.json")
                    launch = read_json(candidate / "launch.json")
                    if not isinstance(launch, dict) or not isinstance(launch.get("pid"), int) or isinstance(launch.get("pid"), bool) or launch["pid"] <= 0:
                        raise ValueError("launch.json must contain a positive integer pid")
                if (not isinstance(saved, dict) or saved.get("workspace") != str(workspace)
                        or saved.get("job_id", saved.get("id")) != candidate.name
                        or Path(saved.get("run_dir", "")).resolve() != resolved):
                    reason = "invalid_state_path_or_identity"
                elif not isinstance(saved.get("artifacts", {}), dict):
                    reason = "invalid_artifact_metadata"
                elif saved.get("status") not in TERMINAL_STATUSES:
                    reason = "active_or_unknown_status"
                elif any(process_alive(saved.get(field)) for field in ("controller_pid", "child_pid")):
                    reason = "recorded_process_alive"
                elif launch and process_alive(launch["pid"]):
                    reason = "launched_process_alive"
                elif any(isinstance(value, str) and not within(Path(value).resolve(), resolved) for value in saved.get("artifacts", {}).values()):
                    reason = "external_artifact_path"
                else:
                    finished_value = saved.get("finished_at")
                    if not isinstance(finished_value, str) or not finished_value.strip():
                        raise ValueError("finished_at must be a nonempty timestamp string")
                    finished = dt.datetime.fromisoformat(finished_value.replace("Z", "+00:00"))
                    if finished.tzinfo is None:
                        reason = "invalid_finished_at"
                    elif finished > cutoff:
                        reason = "not_expired"
                    else:
                        execution = saved.get("execution_workspace") or {}
                        if isinstance(execution, str):
                            execution = {"path": execution}
                        if not isinstance(execution, dict):
                            reason = "invalid_execution_workspace"
                        elif "worktree" in str(execution.get("strategy", "")).lower() or (execution.get("path") and not within(Path(execution["path"]).resolve(), resolved)):
                            reason = "linked_or_external_workspace"
                        else:
                            for root, dirs, files in os.walk(candidate, followlinks=False):
                                children = [Path(root) / name for name in dirs + files]
                                if any(scope_snapshot.is_link(child) for child in children):
                                    reason = "linked_path_in_job"
                                    break
                                if ".git" in files:
                                    reason = "linked_worktree"
                                    break
            if reason:
                result["skipped"].append({"run_dir": str(candidate), "reason": reason})
            elif args.dry_run:
                result["would_delete"].append(str(candidate))
            else:
                # Re-resolve immediately before deletion; never cross the runtime root.
                if candidate.resolve() != resolved or not within(resolved, directory) or scope_snapshot.is_link(candidate):
                    raise WorkerError("Job path changed during cleanup.", "validation")
                deletion_attempted = True
                shutil.rmtree(candidate)
                result["deleted"].append(str(candidate))
        except (OSError, ValueError, TypeError, KeyError, WorkerError) as exc:
            if deletion_attempted:
                result["errors"].append({"run_dir": str(candidate), "detail": str(exc)})
                result["status"] = "blocked"
                result.update(failure_fields(str(exc)))
            else:
                result["skipped"].append({"run_dir": str(candidate), "reason": "invalid_or_unreadable_state", "detail": str(exc)})
    return result


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    sub = command.add_subparsers(dest="command", required=True)
    diagnostic = sub.add_parser("doctor", help="Check CLI/auth/features and cached model choices; never run a model.")
    diagnostic.add_argument("--json", action="store_true", help="JSON is always used; this flag is accepted for explicit callers.")
    cat = sub.add_parser("catalog", help="Read the account cache by default; --refresh explicitly permits a network refresh.")
    group = cat.add_mutually_exclusive_group()
    group.add_argument("--cached", action="store_true")
    group.add_argument("--refresh", action="store_true")
    cat.add_argument("--json", action="store_true")
    for action, help_text in (("run", "Run one task with explicit model and effort."), ("start", "Queue one hidden background run and immediately return its artifact paths.")):
        run = sub.add_parser(action, help=help_text)
        run.add_argument("--workspace", required=True)
        run.add_argument("--packet", required=True)
        run.add_argument("--model", required=True)
        run.add_argument("--effort", required=True)
        run.add_argument("--permission", choices=["read-only", "workspace-write"])
        run.add_argument("--approval", choices=["never", "auto-review"])
        run.add_argument("--timeout", type=int, default=900)
        run.add_argument("--state-root", help="Override run storage for isolated testing.")
        run.add_argument("--job-id", help="Explicit unique id; existing completed/running runs cannot be overwritten.")
        run.add_argument("--parent-job", help="Link an explicitly requested new attempt to an existing job.")
        run.add_argument("--review-of", help="Link a read-only kind=review task to an existing job.")
        run.add_argument("--attempt", type=int, help="Explicit positive attempt number; no hidden retries occur.")
    query = sub.add_parser("status", help="Read a saved run without resubmitting work.")
    location = query.add_mutually_exclusive_group(required=True)
    location.add_argument("--workspace")
    location.add_argument("--run-dir")
    query.add_argument("--job-id")
    query.add_argument("--state-root")
    waiting = sub.add_parser("wait", help="Wait on a saved run using no model calls; timeout never marks it complete.")
    waiting.add_argument("--run-dir", required=True)
    waiting.add_argument("--timeout", type=float, default=900)
    cleaning = sub.add_parser("cleanup", help="Remove only expired terminal jobs; never starts a model or schedules cleanup.")
    cleaning.add_argument("--workspace", required=True)
    cleaning.add_argument("--state-root")
    cleaning.add_argument("--days", type=int, default=30)
    cleaning.add_argument("--dry-run", action="store_true")
    return command


def main(argv=None):
    # Desktop process APIs decode stdout as UTF-8 even on Windows systems whose
    # default pipe encoding is GBK. Reconfigure only this process's streams.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            output = doctor()
        elif args.command == "catalog":
            output = refresh_catalog(discover_cli(), child_environment()) if args.refresh else cached_catalog()
            output = {"status": "complete", **output}
        elif args.command == "run":
            output = run_task(args)
        elif args.command == "start":
            output = start_task(args)
        elif args.command == "wait":
            output = wait_for_run(args)
        elif args.command == "cleanup":
            output = cleanup(args)
        else:
            output = status(args)
    except (WorkerError, OSError, ValueError) as exc:
        output = {"status": "blocked", "summary": str(exc), **failure_fields(str(exc), getattr(exc, "failure_kind", None))}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get("status") in {"complete", "queued", "preparing", "running"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
