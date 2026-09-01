#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ORACLE = json.loads((ROOT / "oracle/expected_findings.json").read_text())
SUITE = ROOT / "tests/compliance_suite.py"


def run_version(name: str) -> tuple[dict, str]:
    completed = subprocess.run(
        [sys.executable, str(SUITE), str(ROOT / "versions" / name)],
        text=True,
        capture_output=True,
        check=False,
    )
    combined = completed.stdout + completed.stderr
    marker = next(
        line for line in combined.splitlines() if line.startswith("BENCHMARK_RESULT=")
    )
    return json.loads(marker.split("=", 1)[1]), combined


def main() -> int:
    mismatches = []
    print("版本                                  实际失败  预期失败  结果")
    print("-" * 70)
    for name, expected in ORACLE["versions"].items():
        actual, output = run_version(name)
        actual_failed = actual["failed_tests"]
        expected_failed = sorted(expected["expected_failed_tests"])
        matched = actual_failed == expected_failed
        print(
            f"{name:<36} {len(actual_failed):>6} {len(expected_failed):>7}  "
            f"{'符合预期' if matched else '不符合预期'}"
        )
        if not matched:
            mismatches.append(name)
            print(output)
    if mismatches:
        print("\n与标准答案不一致：" + ", ".join(mismatches))
        return 1
    print("\n所有版本均与预期标准答案一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
