"""Prevent divergence between the Python JSON and host-compatible generated TS."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("policy_sync", ROOT / "scripts" / "sync_policy.py")
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class PolicyTests(unittest.TestCase):
    def test_generated_host_policy_matches_canonical_json(self):
        self.assertEqual((ROOT / "shared" / "path-policy.ts").read_text(encoding="utf-8"), SYNC.generated(ROOT))

    def test_host_fixture_module_matches_canonical_cases(self):
        self.assertEqual((ROOT / "shared" / "path-policy-cases.ts").read_text(encoding="utf-8"), SYNC.generated_cases(ROOT))

    def test_canonical_cases_match_python_credential_guard(self):
        spec = importlib.util.spec_from_file_location("snapshot_policy", ROOT / "skills" / "codex-worker" / "scripts" / "scope_snapshot.py")
        snapshot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(snapshot)
        cases = json.loads((ROOT / "testfixtures" / "path-policy-cases.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(path=case["path"]):
                self.assertEqual(snapshot.sensitive(case["path"]), case["denied"])
