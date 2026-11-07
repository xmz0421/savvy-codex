"""macOS CLI discovery and precise cache-warning regressions; no model calls."""
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest
from unittest import mock

import codex_delegate as worker


CACHE_WARNING = "git: error: couldn't create cache file '/tmp/xcrun_db-AB12_cd' (errno=Operation not permitted)"


class MacCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="savvy-mac-test-")
        self.root = Path(self.temporary.name).resolve()
        self.events = self.root / "events.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, command="/usr/bin/git --version", output=None, **fields):
        item = {"type": "command_execution", "status": "completed", "exit_code": 0,
                "command": command, "aggregated_output": CACHE_WARNING + "\ngit version 2.50.0\n" if output is None else output}
        item.update(fields)
        return {"type": "item.completed", "item": item}

    def evidence(self, event, system="Darwin"):
        events = event if isinstance(event, list) else [event]
        self.events.write_text("".join(json.dumps(value) + "\n" for value in events), encoding="utf-8")
        with mock.patch.object(worker.platform, "system", return_value=system):
            return worker.event_evidence(self.events)

    def assess(self, evidence, kind="inspect", returncode=0, stderr="", tools=None):
        response = {"status": "complete", "summary": "Read the scoped module and verified its exported function.",
                    "changed_files": [], "checks": ["Scoped command completed with exit 0."], "findings": [], "limitations": []}
        packet = {"kind": kind, "required_tools": ["exec_command"] if tools is None else tools}
        return worker.assess_result(response, evidence, packet, self.root, [self.root / "main.py"],
                                    "read-only" if kind in {"inspect", "review"} else "workspace-write", returncode, stderr)

    def test_successful_git_cache_warning_is_visible_without_authorizing_writes(self):
        for kind in ("inspect", "review", "implement"):
            with self.subTest(kind=kind):
                event = self.event()
                evidence = self.evidence(event)
                self.assertEqual(evidence["successful_tools"], {"exec_command"})
                self.assertEqual(evidence["denials"], [])
                self.assertEqual(evidence["platform_warnings"], [{"kind": "xcrun_cache", "command": "/usr/bin/git --version",
                                                                  "message": CACHE_WARNING, "exit_code": 0}])
                result = self.assess(evidence, kind)
                self.assertEqual(result["status"], "complete", result)
                self.assertEqual(result["platform_warnings"], evidence["platform_warnings"])
                self.assertIn(CACHE_WARNING, self.events.read_text())
                self.assertEqual(worker.sandbox_policy("read-only")["writable_roots"], [])
                self.assertTrue(worker.sandbox_policy("read-only")["exclude_slash_tmp"])

    def test_supported_single_commands_and_shell_wrappers(self):
        for command in ("git status --short", "/usr/bin/xcrun --find git", "xcrun git --version",
                        "/bin/zsh -lc '/usr/bin/git --version'", '/bin/sh -c "git -C \'a path\' diff --check"'):
            with self.subTest(command=command):
                output = CACHE_WARNING.replace("git: error:", "xcrun: warning:") + "\n"
                self.assertEqual(self.assess(self.evidence(self.event(command, output)))["status"], "complete")

    def test_unknown_or_compound_command_never_receives_cache_exception(self):
        for command in (None, "python check.py", "/other/git --version", "env git --version",
                        "git --version;echo done", "git --version && true", "git --version | cat",
                        "git --version\necho done", "git --version >file", 'git "$(echo --version)"',
                        "git `echo --version`", "/bin/zsh -lc 'git --version; true'", "git 'unterminated"):
            with self.subTest(command=command):
                evidence = self.evidence(self.event(command))
                self.assertFalse(evidence["platform_warnings"])
                self.assertEqual(self.assess(evidence)["status"], "blocked")

    def test_warning_requires_darwin_completed_command_and_real_exit_zero(self):
        for fields, event_type, system in (({"exit_code": 1}, "item.completed", "Darwin"),
                                            ({"exit_code": None}, "item.completed", "Darwin"),
                                            ({"exit_code": False}, "item.completed", "Darwin"),
                                            ({"status": "failed"}, "item.completed", "Darwin"),
                                            ({"error": "tool execution failed"}, "item.completed", "Darwin"),
                                            ({"result": {"isError": True}}, "item.completed", "Darwin"),
                                            ({}, "item.started", "Darwin"),
                                            ({}, "item.completed", "Linux"), ({}, "item.completed", "Windows")):
            with self.subTest(fields=fields, event_type=event_type, system=system):
                event = self.event(**fields)
                event["type"] = event_type
                evidence = self.evidence(event, system)
                self.assertFalse(evidence["platform_warnings"])
                self.assertEqual(self.assess(evidence)["status"], "blocked")

    def test_other_or_mixed_denial_never_disappears(self):
        for output in (CACHE_WARNING + "\nopen secret.txt: Operation not permitted\n",
                       CACHE_WARNING + "\nsandbox tool was denied\n",
                       "prefix " + CACHE_WARNING,
                       CACHE_WARNING + " extra error detail",
                       CACHE_WARNING.replace("/tmp/xcrun_db-AB12_cd", "/tmp/other-cache"),
                       CACHE_WARNING.replace("Operation not permitted", "Permission denied")):
            with self.subTest(output=output):
                result = self.assess(self.evidence(self.event(output=output)))
                self.assertEqual(result["status"], "blocked")
                self.assertIn("denial", " ".join(result["limitations"]))

    def test_cli_stderr_has_no_command_provenance_and_still_blocks(self):
        evidence = self.evidence(self.event())
        result = self.assess(evidence, stderr=CACHE_WARNING)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["platform_warnings"])

    def test_warning_does_not_satisfy_missing_required_tool_or_failed_cli(self):
        evidence = self.evidence(self.event())
        self.assertEqual(self.assess(evidence, tools=["apply_patch"])["status"], "blocked")
        self.assertEqual(self.assess(evidence, returncode=1)["status"], "blocked")
        missing = self.evidence(self.event(exit_code=1))
        self.assertNotIn("exec_command", missing["successful_tools"])

    def test_report_preserves_platform_warning(self):
        result = self.assess(self.evidence(self.event()))
        report = self.root / "report.md"
        state = {"status": "complete", "selected_model": "test", "selected_effort": "medium",
                 "workspace": str(self.root), "artifacts": {"report": str(report)}}
        worker.write_report(state, result)
        self.assertIn(CACHE_WARNING, report.read_text())
        self.assertIn("successful command: /usr/bin/git --version", report.read_text())

    def test_repeated_warnings_are_globally_bounded_without_false_denials(self):
        events = [self.event(output=CACHE_WARNING + "\n" + CACHE_WARNING + "\n") for _ in range(30)]
        evidence = self.evidence(events)
        self.assertEqual(evidence["platform_warnings_total"], 60)
        self.assertEqual(len(evidence["platform_warnings"]), 20)
        self.assertEqual(evidence["platform_warnings_truncated"], 40)
        self.assertEqual(evidence["platform_warnings_omitted"], 40)
        self.assertFalse(evidence["denials"])
        result = self.assess(evidence)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["platform_warnings_total"], 60)
        self.assertEqual(self.events.read_text().count("couldn't create cache file"), 60)
        report = self.root / "report.md"
        state = {"status": "complete", "selected_model": "test", "selected_effort": "medium",
                 "workspace": str(self.root), "artifacts": {"report": str(report)}}
        worker.attach_result_metadata(state, result)
        self.assertEqual(state["platform_warnings_total"], 60)
        self.assertEqual(state["platform_warnings_truncated"], 40)
        worker.write_report(state, result)
        self.assertIn("40 of 60", report.read_text())
        self.assertIn("original output remains", report.read_text())
        later_failure = worker.blocked_result("Snapshot validation failed.")
        worker.attach_result_metadata(state, later_failure)
        self.assertEqual(later_failure["platform_warnings_total"], 60)
        self.assertEqual(later_failure["platform_warnings_truncated"], 40)

    def test_long_warning_details_are_bounded_and_raw_events_retained(self):
        command = "git show " + "x" * 12000
        message = CACHE_WARNING.replace("AB12_cd", "a" * 2000)
        evidence = self.evidence(self.event(command, message))
        result = self.assess(evidence)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["platform_warnings"][0]["command"]), 2000)
        self.assertEqual(len(result["platform_warnings"][0]["message"]), 1200)
        self.assertEqual(result["platform_warnings_total"], 1)
        self.assertEqual(result["platform_warnings_truncated"], 1)
        self.assertEqual(result["platform_warnings_omitted"], 0)
        raw = self.events.read_text()
        self.assertIn(command, raw)
        self.assertIn(message, raw)

    def test_warning_byte_budget_covers_multibyte_commands_and_nested_state(self):
        command = "git show " + "🌍" * 3000
        evidence = self.evidence([self.event(command) for _ in range(30)])
        self.assertEqual(evidence["platform_warnings_total"], 30)
        self.assertLess(len(evidence["platform_warnings"]), 20)
        self.assertEqual(evidence["platform_warnings_truncated"], 30)
        self.assertEqual(evidence["platform_warnings_omitted"], 30 - len(evidence["platform_warnings"]))
        self.assertLessEqual(evidence["platform_warning_bytes"], 32 * 1024)
        result = self.assess(evidence)
        state = {"artifacts": {}}
        worker.attach_result_metadata(state, result)
        state["result"] = result
        self.assertLess(len(json.dumps(state, ensure_ascii=False).encode("utf-8")), 70 * 1024)
        self.assertEqual(result["status"], "complete")

    def candidates(self):
        relative = Path("ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
        return [self.root / "Applications" / relative, self.root / "a user" / "Applications" / relative]

    def bundle(self, candidate):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o755)

    def discover(self, candidates, available=None, executable=True, system="Darwin"):
        with mock.patch.object(worker.platform, "system", return_value=system), \
             mock.patch.object(worker.shutil, "which", side_effect=(available or {}).get), \
             mock.patch.object(worker, "macos_cli_candidates", return_value=candidates), \
             mock.patch.object(worker.os, "access", return_value=executable):
            return worker.discover_cli()

    def test_macos_bundle_is_an_argv_entry_with_spaces_and_system_app_precedence(self):
        candidates = self.candidates()
        for candidate in candidates:
            self.bundle(candidate)
        self.assertEqual(self.discover(candidates), [str(candidates[0].resolve())])
        candidates[0].unlink()
        self.assertEqual(self.discover(candidates), [str(candidates[1].resolve())])
        self.assertIn("a user", self.discover(candidates)[0])

    def test_path_native_or_npm_shim_always_wins_over_app(self):
        candidates = self.candidates()
        self.bundle(candidates[0])
        native = self.root / "codex.exe"
        self.assertEqual(self.discover(candidates, {"codex.exe": str(native)}), [str(native.resolve())])
        npm = self.root / "npm"
        entry = npm / "node_modules/@openai/codex/bin/codex.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("// official shim fixture")
        node = self.root / "node"
        selected = self.discover(candidates, {"codex": str(npm / "codex.cmd"), "node": str(node)})
        self.assertEqual(selected, [str(node.resolve()), str(entry.resolve())])

    def test_invalid_existing_path_shim_does_not_silently_fall_back(self):
        candidates = self.candidates()
        self.bundle(candidates[0])
        with self.assertRaisesRegex(worker.WorkerError, "shim found"):
            self.discover(candidates, {"codex.cmd": str(self.root / "missing/codex.cmd")})

    def test_bundle_fallback_does_not_run_on_other_platforms(self):
        candidates = self.candidates()
        self.bundle(candidates[0])
        for system in ("Linux", "Windows"):
            with self.subTest(system=system), self.assertRaisesRegex(worker.WorkerError, "not found on PATH"):
                self.discover(candidates, system=system)

    def test_missing_or_invalid_bundle_has_actionable_discovery_error(self):
        candidates = self.candidates()
        with self.assertRaisesRegex(worker.WorkerError, "checked:"):
            self.discover(candidates)
        self.bundle(candidates[0])
        with self.assertRaisesRegex(worker.WorkerError, "not a regular executable file") as error:
            self.discover(candidates, executable=False)
        self.assertIn(str(candidates[0]), str(error.exception))
        candidates[0].unlink()
        candidates[0].mkdir()
        with self.assertRaisesRegex(worker.WorkerError, "not a regular executable file"):
            self.discover(candidates)
        self.bundle(candidates[1])
        self.assertEqual(self.discover(candidates), [str(candidates[1].resolve())])

    def test_default_bundle_locations(self):
        with mock.patch.object(worker.Path, "home", return_value=self.root):
            self.assertEqual(worker.macos_cli_candidates(), [Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
                             self.root / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox-exec smoke")
    def test_real_system_git_under_deny_write_sandbox(self):
        # This checks an actual system command without invoking Codex or a
        # model. An already warm xcrun cache may produce no cache warning.
        self.assertTrue(Path("/usr/bin/sandbox-exec").is_file(), "macOS smoke requires the system sandbox-exec executable")
        # Apple's Git shim redirects xcodebuild diagnostics to this device.
        # Permit that exact sink while retaining the ordinary-file write deny.
        profile = '(version 1)(allow default)(deny file-write*)(allow file-write* (literal "/dev/null"))'
        result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/git", "--version"],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git version", result.stdout)
        evidence = self.evidence(self.event(output=result.stdout + result.stderr))
        self.assertEqual(self.assess(evidence)["status"], "complete")
        # Demonstrate that the profile still rejects writes.
        forbidden = self.root / "forbidden.txt"
        denied = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", 'printf forbidden > "$1"', "sh", str(forbidden)],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(forbidden.exists())


if __name__ == "__main__":
    unittest.main()
