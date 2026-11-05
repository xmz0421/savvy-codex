"""Installer tests use isolated homes and fake doctor JSON; no model is invoked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("savvy_install", Path(__file__).resolve().parents[1] / "scripts" / "install.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "checkout"
        self.home = self.root / "home"
        self.home.mkdir()
        for name in INSTALLER.CORE:
            target = self.source / name
            if name in ("LICENSE", "NOTICE"):
                target.write_text("MIT", encoding="utf-8")
            else:
                target.mkdir(parents=True)
                (target / "sample.txt").write_text(name, encoding="utf-8")
        (self.source / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "savvy-codex", "version": "2.0.0", "userConfig": {"mode": {"default": "manual"}}}), encoding="utf-8")
        (self.source / ".claude-plugin" / "marketplace.json").write_text("{}", encoding="utf-8")
        cache = self.source / "hooks" / "__pycache__"
        cache.mkdir()
        (cache / "sample.pyc").write_bytes(b"cache")
        (self.source / "hooks" / "loose.pyc").write_bytes(b"cache")
        self.helper = self.source / "skills" / "codex-worker" / "scripts" / "codex_delegate.py"
        self.helper.parent.mkdir(parents=True)
        self.write_doctor({"ready": True, "checks": {"official_cli": {"ok": True}}})
        self.destination = self.home / "skills" / "savvy-codex"

    def write_doctor(self, output, code=0):
        # This fixture is an inference-free subprocess, not a CLI stub that can
        # accidentally fall through to the real CLI/model.
        self.helper.write_text("import json, sys\nassert sys.argv[1:] == ['doctor', '--json']\nprint(" + repr(json.dumps(output)) + ")\nraise SystemExit(" + str(code) + ")\n", encoding="utf-8")

    def run_install(self, workflow=False, *, skip_doctor=True):
        return INSTALLER.install(self.source, self.home, workflow, skip_doctor=skip_doctor)

    def settings(self):
        return json.loads((self.home / "settings.json").read_text(encoding="utf-8"))

    def write_settings(self, value):
        (self.home / "settings.json").write_text(json.dumps(value), encoding="utf-8")

    def snapshot(self):
        return {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}

    def test_narrow_settings_change_and_exact_backups(self):
        settings = {"env": {"PRIVATE_TOKEN": "fixture-only"}, "model": "keep", "provider": "keep", "other": [1], "enabledPlugins": {"other@local": True, "savvy-flow@claude-kit": True}}
        original = json.dumps(settings).encode()
        (self.home / "settings.json").write_bytes(original)
        (self.home / "CLAUDE.md").write_bytes(b"keep global instructions\r\n")
        self.destination.mkdir(parents=True)
        (self.destination / "old.txt").write_text("old", encoding="utf-8")
        result = self.run_install()
        expected = {**settings, "enabledPlugins": {**settings["enabledPlugins"], **INSTALLER.PLUGIN_FLAGS}}
        self.assertEqual(self.settings(), expected)
        backup = Path(result["backup"])
        self.assertEqual((backup / "settings.json").read_bytes(), original)
        self.assertEqual((backup / "savvy-codex" / "old.txt").read_text(), "old")
        self.assertEqual((self.home / "CLAUDE.md").read_bytes(), b"keep global instructions\r\n")
        self.assertFalse((self.destination / ".claude-plugin" / "marketplace.json").exists())
        self.assertFalse((self.destination / "hooks" / "__pycache__").exists())
        self.assertFalse((self.destination / "hooks" / "loose.pyc").exists())
        self.assertTrue((self.destination / "NOTICE").exists())
        receipt = json.loads((self.destination / ".install-info.json").read_text())
        self.assertEqual(receipt["installer_python"], sys.executable)
        self.assertEqual(receipt["plugin_version"], "2.0.0")
        self.assertEqual(receipt["plugin_flags"]["savvy-flow@claude-kit"]["before"], True)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(receipt))

    def test_fresh_skip_doctor_is_explicitly_unverified_and_repeat_noop(self):
        first = self.run_install()
        self.assertFalse(first["ready"])
        self.assertEqual(first["status"], "installed_but_not_ready")
        self.assertTrue(any("skipped" in value for value in first["warnings"]))
        before = self.snapshot()
        second = self.run_install()
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])
        self.assertEqual(before, self.snapshot())

    def test_default_doctor_runs_in_same_python_and_accepts_ready(self):
        result = self.run_install(skip_doctor=False)
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["doctor"]["checks"]["official_cli"]["ok"])

    def test_doctor_failure_has_no_install_or_global_mutations(self):
        self.write_settings({"env": {"keep": "secret"}, "enabledPlugins": {"original": True}})
        (self.home / "CLAUDE.md").write_text(INSTALLER.START + "old global workflow" + INSTALLER.END, encoding="utf-8")
        self.write_doctor({"ready": False, "next_action": "Login with ChatGPT", "failure_kind": "authentication"}, code=1)
        before = self.snapshot()
        with self.assertRaisesRegex(INSTALLER.NotReadyError, "Login with ChatGPT"):
            self.run_install(skip_doctor=False)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.home / "backups").exists())

    def test_doctor_failure_does_not_even_create_a_new_home(self):
        self.home.rmdir()
        self.write_doctor({"ready": False}, code=1)
        with self.assertRaises(INSTALLER.NotReadyError):
            self.run_install(skip_doctor=False)
        self.assertFalse(self.home.exists())

    def test_doctor_nonzero_is_not_success_even_with_ready_json(self):
        self.write_doctor({"ready": True}, code=3)
        with self.assertRaises(INSTALLER.NotReadyError):
            self.run_install(skip_doctor=False)
        self.assertFalse(self.destination.exists())

    def test_doctor_bad_json_and_missing_helper_are_not_success(self):
        for output in ("print('not JSON')", "print('[]')"):
            self.helper.write_text(output, encoding="utf-8")
            with self.assertRaises(INSTALLER.NotReadyError):
                self.run_install(skip_doctor=False)
        self.helper.unlink()
        with self.assertRaises(INSTALLER.NotReadyError):
            self.run_install(skip_doctor=False)

    def test_doctor_timeout_is_not_success(self):
        with mock.patch.object(INSTALLER.subprocess, "run", side_effect=subprocess.TimeoutExpired("doctor", 60)):
            with self.assertRaises(INSTALLER.NotReadyError):
                self.run_install(skip_doctor=False)
        self.assertFalse(self.destination.exists())

    def test_cli_doctor_failure_prints_no_success(self):
        self.write_doctor({"ready": False, "next_action": "Fix CLI version"}, code=1)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = INSTALLER.main(["--source", str(self.source), "--claude-home", str(self.home)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Not installed: not ready", stderr.getvalue())
        self.assertNotIn("Restart", stderr.getvalue())

    def test_existing_install_doctor_failure_reports_installed_but_not_ready(self):
        self.run_install()
        self.write_doctor({"ready": False, "next_action": "Fix CLI version"}, code=1)
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = INSTALLER.main(["--source", str(self.source), "--claude-home", str(self.home)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Installed but not ready; update refused", stderr.getvalue())
        self.assertEqual(before, self.snapshot())

    def test_cli_skip_doctor_and_uninstall(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = INSTALLER.main(["--source", str(self.source), "--claude-home", str(self.home), "--skip-doctor"])
        self.assertEqual(code, 0)
        self.assertIn("Installed but not ready", output.getvalue())
        self.assertNotIn("Installed and ready", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(INSTALLER.main(["--claude-home", str(self.home), "--uninstall"]), 0)
        self.assertFalse(self.destination.exists())

    def test_old_global_workflow_is_removed_with_surroundings_and_backup(self):
        original = b"\xef\xbb\xbfBefore\r\n" + INSTALLER.START.encode() + b"\r\nold preference\r\n" + INSTALLER.END.encode() + b"\r\nAfter\r\n"
        (self.home / "CLAUDE.md").write_bytes(original)
        result = self.run_install()
        self.assertEqual((self.home / "CLAUDE.md").read_bytes(), b"\xef\xbb\xbfBefore\r\n\r\nAfter\r\n")
        self.assertEqual((Path(result["backup"]) / "CLAUDE.md").read_bytes(), original)
        self.assertTrue(any("Removed" in warning for warning in result["warnings"]))

    def test_compat_workflow_option_never_adds_global_instructions(self):
        result = self.run_install(True)
        self.assertFalse((self.home / "CLAUDE.md").exists())
        self.assertTrue(any("deprecated" in warning for warning in result["warnings"]))
        self.assertFalse(self.run_install(True)["changed"])

    def test_invalid_configuration_fails_before_installing(self):
        for invalid in (b"{oops", b"[]", b'{"enabledPlugins": []}'):
            (self.home / "settings.json").write_bytes(invalid)
            with self.assertRaises(ValueError):
                self.run_install()
            self.assertFalse(self.destination.exists())
            self.assertEqual((self.home / "settings.json").read_bytes(), invalid)

    def test_incomplete_duplicate_and_reversed_markers_are_safe(self):
        for invalid in (INSTALLER.START, INSTALLER.END + INSTALLER.START, INSTALLER.START + INSTALLER.START + INSTALLER.END):
            (self.home / "CLAUDE.md").write_text(invalid, encoding="utf-8")
            before = self.snapshot()
            with self.assertRaises(ValueError):
                self.run_install()
            with self.assertRaises(ValueError):
                INSTALLER.uninstall(self.home)
            self.assertEqual(before, self.snapshot())

    def test_upgrade_reports_custom_changes_and_keeps_backup(self):
        self.run_install()
        (self.destination / "stale.txt").write_text("preserve in backup", encoding="utf-8")
        (self.destination / "hooks" / "sample.txt").write_text("user customized", encoding="utf-8")
        (self.source / "hooks" / "sample.txt").write_text("upstream updated", encoding="utf-8")
        upgraded = self.run_install()
        self.assertEqual(upgraded["local_changes"], ["hooks/sample.txt", "stale.txt"])
        self.assertTrue(any("Local package changes" in warning for warning in upgraded["warnings"]))
        backup = Path(upgraded["backup"]) / "savvy-codex"
        self.assertEqual((backup / "stale.txt").read_text(), "preserve in backup")
        self.assertEqual((backup / "hooks" / "sample.txt").read_text(), "user customized")
        self.assertEqual((self.destination / "hooks" / "sample.txt").read_text(), "upstream updated")
        self.assertFalse((self.destination / "stale.txt").exists())

    def test_update_keeps_host_settings_and_external_preferences(self):
        self.run_install()
        settings = self.settings()
        settings.update({"env": {"NEW_TOKEN": "keep"}, "model": "new model", "pluginConfigs": {"savvy-codex": {"mode": "off", "language": "en"}}})
        settings["enabledPlugins"]["added@other"] = True
        self.write_settings(settings)
        prefs = self.home / "savvy-user-preferences.json"
        prefs.write_text('{"custom":"keep"}', encoding="utf-8")
        (self.source / "hooks" / "sample.txt").write_text("upstream", encoding="utf-8")
        self.run_install()
        self.assertEqual(self.settings(), settings)
        self.assertEqual(prefs.read_text(), '{"custom":"keep"}')

    def test_uninstall_restores_only_owned_flags_and_preserves_later_settings(self):
        original_flags = {"savvy-flow@claude-kit": True, "savvy-progress@claude-kit": True, "other@local": True}
        self.write_settings({"env": {"OLD": "old"}, "enabledPlugins": original_flags})
        self.run_install()
        settings = self.settings()
        settings["env"] = {"NEW": "new"}
        settings["model"] = "new user choice"
        settings["enabledPlugins"]["later@local"] = True
        self.write_settings(settings)
        runs = self.home / "codex-worker-runs" / "job"
        runs.mkdir(parents=True)
        (runs / "result.json").write_text("keep records", encoding="utf-8")
        prefs = self.home / "preferences.json"
        prefs.write_text("keep preferences", encoding="utf-8")
        result = INSTALLER.uninstall(self.home)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.settings(), {"env": {"NEW": "new"}, "model": "new user choice", "enabledPlugins": {**original_flags, "later@local": True}})
        self.assertEqual((runs / "result.json").read_text(), "keep records")
        self.assertEqual(prefs.read_text(), "keep preferences")
        self.assertTrue((Path(result["backup"]) / "savvy-codex").is_dir())
        self.assertEqual(json.loads((self.home / INSTALLER.RECEIPT_NAME).read_text())["status"], "uninstalled")

    def test_update_preserves_original_restoration_baseline(self):
        self.write_settings({"enabledPlugins": {"savvy-flow@claude-kit": True}})
        self.run_install()
        (self.source / "hooks" / "sample.txt").write_text("new", encoding="utf-8")
        self.run_install()
        INSTALLER.uninstall(self.home)
        self.assertEqual(self.settings()["enabledPlugins"], {"savvy-flow@claude-kit": True})

    def test_uninstall_does_not_overwrite_flags_changed_after_install(self):
        self.write_settings({"enabledPlugins": {"savvy-flow@claude-kit": True}})
        self.run_install()
        settings = self.settings()
        settings["enabledPlugins"][INSTALLER.IDENTITY] = False
        settings["enabledPlugins"]["savvy-flow@claude-kit"] = True
        self.write_settings(settings)
        result = INSTALLER.uninstall(self.home)
        self.assertEqual(self.settings()["enabledPlugins"], {INSTALLER.IDENTITY: False, "savvy-flow@claude-kit": True})
        self.assertEqual(set(result["preserved_flags"]), {INSTALLER.IDENTITY, "savvy-flow@claude-kit"})

    def test_repeated_uninstall_is_noop_and_keeps_later_user_edit(self):
        self.run_install()
        INSTALLER.uninstall(self.home)
        settings = self.settings()
        settings["enabledPlugins"][INSTALLER.IDENTITY] = True
        self.write_settings(settings)
        before = self.snapshot()
        second = INSTALLER.uninstall(self.home)
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])
        self.assertEqual(before, self.snapshot())

    def test_legacy_uninstall_conservatively_disables_and_preserves_original_savvy(self):
        self.run_install()
        (self.home / INSTALLER.RECEIPT_NAME).unlink()
        settings = self.settings()
        settings["enabledPlugins"]["savvy-flow@claude-kit"] = False
        self.write_settings(settings)
        result = INSTALLER.uninstall(self.home)
        self.assertFalse(self.settings()["enabledPlugins"][INSTALLER.IDENTITY])
        self.assertFalse(self.settings()["enabledPlugins"]["savvy-flow@claude-kit"])
        self.assertTrue(any("manually" in warning for warning in result["warnings"]))

    def test_no_ownership_is_claimed_for_preexisting_desired_flags(self):
        self.write_settings({"enabledPlugins": dict(INSTALLER.PLUGIN_FLAGS)})
        self.run_install()
        receipt = json.loads((self.home / INSTALLER.RECEIPT_NAME).read_text())
        self.assertEqual(receipt["plugin_flags"], {})
        INSTALLER.uninstall(self.home)
        self.assertEqual(self.settings()["enabledPlugins"], {**INSTALLER.PLUGIN_FLAGS, INSTALLER.IDENTITY: False})

    def test_legacy_package_upgrade_uninstall_never_restores_self_true(self):
        self.destination.mkdir(parents=True)
        (self.destination / ".install-info.json").write_text(json.dumps({"plugin_identity": INSTALLER.IDENTITY, "installer_python": sys.executable}), encoding="utf-8")
        (self.destination / "legacy.txt").write_text("old package", encoding="utf-8")
        self.write_settings({"env": {"keep": "value"}, "enabledPlugins": dict(INSTALLER.PLUGIN_FLAGS)})
        self.run_install()
        receipt = json.loads((self.home / INSTALLER.RECEIPT_NAME).read_text())
        self.assertNotIn(INSTALLER.IDENTITY, receipt["plugin_flags"])
        result = INSTALLER.uninstall(self.home)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.settings()["enabledPlugins"][INSTALLER.IDENTITY])
        self.assertFalse(self.settings()["enabledPlugins"]["savvy-flow@claude-kit"])
        self.assertEqual(self.settings()["env"], {"keep": "value"})
        self.assertTrue(any("No owned previous value" in warning for warning in result["warnings"]))

    def test_owned_unusual_flag_value_is_restored_exactly(self):
        self.write_settings({"enabledPlugins": {"savvy-flow@claude-kit": 0}})
        self.run_install()
        INSTALLER.uninstall(self.home)
        original = self.settings()["enabledPlugins"]["savvy-flow@claude-kit"]
        self.assertIs(type(original), int)
        self.assertEqual(original, 0)

    def test_unrecognized_package_is_preserved(self):
        self.destination.mkdir(parents=True)
        (self.destination / "user.txt").write_text("keep", encoding="utf-8")
        self.write_settings({"enabledPlugins": {INSTALLER.IDENTITY: True}})
        result = INSTALLER.uninstall(self.home)
        self.assertTrue((self.destination / "user.txt").exists())
        self.assertFalse(self.settings()["enabledPlugins"][INSTALLER.IDENTITY])
        self.assertTrue(any("not identified" in warning for warning in result["warnings"]))

    def test_damaged_receipt_prevents_mutations(self):
        self.run_install()
        for invalid in (b"{invalid", b'{"owner":"other"}', b'{"owner":"savvy-codex-installer","schema_version":2,"plugin_identity":"savvy-codex@skills-dir","status":"installed","plugin_flags":{"other":{}}}'):
            (self.home / INSTALLER.RECEIPT_NAME).write_bytes(invalid)
            before = self.snapshot()
            with self.assertRaises(ValueError):
                self.run_install()
            with self.assertRaises(ValueError):
                INSTALLER.uninstall(self.home)
            self.assertEqual(before, self.snapshot())

    def test_manifest_identity_is_validated(self):
        (self.source / ".claude-plugin" / "plugin.json").write_text('{"name":"different","version":"2"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse(self.destination.exists())

    def test_link_ancestors_and_installed_child_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable for this test account")
        with self.assertRaises(ValueError):
            INSTALLER.install(self.source, link / "nested", skip_doctor=True)
        self.assertFalse((outside / "nested").exists())
        self.run_install()
        (self.destination / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            INSTALLER.uninstall(self.home)
        self.assertTrue(self.destination.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def assert_live_snapshot(self, expected):
        actual = {key: value for key, value in self.snapshot().items()
                  if not key.startswith("backups/") and not key.startswith("backups\\")}
        self.assertEqual(actual, expected)

    def live_snapshot(self):
        return {key: value for key, value in self.snapshot().items()
                if not key.startswith("backups/") and not key.startswith("backups\\")}

    def test_failed_staged_move_restores_original_package_and_settings(self):
        self.run_install()
        before = self.live_snapshot()
        (self.source / "hooks" / "sample.txt").write_text("new version")
        with mock.patch.object(INSTALLER.shutil, "move", side_effect=OSError("injected move failure")):
            with self.assertRaisesRegex(OSError, "injected move"):
                self.run_install()
        self.assert_live_snapshot(before)
        self.assertEqual(list(self.destination.parent.glob(".savvy-codex-rollback-*")), [])
        backups = list((self.home / "backups" / "savvy-codex").glob("*/" + INSTALLER.BACKUP_MARKER))
        self.assertTrue(any(not json.loads(path.read_text())["successful"] for path in backups))

    def test_partial_move_failure_removes_partial_package_before_restoration(self):
        self.run_install()
        before = self.live_snapshot()
        (self.source / "hooks" / "sample.txt").write_text("new version")
        def partial_move(source, destination):
            Path(destination).mkdir()
            (Path(destination) / "partial.txt").write_text("partial")
            raise OSError("injected partial move")
        with mock.patch.object(INSTALLER.shutil, "move", side_effect=partial_move):
            with self.assertRaisesRegex(OSError, "partial move"):
                self.run_install()
        self.assert_live_snapshot(before)

    def test_each_configuration_commit_failure_rolls_back_package_and_exact_files(self):
        for target in ("settings.json", "CLAUDE.md", INSTALLER.RECEIPT_NAME):
            with self.subTest(target=target):
                self.write_settings({"model": "keep", "enabledPlugins": {"other": True}})
                (self.home / "CLAUDE.md").write_text("before" + INSTALLER.START + "old" + INSTALLER.END + "after")
                before = self.live_snapshot()
                original = INSTALLER.atomic_write
                failed = False
                def fail_once(path, data):
                    nonlocal failed
                    if path == self.home / target and not failed:
                        failed = True
                        raise OSError("injected config failure")
                    return original(path, data)
                with mock.patch.object(INSTALLER, "atomic_write", side_effect=fail_once):
                    with self.assertRaisesRegex(OSError, "config failure"):
                        self.run_install()
                self.assertTrue(failed)
                self.assert_live_snapshot(before)

    def test_uninstall_failure_restores_package_and_receipt(self):
        self.run_install()
        before = self.live_snapshot()
        original = INSTALLER.atomic_write
        failed = False
        def fail_once(path, data):
            nonlocal failed
            if path == self.home / INSTALLER.RECEIPT_NAME and not failed:
                failed = True
                raise OSError("injected uninstall failure")
            return original(path, data)
        with mock.patch.object(INSTALLER, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "uninstall failure"):
                INSTALLER.uninstall(self.home)
        self.assert_live_snapshot(before)

    def test_failed_rollback_keeps_recovery_backup_and_original_package(self):
        self.run_install()
        old_package = INSTALLER.file_hashes(self.destination)
        self.write_settings({"enabledPlugins": {"other": True}})
        original_settings = (self.home / "settings.json").read_bytes()
        (self.source / "hooks" / "sample.txt").write_text("upgrade")
        original = INSTALLER.atomic_write
        def persistent_failure(path, data):
            if path == self.home / "settings.json":
                raise OSError("settings locked")
            return original(path, data)
        with mock.patch.object(INSTALLER, "atomic_write", side_effect=persistent_failure):
            with self.assertRaisesRegex(OSError, "preserved backup/sibling"):
                self.run_install()
        self.assertEqual(INSTALLER.file_hashes(self.destination), old_package)
        failed = [entry for entry in (self.home / "backups" / "savvy-codex").iterdir()
                  if not json.loads((entry / INSTALLER.BACKUP_MARKER).read_text())["successful"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0] / "settings.json").read_bytes(), original_settings)
        INSTALLER.prune_backups(self.home, 0)
        self.assertTrue(failed[0].exists())

    def test_retention_only_prunes_successful_identified_backups(self):
        self.run_install()
        root = self.home / "backups" / "savvy-codex"
        unknown = root / "unknown"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("user backup")
        INSTALLER.backup_metadata(unknown, self.home, successful=True)
        failed = root / "failed"
        failed.mkdir()
        INSTALLER.backup_metadata(failed, self.home, successful=False)
        wrong_path = root / "wrong-path"
        wrong_path.mkdir()
        INSTALLER.backup_metadata(wrong_path, self.home, successful=True)
        marker = json.loads((wrong_path / INSTALLER.BACKUP_MARKER).read_text())
        marker["backup_path"] = str(root / "other")
        (wrong_path / INSTALLER.BACKUP_MARKER).write_text(json.dumps(marker))
        for index in range(7):
            (self.source / "hooks" / "sample.txt").write_text(str(index))
            self.run_install()
        managed = [path for path in root.iterdir() if path not in (unknown, failed, wrong_path)]
        self.assertEqual(len(managed), 5)
        self.assertTrue((unknown / "keep.txt").exists())
        self.assertTrue(failed.exists())
        self.assertTrue(wrong_path.exists())

    def test_invalid_retention_fails_before_any_mutation(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            INSTALLER.install(self.source, self.home, skip_doctor=True, keep_backups=-1)
        self.assertEqual(before, self.snapshot())

    def test_postcommit_old_package_cleanup_failure_returns_success_with_recovery_path(self):
        self.run_install()
        (self.source / "hooks/sample.txt").write_text("committed upgrade")
        original = INSTALLER.shutil.rmtree
        def locked_old_package(path, *args, **kwargs):
            if Path(path).name.startswith(".savvy-codex-rollback-"):
                raise PermissionError("injected cleanup lock")
            return original(path, *args, **kwargs)
        with mock.patch.object(INSTALLER.shutil, "rmtree", side_effect=locked_old_package):
            result = self.run_install()
        self.assertTrue(result["changed"])
        self.assertEqual((self.destination / "hooks/sample.txt").read_text(), "committed upgrade")
        self.assertEqual(json.loads((self.home / INSTALLER.RECEIPT_NAME).read_text())["status"], "installed")
        leftovers = list(self.destination.parent.glob(".savvy-codex-rollback-*"))
        self.assertEqual(len(leftovers), 1)
        self.assertTrue(any(str(leftovers[0]) in warning for warning in result["warnings"]))

    def test_postcommit_backup_marker_failure_returns_success_and_skips_retention(self):
        self.run_install()
        (self.source / "hooks/sample.txt").write_text("committed marker failure")
        original = INSTALLER.backup_metadata
        def fail_completion(backup, home, *, successful):
            if successful:
                raise OSError("injected marker failure")
            return original(backup, home, successful=successful)
        with mock.patch.object(INSTALLER, "backup_metadata", side_effect=fail_completion), \
             mock.patch.object(INSTALLER, "prune_backups") as prune:
            result = self.run_install()
        prune.assert_not_called()
        self.assertEqual((self.destination / "hooks/sample.txt").read_text(), "committed marker failure")
        backup = Path(result["backup"])
        self.assertTrue(backup.exists())
        self.assertFalse(json.loads((backup / INSTALLER.BACKUP_MARKER).read_text())["successful"])
        self.assertTrue(any("Changes committed" in warning and "marker" in warning for warning in result["warnings"]))

    def test_postcommit_retention_failure_preserves_uninstall_success(self):
        self.run_install()
        with mock.patch.object(INSTALLER, "prune_backups", side_effect=PermissionError("injected prune lock")):
            result = INSTALLER.uninstall(self.home)
        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse(self.destination.exists())
        self.assertEqual(json.loads((self.home / INSTALLER.RECEIPT_NAME).read_text())["status"], "uninstalled")
        self.assertTrue((Path(result["backup"]) / "savvy-codex").exists())
        self.assertTrue(any("Changes committed" in warning and "retention" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
