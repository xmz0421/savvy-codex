"""Contract tests use temporary directories and mocks; no model is ever invoked."""
import argparse
import contextlib
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
            {"slug": MODEL, "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]}]}
        catalog = worker.normalize_catalog(raw, "cache")
        self.assertNotIn("identity", catalog)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
