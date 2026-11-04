"""Contract tests use temporary directories and mocks; no model is ever invoked."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import codex_delegate as worker


MODEL = "gpt-6.1-sol"
CATALOG = {"source": "test", "models": [{"id": MODEL, "efforts": ["low", "medium", "high"], "default_effort": "low"}]}


def packet(kind="inspect"):
    value = {"version": 1, "goal": "Check the scoped example module and explain its behavior.", "kind": kind,
             "scope_paths": ["example.py"], "acceptance": ["Explain the return value with code evidence."],
             "reason": "Sol medium is sufficient for this bounded code inspection.", "required_tools": ["exec_command"]}
    if kind == "implement":
        value["authorization"] = {"write_intent": True}
    return value


def response(status="complete"):
    return {"status": status, "summary": "The example returns its input plus one; the scoped file was read.",
            "changed_files": [], "checks": ["Read example.py with a successful command (exit 0)."], "findings": [], "limitations": []}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-worker-test-")
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        (self.workspace / "example.py").write_text("def example(x):\n    return x + 1\n", encoding="utf-8")
        self.packet_path = self.root / "task.json"
        worker.write_json(self.packet_path, packet())
        self.args = argparse.Namespace(workspace=str(self.workspace), packet=str(self.packet_path), model=MODEL,
                                       effort="medium", permission=None, approval=None, timeout=5,
                                       state_root=str(self.root / "runs"), job_id=None)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CODEX_WORKER": "0", "MANIFOLD_COLLAB_WORKER": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def fake_execute(self, command, prompt, workspace, env, timeout, artifacts, on_started, on_progress=None):
        self.assertIn(self.args.model, command)
        self.assertIn("Task packet follows as JSON data:", prompt)
        self.assertEqual(env["CLAUDE_CODEX_WORKER"], "1")
        on_started(12345)
        event = {"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "exit_code": 0,
                                                        "command": "read example.py", "aggregated_output": "return x + 1"}}
        Path(artifacts["events"]).write_text(json.dumps(event) + "\n", encoding="utf-8")
        worker.write_json(artifacts["model_response"], response())
        if on_progress:
            on_progress()
        return 0

    @contextlib.contextmanager
    def preflight(self, execute=None):
        with mock.patch.object(worker, "discover_cli", return_value=["codex.exe"]), \
             mock.patch.object(worker, "capabilities", return_value={"version": "codex-cli test"}), \
             mock.patch.object(worker, "check_auth", return_value={"verified": True, "method": "ChatGPT"}), \
             mock.patch.object(worker, "cached_catalog", return_value=CATALOG), \
             mock.patch.object(worker, "execute", side_effect=execute or self.fake_execute):
            yield

    def evidence(self, events):
        path = self.root / "events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return worker.event_evidence(path)

    def assess(self, value, events=(), **kwargs):
        return worker.assess_result(value, self.evidence(events), packet(), self.workspace,
                                    [self.workspace / "example.py"], "read-only", kwargs.get("returncode", 0), kwargs.get("stderr", ""))

    def test_environment_strips_api_keys_and_provider_overrides_but_keeps_home(self):
        original = {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret", "OPENAI_BASE_URL": "external",
                    "CODEX_MODEL_PROVIDER": "other", "ANTHROPIC_AUTH_TOKEN": "secret", "CODEX_OSS_BASE_URL": "external",
                    "HOME": "original-home", "CODEX_HOME": "original-codex", "PATH": "path", "https_proxy": "existing"}
        clean = worker.child_environment(original, {"https": "system", "http": "system-http", "no": "localhost"})
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_MODEL_PROVIDER", "ANTHROPIC_AUTH_TOKEN", "CODEX_OSS_BASE_URL"):
            self.assertNotIn(key, clean)
        self.assertEqual(clean["HOME"], "original-home")
        self.assertEqual(clean["CODEX_HOME"], "original-codex")
        self.assertEqual(clean["https_proxy"], "existing")
        self.assertNotIn("HTTPS_PROXY", clean)
        self.assertEqual(clean["HTTP_PROXY"], "system-http")
        self.assertEqual(clean["NO_PROXY"], "localhost")
        self.assertEqual(original["OPENAI_API_KEY"], "secret")

    def test_cli_discovery_prefers_native_executable(self):
        with mock.patch.object(worker.shutil, "which", side_effect=lambda name: str(self.root / "codex.exe") if name == "codex.exe" else None):
            self.assertEqual(worker.discover_cli(), [str((self.root / "codex.exe").resolve())])

    def test_cli_discovery_uses_node_with_official_npm_shim(self):
        npm = self.root / "npm"
        js = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        js.parent.mkdir(parents=True)
        js.write_text("// fixture", encoding="utf-8")
        values = {"codex": str(npm / "codex.cmd"), "node.exe": str(self.root / "node.exe")}
        with mock.patch.object(worker.shutil, "which", side_effect=values.get):
            self.assertEqual(worker.discover_cli(), [str((self.root / "node.exe").resolve()), str(js.resolve())])

    def test_user_global_cli_on_path_wins_over_desktop_bundled_exe(self):
        npm = self.root / "npm"
        js = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        js.parent.mkdir(parents=True)
        js.write_text("// official shim fixture", encoding="utf-8")
        values = {"codex": str(npm / "codex.cmd"), "node.exe": str(self.root / "node.exe"),
                  "codex.exe": str(self.root / "desktop-bundled" / "codex.exe")}
        with mock.patch.object(worker.shutil, "which", side_effect=values.get):
            selected = worker.discover_cli()
        self.assertEqual(selected, [str((self.root / "node.exe").resolve()), str(js.resolve())])

    def test_auth_accepts_chatgpt_only_and_never_prints_key(self):
        for output, code in (("Logged in using ChatGPT", 0), ("Logged in using an API key: sk-test", 0), ("Logged in using ChatGPT", 1)):
            result = subprocess.CompletedProcess([], code, "", output)
            with mock.patch.object(worker, "capture", return_value=result):
                if code == 0 and "ChatGPT" in output:
                    self.assertTrue(worker.check_auth(["codex"], {})["verified"])
                else:
                    with self.assertRaises(worker.WorkerError) as failure:
                        worker.check_auth(["codex"], {})
                    self.assertNotIn("sk-test", str(failure.exception))

    def test_catalog_normalizes_real_cache_shape_and_checks_effort(self):
        raw = {"fetched_at": "test", "identity": {"not_exported": True}, "models": [
            {"slug": MODEL, "display_name": "Future account model", "description": "official metadata " + "x" * 1200,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]}]}
        catalog = worker.normalize_catalog(raw, "cache")
        self.assertNotIn("identity", catalog)
        self.assertEqual(catalog["models"][0]["display_name"], "Future account model")
        self.assertEqual(len(catalog["models"][0]["description"]), 1000)
        worker.verify_model(catalog, MODEL, "medium")
        for model, effort in ((MODEL, "ultra"), ("invented-model", "medium")):
            with self.assertRaises(worker.WorkerError):
                worker.verify_model(catalog, model, effort)

    def test_scope_rejects_escape_globs_and_symlink_escape(self):
        for path in ("../outside.py", str(self.root / "outside.py"), "*.py", "a\x00b"):
            with self.assertRaises(worker.WorkerError):
                worker.normalize_scope(self.workspace, [path])
        self.assertEqual(worker.normalize_scope(self.workspace, ["example.py", "./example.py"]), [self.workspace / "example.py"])
        try:
            (self.workspace / "link").symlink_to(self.root, target_is_directory=True)
        except OSError:
            pass
        else:
            with self.assertRaises(worker.WorkerError):
                worker.normalize_scope(self.workspace, ["link/outside.py"])

    def test_packet_requires_explicit_write_authorization_and_scope(self):
        value = packet("implement")
        self.assertEqual(worker.validate_packet(value, self.workspace)[1:], ("workspace-write", "auto-review"))
        for alteration in ({"authorization": {}}, {"scope_paths": []}, {"authorization": {"write_intent": "true"}}):
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet({**value, **alteration}, self.workspace)
        with self.assertRaises(worker.WorkerError):
            worker.validate_packet(packet(), self.workspace, "workspace-write")
        with self.assertRaises(worker.WorkerError):
            worker.validate_packet(value, self.workspace, approval="never")

    def test_command_keeps_config_rules_and_shell_with_no_conflicting_flags(self):
        artifacts = {"schema": "schema.json", "model_response": "final.json"}
        readonly = worker.build_command(["codex.exe"], self.workspace, MODEL, "medium", "read-only", artifacts)
        writing = worker.build_command(["codex.exe"], self.workspace, MODEL, "medium", "workspace-write", artifacts)
        self.assertIn("--approve-for-me", writing)
        self.assertNotIn("-a", writing)
        self.assertNotIn("-s", writing)
        self.assertIn("-a", readonly)
        self.assertIn("never", readonly)
        for command in (readonly, writing):
            self.assertNotIn("--ignore-user-config", command)
            self.assertNotIn("--ignore-rules", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertNotIn("shell_tool", [command[i+1] for i, flag in enumerate(command[:-1]) if flag == "--disable"])
            self.assertIn('model_provider="openai"', command)
            self.assertIn("skip_host_skill_discovery", command)
            self.assertEqual(command[-1], "-")

    def test_workspace_os_lock_rejects_second_owner_then_releases(self):
        path = self.root / "lock"
        with worker.WorkspaceLock(path):
            with self.assertRaises(worker.WorkerError):
                with worker.WorkspaceLock(path):
                    self.fail("Second owner acquired lock")
        with worker.WorkspaceLock(path):
            pass

    def test_required_tool_needs_completed_success_not_started_or_exit_one(self):
        for event in (
            {"type": "item.started", "item": {"type": "command_execution", "status": "in_progress"}},
            {"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "exit_code": 1}},
        ):
            assessed = self.assess(response(), [event])
            self.assertEqual(assessed["status"], "blocked")
            self.assertIn("required_tools", " ".join(assessed["limitations"]))

    def test_permission_denial_and_exit_zero_cannot_be_success(self):
        event = {"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "exit_code": 0,
                                                      "aggregated_output": "Access is denied"}}
        assessed = self.assess(response(), [event])
        self.assertEqual(assessed["status"], "blocked")
        self.assertIn("denial", " ".join(assessed["limitations"]))

    def test_model_blocked_is_preserved_and_cli_exit_zero_is_not_success(self):
        value = response("blocked")
        self.assertEqual(self.assess(value)["status"], "blocked")
        value = response()
        value.update(summary="Done", checks=[], findings=[], changed_files=[])
        self.assertEqual(self.assess(value)["status"], "blocked")
        self.assertEqual(self.assess(response(), returncode=3)["status"], "blocked")

    def test_readonly_changes_and_write_scope_violations_are_reported(self):
        value = response()
        value["changed_files"] = ["../outside.py"]
        checked = self.assess(value)
        self.assertEqual(checked["status"], "blocked")
        self.assertIn("outside authorized scope", " ".join(checked["limitations"]))

    def test_successful_run_persists_actual_contract_and_does_not_touch_dirty_files(self):
        dirty = self.workspace / "preexisting-dirty.txt"
        dirty.write_text("keep this", encoding="utf-8")
        with self.preflight():
            state = worker.run_task(self.args)
        self.assertEqual(state["status"], "complete")
        self.assertTrue(state["authentication"]["verified"])
        self.assertEqual(state["observed_tools"], ["exec_command"])
        self.assertEqual(state["model"], MODEL)
        self.assertEqual(state["goal"], packet()["goal"])
        self.assertEqual(dirty.read_text(encoding="utf-8"), "keep this")
        for name in ("state", "final", "events", "stderr", "packet", "report"):
            self.assertTrue(Path(state["artifacts"][name]).exists())
        self.assertEqual(worker.read_json(state["result_path"])["status"], "complete")

    def test_auth_failure_and_timeout_always_leave_terminal_state(self):
        for error in ("ChatGPT auth not verified", "Task timed out after 5 seconds"):
            with self.preflight(), mock.patch.object(worker, "execute", side_effect=worker.WorkerError(error)):
                state = worker.run_task(self.args)
            self.assertEqual(state["status"], "blocked")
            self.assertIsNotNone(state["finished_at"])
            self.assertEqual(worker.read_json(state["artifacts"]["state"])["status"], "blocked")

    def test_bad_packet_and_recursion_are_blocked_before_cli(self):
        with mock.patch.object(worker, "discover_cli") as cli:
            self.packet_path.write_text("invalid json", encoding="utf-8")
            saved = worker.run_task(self.args)
            self.assertEqual(saved["status"], "blocked")
            cli.assert_not_called()
        worker.write_json(self.packet_path, packet())
        with mock.patch.dict(os.environ, {"CLAUDE_CODEX_WORKER": "1"}), mock.patch.object(worker, "discover_cli") as cli:
            self.assertEqual(worker.run_task(self.args)["status"], "blocked")
            cli.assert_not_called()

    def test_async_start_returns_paths_and_does_not_run_auth_or_model(self):
        with mock.patch.object(worker.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())) as popen, mock.patch.object(worker, "check_auth") as auth:
            queued = worker.start_task(self.args)
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(popen.call_args.args[0][0], sys.executable)
            self.assertIn("--job-id", popen.call_args.args[0])
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUTF8"], "1")
            auth.assert_not_called()
            saved = worker.read_json(queued["state_path"])
            self.assertEqual(saved["job_id"], queued["job_id"])
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(saved["result_path"], queued["result_path"])

    def test_async_queue_is_claimed_once_and_duplicate_cannot_overwrite(self):
        with mock.patch.object(worker.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())):
            queued = worker.start_task(self.args)
        self.args.job_id = queued["job_id"]
        with self.preflight():
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        with self.assertRaises(worker.WorkerError):
            worker.run_task(self.args)
        self.assertEqual(worker.read_json(queued["state_path"])["status"], "complete")

    def test_async_launch_failure_is_terminal_for_same_id(self):
        with mock.patch.object(worker.subprocess, "Popen", side_effect=PermissionError("denied")):
            saved = worker.start_task(self.args)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(worker.read_json(saved["artifacts"]["state"])["job_id"], saved["job_id"])
        self.assertTrue(Path(saved["result_path"]).exists())

    def test_wait_timeout_keeps_queued_and_terminal_result_is_compact(self):
        with mock.patch.object(worker.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())):
            queued = worker.start_task(self.args)
        args = argparse.Namespace(run_dir=queued["run_dir"], timeout=0)
        pending = worker.wait_for_run(args)
        self.assertEqual(pending["status"], "queued")
        self.assertTrue(pending["wait_timed_out"])
        self.args.job_id = queued["job_id"]
        with self.preflight():
            worker.run_task(self.args)
        done = worker.wait_for_run(args)
        self.assertEqual(done["status"], "complete")
        self.assertFalse(done["wait_timed_out"])
        self.assertEqual(done["result"]["summary"], response()["summary"])
        self.assertNotIn("command", done)

    def test_queued_python_startup_failure_becomes_blocked_on_status(self):
        with mock.patch.object(worker.subprocess, "Popen", return_value=mock.Mock(pid=-1)):
            queued = worker.start_task(self.args)
        saved = worker.status(argparse.Namespace(run_dir=queued["run_dir"], workspace=None, state_root=None, job_id=None))
        self.assertEqual(saved["status"], "blocked")
        self.assertIsNotNone(saved["finished_at"])

    def test_execute_timeout_kills_only_the_created_child(self):
        _, _, saved = worker.initialize_run(self.args)
        fake = mock.Mock()
        fake.wait.side_effect = subprocess.TimeoutExpired(["codex"], 0.01)
        with mock.patch.object(worker.subprocess, "Popen", return_value=fake), \
             mock.patch.object(worker, "kill_child_tree") as kill, \
             mock.patch.object(worker.time, "monotonic", side_effect=[0, 0, 6]):
            with self.assertRaises(worker.WorkerError):
                worker.execute(["codex"], "prompt", self.workspace, {}, 5, saved["artifacts"], lambda pid: None)
            kill.assert_called_once_with(fake)

    def test_preflight_timeout_kills_only_its_own_child_tree(self):
        fake = mock.Mock()
        fake.communicate.side_effect = subprocess.TimeoutExpired(["codex"], 0.1)
        with mock.patch.object(worker.subprocess, "Popen", return_value=fake), mock.patch.object(worker, "kill_child_tree") as kill:
            with self.assertRaises(worker.WorkerError):
                worker.capture(["codex", "login", "status"], {}, timeout=0.1)
            kill.assert_called_once_with(fake)

    def test_concrete_looking_summary_cannot_replace_actual_scoped_tool_evidence(self):
        task = packet()
        task["required_tools"] = []
        result = worker.assess_result(response(), self.evidence([]), task, self.workspace,
                                      [self.workspace / "example.py"], "read-only", 0, "")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("could not be verified", result["summary"])

    def test_cli_config_warnings_do_not_count_as_successful_tools(self):
        evidence = self.evidence([{"type": "item.completed", "item": {"type": "error", "message": "Unrecognized configuration setting"}}])
        self.assertEqual(evidence["successful_tools"], set())

    def test_entrypoint_reconfigures_only_its_streams_as_utf8(self):
        stdout = mock.Mock()
        stderr = mock.Mock()
        with mock.patch.object(worker.sys, "stdout", stdout), mock.patch.object(worker.sys, "stderr", stderr), \
             mock.patch.object(worker, "cached_catalog", return_value=CATALOG):
            self.assertEqual(worker.main(["catalog", "--cached"]), 0)
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_cli_parser_requires_explicit_model_effort_and_main_prints_json_only(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            worker.parser().parse_args(["run", "--workspace", str(self.workspace), "--packet", str(self.packet_path)])
        output = io.StringIO()
        with mock.patch.object(worker, "cached_catalog", return_value=CATALOG), contextlib.redirect_stdout(output):
            exit_code = worker.main(["catalog", "--cached"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "complete")

    def implement(self, scope=None):
        task = packet("implement")
        if scope is not None:
            task["scope_paths"] = scope
        worker.write_json(self.packet_path, task)

    def test_snapshot_copies_current_dirty_input_and_patch_never_changes_source(self):
        source = self.workspace / "example.py"
        source.write_text("dirty input\n", encoding="utf-8")
        (self.workspace / ".env").write_text("PASSWORD=not-for-worker", encoding="utf-8")
        self.implement()
        def change(*args):
            cwd = args[2]
            self.assertNotEqual(cwd, self.workspace)
            self.assertEqual((cwd / "example.py").read_text(), "dirty input\n")
            self.assertEqual(sorted(p.name for p in cwd.iterdir()), ["example.py"])
            self.fake_execute(*args)
            (cwd / "example.py").write_text("reviewable output\n", encoding="utf-8")
            return 0
        with self.preflight(change):
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(source.read_text(), "dirty input\n")
        self.assertEqual(saved["workspace"], str(self.workspace))
        self.assertEqual(saved["execution_workspace"]["strategy"], "scope_snapshot")
        patch_text = Path(saved["artifacts"]["patch"]).read_text()
        self.assertIn("-dirty input", patch_text)
        self.assertIn("+reviewable output", patch_text)
        self.assertNotIn("PASSWORD", patch_text)
        self.assertEqual(saved["result"]["changed_files"], ["example.py"])
        git = worker.shutil.which("git.exe") or worker.shutil.which("git")
        checked = subprocess.run([git, "apply", "--check", saved["artifacts"]["patch"]], cwd=self.workspace,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode("utf-8", "replace"))
        self.assertEqual(source.read_text(), "dirty input\n")

    def test_actual_unreported_out_of_scope_file_or_empty_directory_blocks_export(self):
        self.implement()
        for make_dir in (False, True):
            def change(*args):
                self.fake_execute(*args)
                target = args[2] / "unauthorized"
                if make_dir:
                    target.mkdir()
                else:
                    target.write_text("unreported", encoding="utf-8")
                return 0
            with self.preflight(change):
                saved = worker.run_task(self.args)
            self.assertEqual(saved["status"], "blocked")
            self.assertEqual(saved["failure_kind"], "validation")
            self.assertIsNone(saved["artifacts"]["patch"])
            self.assertIsNone(saved["artifacts"]["changes"])
            self.assertFalse((Path(saved["run_dir"]) / "changes.patch").exists())
            self.assertFalse((self.workspace / "unauthorized").exists())

    def test_actual_deletions_and_renames_are_delivered_without_source_mutation(self):
        directory = self.workspace / "src"
        directory.mkdir()
        (directory / "old.py").write_text("old dirty contents\n", encoding="utf-8")
        self.implement(["src"])
        def rename(*args):
            self.fake_execute(*args)
            (args[2] / "src" / "old.py").rename(args[2] / "src" / "new.py")
            return 0
        with self.preflight(rename):
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        changes = worker.read_json(saved["artifacts"]["changes"])["changes"]
        self.assertEqual({(entry["path"], entry["change"]) for entry in changes}, {("src/old.py", "deleted"), ("src/new.py", "added")})
        self.assertTrue((directory / "old.py").exists())
        self.assertFalse((directory / "new.py").exists())

    def test_rename_outside_scope_cannot_export_even_if_model_omits_it(self):
        self.implement()
        def rename(*args):
            self.fake_execute(*args)
            (args[2] / "example.py").rename(args[2] / "outside.py")
            return 0
        with self.preflight(rename):
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "blocked")
        self.assertIsNone(saved["artifacts"]["patch"])
        self.assertTrue((self.workspace / "example.py").exists())

    def test_actual_readonly_write_is_blocked_without_trusting_report(self):
        def change(*args):
            self.fake_execute(*args)
            (args[2] / "example.py").unlink()
            return 0
        with self.preflight(change):
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "blocked")
        self.assertIsNone(saved["artifacts"]["patch"])
        self.assertIn("read-only", saved["summary"])

    def test_root_empty_sensitive_and_in_repository_links_cannot_expand_scope(self):
        for scope in (["."], [], [".env"], [".git/config"], [".aws/credentials"]):
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet({**packet(), "scope_paths": scope}, self.workspace)
        link = self.workspace / "internal-link.py"
        try:
            link.symlink_to(self.workspace / "example.py")
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        with self.assertRaises(worker.WorkerError):
            worker.normalize_scope(self.workspace, ["internal-link.py"])

    def test_directory_scope_skips_credentials_and_runtime_without_copying(self):
        directory = self.workspace / "src"
        directory.mkdir()
        (directory / "code.py").write_text("safe\n", encoding="utf-8")
        (directory / ".env").write_text("private\n", encoding="utf-8")
        (directory / ".git").mkdir()
        (directory / ".git" / "config").write_text("private", encoding="utf-8")
        task = {**packet(), "scope_paths": ["src"]}
        worker.write_json(self.packet_path, task)
        with self.preflight():
            saved = worker.run_task(self.args)
        execution = saved["execution_workspace"]
        self.assertEqual(saved["status"], "complete")
        self.assertFalse((Path(execution["path"]) / "src" / ".env").exists())
        self.assertFalse((Path(execution["path"]) / "src" / ".git").exists())
        self.assertEqual(set(execution["omitted_paths"]), {"src/.env", "src/.git"})

    def test_nested_symlink_is_not_copied_and_no_model_is_started(self):
        directory = self.workspace / "src"
        directory.mkdir()
        try:
            (directory / "link.py").symlink_to(self.workspace / "example.py")
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        worker.write_json(self.packet_path, {**packet(), "scope_paths": ["src"]})
        with self.preflight(), mock.patch.object(worker, "execute") as execute:
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "blocked")
        execute.assert_not_called()

    def test_failure_categories_are_machine_readable_and_do_not_retry(self):
        examples = {"timeout": "Task timed out after 5 seconds", "quota": "429 rate limit exceeded",
                    "auth": "ChatGPT auth not verified", "permission": "Access is denied by permission",
                    "unsupported_model": "Model 'unlisted' is absent from the official account cache",
                    "validation": "Actual snapshot changes fall outside authorized scope"}
        for expected, message in examples.items():
            with self.subTest(kind=expected):
                failure = worker.blocked_result(message)
                self.assertEqual(failure["failure_kind"], expected)
                self.assertTrue(failure["next_action"])
                self.assertIsInstance(failure["retryable"], bool)
        with self.preflight(), mock.patch.object(worker, "execute", side_effect=worker.WorkerError("429 quota exceeded")) as execute:
            saved = worker.run_task(self.args)
        self.assertEqual(saved["failure_kind"], "quota")
        execute.assert_called_once()

    def test_explicit_review_and_attempt_keep_linkage_and_readonly_contract(self):
        with self.preflight():
            prior = worker.run_task(self.args)
        worker.write_json(self.packet_path, {**packet("review"), "review_of": prior["job_id"], "parent_job": prior["job_id"]})
        with self.preflight():
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["permission"], "read-only")
        self.assertEqual(saved["review_of"], prior["job_id"])
        self.assertEqual(saved["attempt"], 2)
        self.assertEqual(saved["result"]["author_provider"], "codex")
        with self.assertRaises(worker.WorkerError):
            worker.validate_packet({**packet("implement"), "review_of": prior["job_id"]}, self.workspace)

    def test_claude_external_review_reference_does_not_require_codex_job(self):
        worker.write_json(self.packet_path, {**packet("review"), "author_provider": "claude", "review_of": "a17c028f-claude-agent"})
        with self.preflight():
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["author_provider"], "claude")
        self.assertEqual(saved["execution_provider"], "codex")
        self.assertFalse(saved["review_reference"]["local_state_verified"])
        worker.write_json(self.packet_path, {**packet("review"), "author_provider": "claude", "parent_job": "missing-job"})
        with self.preflight(), mock.patch.object(worker, "execute") as execute:
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "blocked")
        execute.assert_not_called()

    def test_codex_review_reads_prior_candidate_and_patch_without_source_fallback(self):
        source = self.workspace / "example.py"
        source.write_text("x = 1\n", encoding="utf-8")
        self.implement()
        def implement(*args):
            self.fake_execute(*args)
            (args[2] / "example.py").write_text("x = 2\n", encoding="utf-8")
            return 0
        with self.preflight(implement):
            prior = worker.run_task(self.args)
        self.assertEqual(prior["status"], "complete")
        self.assertEqual(source.read_text(), "x = 1\n")
        worker.write_json(self.packet_path, {**packet("review"), "author_provider": "codex", "review_of": prior["job_id"]})
        def review(*args):
            self.assertEqual((args[2] / "example.py").read_text(), "x = 2\n")
            evidence = (args[2] / ".savvy-review-evidence" / "changes.patch").read_text()
            self.assertIn("-x = 1", evidence)
            self.assertIn("+x = 2", evidence)
            self.assertIn(prior["job_id"], args[1])
            return self.fake_execute(*args)
        with self.preflight(review):
            saved = worker.run_task(self.args)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["review_source_job"], prior["job_id"])
        self.assertEqual(source.read_text(), "x = 1\n")
        self.assertEqual(Path(saved["artifacts"]["review_source_patch"]).read_bytes(), Path(prior["artifacts"]["patch"]).read_bytes())
        # A test available only in the original tree is not silently substituted.
        (self.workspace / "test_example.py").write_text("original tree only", encoding="utf-8")
        worker.write_json(self.packet_path, {**packet("review"), "author_provider": "codex", "review_of": prior["job_id"], "scope_paths": ["example.py", "test_example.py"]})
        with self.preflight(), mock.patch.object(worker, "execute") as execute:
            missing = worker.run_task(self.args)
        self.assertEqual(missing["status"], "blocked")
        self.assertIn("Missing review candidate input", missing["summary"])
        execute.assert_not_called()

    def test_export_rejects_late_outside_write_and_never_writes_patch(self):
        run = self.root / "snapshot-run"
        run.mkdir()
        snapshot = worker.scope_snapshot.prepare(self.workspace, [self.workspace / "example.py"], run, ["example.py"])
        execution = Path(snapshot["path"])
        (execution / "example.py").write_text("validated change\n", encoding="utf-8")
        changes, rejected = worker.scope_snapshot.inspect_changes(snapshot)
        self.assertFalse(rejected)
        (execution / "outside.txt").write_text("late child output\n", encoding="utf-8")
        with self.assertRaises(worker.scope_snapshot.SnapshotError):
            worker.scope_snapshot.export(snapshot, run, changes)
        self.assertFalse((run / "changes.patch").exists())

    def test_export_frozen_index_uses_only_validated_literal_paths(self):
        run = self.root / "snapshot-run"
        run.mkdir()
        snapshot = worker.scope_snapshot.prepare(self.workspace, [self.workspace / "example.py"], run, ["example.py"])
        execution = Path(snapshot["path"])
        (execution / "example.py").write_text("validated change\n", encoding="utf-8")
        changes, _ = worker.scope_snapshot.inspect_changes(snapshot)
        original = worker.scope_snapshot.git_command
        injected = False
        def late_write(*args):
            nonlocal injected
            output = original(*args)
            if "add" in args and not injected:
                injected = True
                (execution / "outside.txt").write_text("after staging\n", encoding="utf-8")
            return output
        with mock.patch.object(worker.scope_snapshot, "git_command", side_effect=late_write), self.assertRaises(worker.scope_snapshot.SnapshotError):
            worker.scope_snapshot.export(snapshot, run, changes)
        self.assertFalse((run / "changes.patch").exists())
        paths = original(execution, Path(snapshot["git_dir"]), "diff", "--cached", "--name-only", snapshot["baseline_commit"]).decode()
        self.assertEqual(paths.strip(), "example.py")

    def cleanup_args(self, dry_run=False):
        return argparse.Namespace(workspace=str(self.workspace), state_root=self.args.state_root, days=30, dry_run=dry_run)

    def old_job(self, status="complete"):
        _, _, saved = worker.initialize_run(self.args)
        saved.update(status=status, controller_pid=None, child_pid=None,
                     finished_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat())
        worker.write_json(saved["artifacts"]["state"], saved)
        return saved

    def test_cleanup_deletes_only_expired_terminal_jobs_and_dry_run_is_readonly(self):
        old = self.old_job()
        active = self.old_job("running")
        unknown = self.old_job("new_status")
        with mock.patch.object(worker, "discover_cli") as cli:
            dry = worker.cleanup(self.cleanup_args(True))
            self.assertEqual(dry["would_delete"], [old["run_dir"]])
            self.assertTrue(Path(old["run_dir"]).exists())
            actual = worker.cleanup(self.cleanup_args())
            cli.assert_not_called()
        self.assertEqual(actual["deleted"], [old["run_dir"]])
        self.assertTrue(Path(active["run_dir"]).exists())
        self.assertTrue(Path(unknown["run_dir"]).exists())

    def test_cleanup_protects_live_terminal_linked_worktree_external_paths_and_latest(self):
        live, linked, external, latest = (self.old_job() for _ in range(4))
        live["controller_pid"] = os.getpid()
        worker.write_json(live["artifacts"]["state"], live)
        (Path(linked["run_dir"]) / ".git").write_text("gitdir: outside", encoding="utf-8")
        external["execution_workspace"] = {"path": str(self.workspace), "strategy": "scope_snapshot"}
        worker.write_json(external["artifacts"]["state"], external)
        worker.write_json(Path(latest["run_dir"]).parent / "latest.json", {"job_id": latest["job_id"]})
        result = worker.cleanup(self.cleanup_args())
        self.assertEqual(result["deleted"], [])
        self.assertEqual({entry["reason"] for entry in result["skipped"]},
                         {"recorded_process_alive", "linked_worktree", "linked_or_external_workspace", "protected_latest_reference"})

    def test_cleanup_rejects_symlinked_hash_directory_and_state_root_ancestors(self):
        saved = self.old_job()
        real_root = Path(self.args.state_root)
        linked_root = self.root / "linked-runs"
        try:
            linked_root.symlink_to(real_root, target_is_directory=True)
        except OSError:
            self.skipTest("This account cannot create symbolic links.")
        args = self.cleanup_args()
        args.state_root = str(linked_root)
        with self.assertRaises(worker.WorkerError):
            worker.cleanup(args)
        digest = Path(saved["run_dir"]).parent.name
        other_root = self.root / "other-runs"
        other_root.mkdir()
        (other_root / digest).symlink_to(Path(saved["run_dir"]).parent, target_is_directory=True)
        args.state_root = str(other_root)
        with self.assertRaises(worker.WorkerError):
            worker.cleanup(args)
        self.assertTrue(Path(saved["run_dir"]).exists())

    def test_cleanup_skips_malformed_launch_and_timestamp_and_continues(self):
        malformed_launch, malformed_finished, valid = (self.old_job() for _ in range(3))
        worker.write_json(Path(malformed_launch["run_dir"]) / "launch.json", [])
        malformed_finished["finished_at"] = None
        worker.write_json(malformed_finished["artifacts"]["state"], malformed_finished)
        result = worker.cleanup(self.cleanup_args())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["deleted"], [valid["run_dir"]])
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(Path(malformed_launch["run_dir"]).exists())
        self.assertTrue(Path(malformed_finished["run_dir"]).exists())

    def test_doctor_json_has_readiness_checks_and_never_runs_a_model(self):
        with self.preflight(), mock.patch.object(worker, "capture", return_value=subprocess.CompletedProcess([], 0, "git version 2.0", "")), \
             mock.patch.object(worker, "execute") as execute:
            info = worker.doctor()
            execute.assert_not_called()
        self.assertTrue(info["ready"])
        self.assertEqual(set(info["checks"]), {"official_cli", "version", "features", "authentication", "catalog", "git"})
        self.assertEqual(worker.parser().parse_args(["doctor", "--json"]).command, "doctor")
        with self.preflight(), mock.patch.object(worker.shutil, "which", return_value=None):
            info = worker.doctor()
        self.assertFalse(info["ready"])
        self.assertFalse(info["checks"]["git"]["ok"])

    def test_additional_credential_environment_is_not_forwarded(self):
        env = worker.child_environment({"GH_TOKEN": "secret", "AWS_ACCESS_KEY_ID": "secret", "SSH_AUTH_SOCK": "socket",
                                        "SERVICE_PASSWORD": "secret", "PATH": "keep"}, {})
        self.assertEqual(env, {"PATH": "keep", "CLAUDE_CODEX_WORKER": "1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
