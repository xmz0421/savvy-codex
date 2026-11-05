"""Executable isolation/IO regressions; no inference or Claude CLI is invoked."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import codex_delegate as worker
import test_codex_delegate as support


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("value = 4\n")

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, **kwargs):
        run = self.root / ("run-" + str(time.time_ns()))
        run.mkdir()
        return worker.scope_snapshot.prepare(self.source, [self.source / "main.py"], run, ["main.py"], **kwargs)

    def test_actual_dependency_test_executes_inside_snapshot(self):
        # A reproducible project with an already installed local dependency.
        dep = self.source / "node_modules" / "arithmetic"
        dep.mkdir(parents=True)
        (dep / "__init__.py").write_text("def double(x): return 2*x\n")
        (self.source / "AGENTS.md").write_text("Use existing runtime; do not install dependencies.\n")
        (self.source / "test_main.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, 'node_modules')\n"
            "from arithmetic import double\nfrom main import value\n"
            "assert double(value) == 8\n"
            "pathlib.Path('test-results').mkdir()\npathlib.Path('test-results/result.txt').write_text('8')\n")
        snap = self.snapshot(context_paths=["test_main.py"], dependency_paths=["node_modules"], verification_output_paths=["test-results/"])
        before, _ = worker.scope_snapshot.inventory(self.source)
        result = subprocess.run([sys.executable, "-B", "test_main.py"], cwd=snap["path"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(worker.scope_snapshot.inspect_changes(snap), ([], []))
        self.assertEqual(before, worker.scope_snapshot.inventory(self.source)[0])
        self.assertEqual(snap["dependency_copy"]["files"], 1)
        self.assertGreater(snap["dependency_copy"]["bytes"], 0)
        self.assertEqual(snap["implicit_context_paths"], ["AGENTS.md"])
        (Path(snap["path"]) / "node_modules/arithmetic/__init__.py").write_text("changed")
        self.assertIn("node_modules/arithmetic/__init__.py", worker.scope_snapshot.inspect_changes(snap)[1])

    def test_context_mutation_is_rejected_and_output_never_enters_patch(self):
        (self.source / "spec.md").write_text("immutable")
        snap = self.snapshot(context_paths=["spec.md"], verification_output_paths=["result.txt"])
        path = Path(snap["path"])
        (path / "main.py").write_text("value = 5\n")
        (path / "result.txt").write_text("passed")
        changes, rejected = worker.scope_snapshot.inspect_changes(snap)
        self.assertFalse(rejected)
        patch = worker.scope_snapshot.export(snap, path.parent, changes).read_text()
        self.assertIn("main.py", patch)
        self.assertNotIn("result.txt", patch)
        (path / "spec.md").unlink()
        self.assertIn("spec.md", worker.scope_snapshot.inspect_changes(snap)[1])

    def test_real_python_tempfile_uses_explicit_snapshot_output_directory(self):
        (self.source / "test_temp.py").write_text(
            "import tempfile,pathlib,os\n"
            "output=pathlib.Path(os.environ['SAVVY_VERIFICATION_OUTPUT_DIR']).resolve()\n"
            "with tempfile.TemporaryDirectory() as directory:\n"
            " path=pathlib.Path(directory).resolve()\n"
            " assert output in path.parents\n"
            " (path/'probe.txt').write_text('passed')\n"
            "(output/'result.txt').write_text('tempfile passed')\n")
        before, _ = worker.scope_snapshot.inventory(self.source)
        snap = self.snapshot(context_paths=["test_temp.py"], verification_output_paths=["results/"])
        env, temporary = worker.verification_environment(snap, "workspace-write", dict(os.environ))
        self.assertEqual(temporary, "results/tmp")
        self.assertEqual(env["TMP"], env["TMPDIR"])
        self.assertEqual(env["TEMP"], env["TMPDIR"])
        result = subprocess.run([sys.executable, "-B", "test_temp.py"], cwd=snap["path"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (Path(snap["path"]) / "main.py").write_text("value = 6\n")
        changes, rejected = worker.scope_snapshot.inspect_changes(snap)
        self.assertFalse(rejected)
        patch = worker.scope_snapshot.export(snap, Path(snap["path"]).parent, changes).read_text()
        self.assertNotIn("results", patch)
        self.assertEqual(before, worker.scope_snapshot.inventory(self.source)[0])

    def test_no_temp_write_authorization_for_readonly_or_file_only_outputs(self):
        env = {"TEMP": "existing-system-temp"}
        for outputs, permission in (([], "workspace-write"), (["result.txt"], "workspace-write"), (["results/"], "read-only")):
            snap = self.snapshot(verification_output_paths=outputs)
            result, temporary = worker.verification_environment(snap, permission, env)
            self.assertIsNone(temporary)
            self.assertEqual(result, env)
            self.assertFalse((Path(snap["path"]) / "results").exists())

    def test_explicit_context_write_overlap_rejected(self):
        for kwargs in ({"context_paths": ["main.py"]}, {"dependency_paths": ["main.py"]},
                       {"verification_output_paths": ["main.py"]}, {"context_paths": ["."]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(worker.scope_snapshot.SnapshotError):
                self.snapshot(**kwargs)

    def test_implicit_instructions_only_explicit_file_scope_can_edit(self):
        (self.source / "AGENTS.md").write_text("instructions")
        snap = self.snapshot()
        (Path(snap["path"]) / "AGENTS.md").write_text("changed")
        self.assertIn("AGENTS.md", worker.scope_snapshot.inspect_changes(snap)[1])
        run = self.root / "instruction-run"
        run.mkdir()
        snap = worker.scope_snapshot.prepare(self.source, [self.source / "AGENTS.md"], run, ["AGENTS.md"])
        self.assertNotIn("AGENTS.md", snap["read_only_paths"])

    def test_shared_policy_accepts_ordinary_source_names(self):
        for name in ("tokens.ts", "secret.md", "credentials.py", ".environment"):
            self.assertFalse(worker.scope_snapshot.sensitive(name), name)
        for name in (".env.local", "auth.json", ".ssh/id_rsa", "private.key"):
            self.assertTrue(worker.scope_snapshot.sensitive(name), name)
        for name in ("token/module.py", "config/.env.local/file.txt", "keys/private.key/info.txt"):
            self.assertTrue(worker.scope_snapshot.sensitive(name), name)

    def test_large_dependency_metadata_is_sampled_without_weakening_hash_enforcement(self):
        snap = self.snapshot()
        snap["read_only_paths"] = [f"node_modules/pkg/module-{index}.py" for index in range(20_000)]
        snap["read_only_paths"][-1] = "main.py"
        snap["omitted_paths"] = [f"excluded/file-{index}" for index in range(20_000)]
        summary = worker.snapshot_state(snap)
        self.assertEqual(summary["read_only_file_count"], 20_000)
        self.assertEqual(summary["omitted_path_count"], 20_000)
        self.assertEqual(len(summary["read_only_paths"]), 100)
        self.assertTrue(summary["read_only_paths_truncated"])
        self.assertTrue(summary["omitted_paths_truncated"])
        self.assertLess(len(json.dumps(summary).encode()), 256 * 1024)
        self.assertEqual(len(snap["read_only_paths"]), 20_000)
        self.assertNotIn("main.py", summary["read_only_paths"])
        (Path(snap["path"]) / "main.py").write_text("tampered")
        self.assertIn("main.py", worker.scope_snapshot.inspect_changes(snap)[1])

    def test_unique_atomic_writes_and_threads_hold_shared_read(self):
        path = self.root / "state.json"
        worker.write_json(path, {"n": 0})
        with worker.shared_read(path) as held:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n: worker.write_json(path, {"n": n}), range(30)))
            self.assertEqual(json.loads(held.read()), {"n": 0})
        self.assertIn("n", worker.read_json(path))
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_process_holds_reader_while_atomic_replace_succeeds(self):
        path = self.root / "state.json"
        worker.write_json(path, {"n": 0})
        script = ("import sys;sys.path.insert(0,sys.argv[1]);import codex_delegate as w;"
                  "h=w.shared_read(sys.argv[2]);f=h.__enter__();print('ready',flush=True);"
                  "sys.stdin.readline();h.__exit__(None,None,None)")
        child = subprocess.Popen([sys.executable, "-c", script, str(Path(worker.__file__).parent), str(path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            for n in range(10):
                worker.write_json(path, {"n": n})
            out, err = child.communicate("done\n", timeout=10)
            self.assertEqual(child.returncode, 0, err)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_retry_and_transient_progress_failure_does_not_abort(self):
        path = self.root / "state.json"
        real = os.replace
        with mock.patch.object(worker, "atomic_replace", side_effect=[PermissionError("locked"), PermissionError("locked"), None]) as replace:
            worker.write_json(path, {"n": 1})
            self.assertEqual(replace.call_count, 3)
        state = {"artifacts": {"state": str(path)}}
        with mock.patch.object(worker, "write_json", side_effect=PermissionError("held")):
            worker.save_progress(state)
        self.assertIn("state_write_warning", state)

    def test_positive_cache_fingerprints_auth_config_cli_and_ttl(self):
        home = self.root / "home"
        home.mkdir()
        (home / "auth.json").write_text('{"token":"initial-not-printed"}')
        cli = self.root / "fake-cli"
        cli.write_text("test")
        cache = self.root / "cache"
        cache.mkdir()
        with mock.patch.object(worker, "codex_home", return_value=home), \
             mock.patch.object(worker, "verify_effective_sandbox", return_value={"verified": True}), \
             mock.patch.object(worker, "capabilities", return_value={"version": "test"}) as caps, \
             mock.patch.object(worker, "check_auth", return_value={"verified": True}) as auth:
            for _ in range(2):
                worker.preflight([str(cli)], {}, cache)
            self.assertEqual(caps.call_count, 1)
            self.assertEqual(auth.call_count, 1)
            (home / "auth.json").write_text('{"token":"changed-not-printed"}')
            worker.preflight([str(cli)], {}, cache)
            self.assertEqual(auth.call_count, 2)
            (home / "config.toml").write_text('sandbox_mode="danger-full-access"')
            worker.preflight([str(cli)], {}, cache)
            self.assertEqual(auth.call_count, 3)
            cli.write_text("test-new-version")
            worker.preflight([str(cli)], {}, cache)
            self.assertEqual(auth.call_count, 4)
            self.assertNotIn("changed-not-printed", (cache / "preflight-cache.json").read_text())
            saved = worker.read_json(cache / "preflight-cache.json")
            saved["created"] -= worker.PREFLIGHT_TTL + 1
            worker.write_json(cache / "preflight-cache.json", saved)
            worker.preflight([str(cli)], {}, cache)
            self.assertEqual(auth.call_count, 5)
            (home / "auth.json").write_text("changed")
            auth.side_effect = worker.WorkerError("auth failed")
            with self.assertRaises(worker.WorkerError):
                worker.preflight([str(cli)], {}, cache)
            self.assertFalse((cache / "preflight-cache.json").exists())

    def test_two_independent_jobs_same_project_execute_concurrently(self):
        fixture = support.WorkerTests()
        fixture.setUp()
        try:
            barrier = threading.Barrier(2)
            def execute(*args, **kwargs):
                barrier.wait(timeout=10)
                return fixture.fake_execute(*args, **kwargs)
            with fixture.preflight(execute=execute), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                states = list(pool.map(lambda _: worker.run_task(fixture.args), range(2)))
            self.assertEqual([state["status"] for state in states], ["complete", "complete"])
            self.assertNotEqual(states[0]["run_dir"], states[1]["run_dir"])
        finally:
            fixture.tearDown()

    def test_same_job_initialization_race_has_exactly_one_owner(self):
        fixture = support.WorkerTests()
        fixture.setUp()
        try:
            fixture.args.job_id = "same-job"
            barrier = threading.Barrier(2)
            def initialize(_):
                barrier.wait(timeout=5)
                try:
                    return worker.initialize_run(fixture.args)[2]
                except (OSError, worker.WorkerError):
                    return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(initialize, range(2)))
            self.assertEqual(sum(result is not None for result in results), 1)
            saved = next(result for result in results if result is not None)
            self.assertEqual(worker.read_json(saved["artifacts"]["state"])["job_id"], "same-job")
        finally:
            fixture.tearDown()

    def test_modern_permissions_config_fails_closed_without_cli(self):
        home = self.root / "config-home"
        home.mkdir()
        for content in ('default_permissions=":danger-full-access"', '[profiles.custom]\ndefault_permissions=":danger-full-access"',
                        '[permissions.wide]\nnetwork=true'):
            (home / "config.toml").write_text(content)
            with self.subTest(content=content), mock.patch.object(worker, "codex_home", return_value=home), self.assertRaises(worker.WorkerError):
                worker.validate_sandbox_configuration()

    def test_readonly_output_request_is_blocked_and_metadata_survives(self):
        fixture = support.WorkerTests()
        fixture.setUp()
        try:
            value = support.packet()
            value["verification_output_paths"] = ["results/"]
            with self.assertRaises(worker.WorkerError):
                worker.validate_packet(value, fixture.workspace)
            value.pop("verification_output_paths")
            value.update(title="Named plan item", plan_task_id="task-3", context_paths=[])
            worker.write_json(fixture.packet_path, value)
            with fixture.preflight():
                state = worker.run_task(fixture.args)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["title"], "Named plan item")
            self.assertEqual(state["result"]["plan_task_id"], "task-3")
            self.assertEqual(state["authentication"]["scope"], "local_login_status")
            self.assertFalse(state["preflight_cache"]["enabled"])
        finally:
            fixture.tearDown()

    def test_directory_scope_instructions_remain_immutable(self):
        directory = self.source / "src"
        (directory / "nested").mkdir(parents=True)
        (directory / "AGENTS.md").write_text("instructions")
        (directory / "nested/CLAUDE.md").write_text("instructions")
        run = self.root / "directory-run"
        run.mkdir()
        snap = worker.scope_snapshot.prepare(self.source, [directory], run, ["src/"])
        self.assertEqual(snap["implicit_context_paths"], ["src/AGENTS.md", "src/nested/CLAUDE.md"])
        (Path(snap["path"]) / "src/nested/CLAUDE.md").write_text("changed")
        self.assertIn("src/nested/CLAUDE.md", worker.scope_snapshot.inspect_changes(snap)[1])

    def test_canonical_context_alias_cannot_bypass_write_scope_overlap(self):
        (self.source / "src").mkdir()
        (self.source / "src/main.py").write_text("original")
        (self.source / "docs").mkdir()
        value = support.packet("implement")
        value.update(scope_paths=["src/main.py"], context_paths=["docs/../src"])
        scopes, _, _ = worker.validate_packet(value, self.source)
        self.assertEqual(value["context_paths"], ["src"])
        run = self.root / "alias-run"
        run.mkdir()
        with self.assertRaises(worker.scope_snapshot.SnapshotError):
            worker.scope_snapshot.prepare(self.source, scopes, run, value["scope_paths"], context_paths=value["context_paths"])

    def test_absolute_input_output_paths_relocate_to_snapshot(self):
        (self.source / "spec.txt").write_text("immutable")
        value = support.packet("implement")
        value.update(scope_paths=["main.py"], context_paths=[str(self.source / "spec.txt")],
                     verification_output_paths=[str(self.source / "results") + "/"])
        worker.validate_packet(value, self.source)
        self.assertEqual(value["context_paths"], ["spec.txt"])
        self.assertEqual(value["verification_output_paths"], ["results/"])
        snap = self.snapshot(context_paths=value["context_paths"], verification_output_paths=value["verification_output_paths"])
        (Path(snap["path"]) / "results").mkdir()
        (Path(snap["path"]) / "results/check.txt").write_text("pass")
        self.assertEqual(worker.scope_snapshot.inspect_changes(snap), ([], []))

    def test_keyring_login_status_is_never_cached(self):
        home = self.root / "home"
        home.mkdir()
        cli = self.root / "cli"
        cli.write_text("test")
        directory = self.root / "runs"
        directory.mkdir()
        with mock.patch.object(worker, "codex_home", return_value=home), \
             mock.patch.object(worker, "verify_effective_sandbox", return_value={"verified": True}), \
             mock.patch.object(worker, "capabilities", return_value={"version": "test"}) as caps, \
             mock.patch.object(worker, "check_auth", return_value={"verified": True}) as auth:
            worker.preflight([str(cli)], {}, directory)
            worker.preflight([str(cli)], {}, directory)
            self.assertEqual(caps.call_count, 1)
            self.assertEqual(auth.call_count, 2)
            auth.side_effect = worker.WorkerError("keyring login changed")
            with self.assertRaises(worker.WorkerError):
                worker.preflight([str(cli)], {}, directory)
            self.assertFalse((directory / "preflight-cache.json").exists())

    def test_config_read_rpc_is_no_model_and_rejects_effective_modern_policy(self):
        script = self.root / "fake_server.py"
        script.write_text("import sys,json,os\nfor line in sys.stdin:\n"
                          " v=json.loads(line)\n"
                          " if v.get('method')=='initialize': print(json.dumps({'id':v['id'],'result':{}}),flush=True)\n"
                          " if v.get('method')=='config/read':\n"
                          "  c={'sandbox_mode':'workspace-write','sandbox_workspace_write':{'writable_roots':[],'network_access':False,'exclude_tmpdir_env_var':True,'exclude_slash_tmp':True}}\n"
                          "  if os.environ.get('TEST_MODERN'): c['default_permissions']=':danger-full-access'\n"
                          "  print(json.dumps({'id':v['id'],'result':{'config':c}}),flush=True)\n")
        result = worker.verify_effective_sandbox([sys.executable, str(script)], dict(os.environ))
        self.assertTrue(result["verified"])
        self.assertEqual(result["model_turns"], 0)
        env = dict(os.environ, TEST_MODERN="1")
        with self.assertRaises(worker.WorkerError):
            worker.verify_effective_sandbox([sys.executable, str(script)], env)

    @unittest.skipUnless(os.environ.get("SAVVY_CLI_INTEGRATION") == "1", "opt-in installed CLI, no model turn")
    def test_installed_cli_help_and_effective_legacy_and_modern_policies(self):
        cli = worker.discover_cli()
        home = self.root / "cli-home"
        home.mkdir()
        env = worker.child_environment()
        env["CODEX_HOME"] = str(home)
        help_result = worker.capture(cli + worker.write_sandbox_arguments() + ["exec", "--help"], env)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        (home / "config.toml").write_text('sandbox_mode="danger-full-access"\n[sandbox_workspace_write]\nwritable_roots=["C:/outside-source-workspace"]\n')
        self.assertTrue(worker.verify_effective_sandbox(cli, env)["verified"])
        (home / "config.toml").write_text('default_permissions=":danger-full-access"\n')
        with self.assertRaises(worker.WorkerError):
            worker.verify_effective_sandbox(cli, env)


if __name__ == "__main__":
    unittest.main()
