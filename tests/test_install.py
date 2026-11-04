"""Installer tests use temporary fake homes; no CLI or model is invoked."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

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
        (self.source / ".claude-plugin" / "marketplace.json").write_text("{}", encoding="utf-8")
        cache = self.source / "hooks" / "__pycache__"
        cache.mkdir()
        (cache / "sample.pyc").write_bytes(b"cache")
        (self.source / "hooks" / "loose.pyc").write_bytes(b"cache")
        self.destination = self.home / "skills" / "savvy-codex"

    def run_install(self, workflow=False):
        return INSTALLER.install(self.source, self.home, workflow)

    def test_narrow_settings_change_and_exact_backups(self):
        settings = {"env": {"PRIVATE_TOKEN": "fixture-only"}, "model": "keep", "provider": "keep", "other": [1], "enabledPlugins": {"other@local": True, "savvy-flow@claude-kit": True}}
        original = json.dumps(settings).encode()
        (self.home / "settings.json").write_bytes(original)
        (self.home / "CLAUDE.md").write_bytes(b"keep global instructions\r\n")
        self.destination.mkdir(parents=True)
        (self.destination / "old.txt").write_text("old", encoding="utf-8")
        result = self.run_install()
        after = json.loads((self.home / "settings.json").read_text())
        expected = dict(settings)
        expected["enabledPlugins"] = {**settings["enabledPlugins"], "savvy-codex@skills-dir": True, "savvy-flow@claude-kit": False, "savvy-progress@claude-kit": False}
        self.assertEqual(after, expected)
        backup = Path(result["backup"])
        self.assertEqual((backup / "settings.json").read_bytes(), original)
        self.assertEqual((backup / "savvy-codex" / "old.txt").read_text(), "old")
        self.assertEqual((self.home / "CLAUDE.md").read_bytes(), b"keep global instructions\r\n")
        self.assertFalse((self.destination / ".claude-plugin" / "marketplace.json").exists())
        self.assertFalse((self.destination / "hooks" / "__pycache__").exists())
        self.assertFalse((self.destination / "hooks" / "loose.pyc").exists())
        self.assertTrue((self.destination / "NOTICE").exists())
        self.assertEqual(json.loads((self.destination / ".install-info.json").read_text())["installer_python"], sys.executable)

    def test_workflow_is_opt_in_and_repeat_is_noop(self):
        original = "用户自己的规则\n"
        (self.home / "CLAUDE.md").write_text(original, encoding="utf-8")
        first = self.run_install(True)
        workflow = (self.home / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(workflow.startswith(original))
        self.assertEqual(workflow.count(INSTALLER.START), 1)
        self.assertEqual((Path(first["backup"]) / "CLAUDE.md").read_text(encoding="utf-8"), original)
        before_settings = (self.home / "settings.json").read_bytes()
        second = self.run_install(True)
        self.assertFalse(second["changed"])
        self.assertIsNone(second["backup"])
        self.assertEqual((self.home / "settings.json").read_bytes(), before_settings)
        self.assertEqual((self.home / "CLAUDE.md").read_text(encoding="utf-8"), workflow)

    def test_invalid_configuration_fails_before_installing(self):
        for invalid in (b"{oops", b"[]", b'{"enabledPlugins": []}'):
            (self.home / "settings.json").write_bytes(invalid)
            with self.assertRaises(ValueError):
                self.run_install()
            self.assertFalse(self.destination.exists())
            self.assertEqual((self.home / "settings.json").read_bytes(), invalid)

    def test_incomplete_markers_fail_without_changes(self):
        (self.home / "CLAUDE.md").write_text(INSTALLER.START, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_install(True)
        self.assertFalse((self.home / "settings.json").exists())
        self.assertFalse(self.destination.exists())

    def test_upgrade_preserves_old_package_and_removes_stale_files(self):
        self.run_install()
        (self.destination / "stale.txt").write_text("preserve in backup", encoding="utf-8")
        (self.source / "hooks" / "sample.txt").write_text("updated", encoding="utf-8")
        upgraded = self.run_install()
        self.assertFalse((self.destination / "stale.txt").exists())
        self.assertEqual((Path(upgraded["backup"]) / "savvy-codex" / "stale.txt").read_text(), "preserve in backup")
        self.assertEqual((self.destination / "hooks" / "sample.txt").read_text(), "updated")

    def test_existing_markers_are_replaced_and_surroundings_preserved(self):
        original = "Before\n" + INSTALLER.START + "\nold preference\n" + INSTALLER.END + "\nAfter\n"
        (self.home / "CLAUDE.md").write_text(original, encoding="utf-8")
        self.run_install(True)
        updated = (self.home / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(updated, "Before\n" + INSTALLER.WORKFLOW + "\nAfter\n")
        with self.assertRaises(ValueError):
            INSTALLER.workflow_text(INSTALLER.END + INSTALLER.START)


if __name__ == "__main__":
    unittest.main()
