#!/usr/bin/env python3
"""Generate the host-compatible TS module from the single JSON path policy."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def generated(root: Path) -> str:
    policy = json.loads((root / "shared" / "path-policy.json").read_text(encoding="utf-8"))
    return ("// Generated from path-policy.json by scripts/sync_policy.py; do not edit.\n"
            + "export const pathPolicy = " + json.dumps(policy, ensure_ascii=False, indent=2) + " as const\n")


def generated_cases(root: Path) -> str:
    cases = json.loads((root / "testfixtures" / "path-policy-cases.json").read_text(encoding="utf-8"))
    return ("// Generated from testfixtures/path-policy-cases.json; do not edit.\n"
            + "export const pathPolicyCases = " + json.dumps(cases, ensure_ascii=False, indent=2) + " as const\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    outputs = {root / "shared" / "path-policy.ts": generated(root),
               root / "shared" / "path-policy-cases.ts": generated_cases(root)}
    if args.check:
        if any(not target.is_file() or target.read_text(encoding="utf-8") != text for target, text in outputs.items()):
            print("Generated path policy differs; run python scripts/sync_policy.py")
            return 1
        print("Shared path policy is synchronized")
        return 0
    for target, text in outputs.items():
        target.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
