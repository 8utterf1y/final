from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from spec_review_runtime.context import build_context_packs
from spec_review_runtime.db import connect
from spec_review_runtime.indexer import build_or_update_index
from spec_review_runtime.locking import repository_lock
from spec_review_runtime.report import finish_case
from spec_review_runtime.cli import _resolve_repo
from spec_review_runtime.workflow import advance, start_case, submit_stage


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self._git("init")
        self._git("config", "user.email", "spec-review@example.test")
        self._git("config", "user.name", "需求审查测试")
        (self.repo / "service.py").write_text(
            "def deliver(value):\n    return value\n\ndef process(value):\n    return deliver(value)\n",
            encoding="utf-8",
        )
        (self.repo / "requirements.md").write_text(
            "# 数据交付\n\n服务必须通过交付处理器发送每个已接受的值。\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base = self._git("rev-parse", "HEAD").strip()
        (self.repo / "service.py").write_text(
            "def deliver(value):\n    return value\n\ndef process(value):\n    if value is None:\n        return None\n    return deliver(value)\n",
            encoding="utf-8",
        )
        self._git("add", "service.py")
        self._git("commit", "-m", "add guard")

    def tearDown(self):
        self.temp.cleanup()

    def test_incremental_snapshot_reuses_unchanged_files(self):
        with connect(self.repo) as connection:
            first = build_or_update_index(connection, self.repo)
            second = build_or_update_index(connection, self.repo)
        self.assertEqual(first["files_total"], 1)
        self.assertEqual(second["files_reused"], 1)
        self.assertEqual(second["files_parsed"], 0)

    def test_duplicate_calls_on_same_line_get_distinct_edges(self):
        (self.repo / "service.py").write_text(
            "def deliver(value):\n    return value\n\ndef process(value):\n    return deliver(value), deliver(value)\n",
            encoding="utf-8",
        )
        self._git("add", "service.py")
        self._git("commit", "-m", "same line duplicate calls")
        with connect(self.repo) as connection:
            snapshot = build_or_update_index(connection, self.repo)
            rows = connection.execute(
                "SELECT edge_id FROM edges WHERE snapshot_id=? AND target_name='deliver'",
                (snapshot["snapshot_id"],),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["edge_id"] for row in rows}), 2)

    def test_fast_case_builds_diff_context_and_report(self):
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["requirements.md"], "base": self.base, "head": "HEAD", "mode": "fast",
            })
            case_id = started["case_id"]
            self.assertEqual(started["next_action"]["action"], "l3_review")
            self.assertGreaterEqual(started["scope"]["seed_count"], 1)
            packet = build_context_packs(connection, self.repo, case_id, None, "both", 40)
            self.assertEqual(len(packet["packs"]), 1)
            evidence = packet["packs"][0]["evidence"]
            self.assertTrue(any(item["kind"] == "diff" for item in evidence))
            self.assertTrue(any(item["kind"] == "source" for item in evidence))
            submit_stage(connection, case_id, "l3_review", json.dumps({
                "summary": "没有发现不一致。",
                "claims": [{
                    "claim_id": packet["packs"][0]["claim"]["claim_id"],
                    "verdict": "consistent",
                    "evidence_ids": [next(item["evidence_id"] for item in evidence if item["kind"] == "source")],
                }],
            }))
            advanced = advance(connection, case_id)
            self.assertEqual(advanced["next_action"]["action"], "finish")
            finished = finish_case(connection, self.repo, case_id)
        self.assertEqual(finished["status"], "completed")
        self.assertTrue(Path(finished["markdown_report"]).exists())

    def test_report_groups_multiple_claims_by_root_cause(self):
        (self.repo / "two.md").write_text(
            "# 功能\n\n- 服务必须交付所有已经接受并完成校验的值。\n- 服务必须记录每次交付行为。\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["two.md"], "paths": ["service.py"], "mode": "fast",
            })
            packet = build_context_packs(connection, self.repo, started["case_id"], None, "both", 10)
            claims = []
            for pack in packet["packs"]:
                evidence_id = next(item["evidence_id"] for item in pack["evidence"] if item["kind"] == "source")
                claims.append({
                    "claim_id": pack["claim"]["claim_id"],
                    "verdict": "inconsistent",
                    "severity": "critical",
                    "attribution": "introduced",
                    "root_cause_id": "ROOT-MISSING-DELIVERY-GUARD",
                    "evidence_ids": [evidence_id],
                    "reason": "process 绕过同一处交付保护逻辑。",
                })
            submit_stage(connection, started["case_id"], "l3_review", json.dumps({"claims": claims}))
            advance(connection, started["case_id"])
            finished = finish_case(connection, self.repo, started["case_id"])
            payload = json.loads(Path(finished["json_report"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["coverage"]["submitted"], 2)
        self.assertEqual(payload["result"]["verdict_counts"]["inconsistent"], 2)
        self.assertEqual(len(payload["result"]["findings"]), 2)
        self.assertEqual(len(payload["result"]["root_findings"]), 1)
        self.assertEqual(payload["result"]["root_findings"][0]["affected_claim_count"], 2)
        self.assertIn("## 未完成或不一致项", finished["report"])
        self.assertIn("影响范围：2 条需求声明", finished["report"])
        self.assertIn("逐条覆盖结果、证据 ID", finished["report"])
        self.assertNotIn("逐声明不一致明细", finished["report"])
        self.assertNotIn("## 审查范围", finished["report"])

    def test_auto_case_escalates_uncertain_result(self):
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["requirements.md"], "base": self.base, "mode": "auto",
            })
            case_id = started["case_id"]
            packet = build_context_packs(connection, self.repo, case_id, None, "both", 40)
            submit_stage(connection, case_id, "l3_review", json.dumps({
                "claims": [{"claim_id": packet["packs"][0]["claim"]["claim_id"], "verdict": "uncertain"}],
            }))
            advanced = advance(connection, case_id)
        self.assertEqual(advanced["next_action"]["action"], "l4_initial")
        self.assertEqual(advanced["next_action"]["executor"], "spec-review")
        self.assertNotIn("agent", advanced["next_action"])

    def test_snapshot_context_is_paged_bounded_and_has_no_empty_diff(self):
        (self.repo / "many.md").write_text(
            "# 文档信息\n\n文档编号：EXP-001\n\n# 功能\n\n" +
            "\n".join(f"- 服务必须处理第 {index} 类交付请求。" for index in range(12)),
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["many.md"], "paths": ["service.py"], "mode": "fast",
            })
            first = build_context_packs(connection, self.repo, started["case_id"], None, "both", 1)
            second = build_context_packs(
                connection, self.repo, started["case_id"], None, "both", 1,
                cursor=first["page"]["next_cursor"], limit=5,
            )
        self.assertEqual(started["review_type"], "snapshot")
        self.assertEqual(len(first["packs"]), 3)
        self.assertEqual(len(second["packs"]), 5)
        self.assertTrue(first["page"]["total"] > len(first["packs"]))
        self.assertEqual(first["packs"][0]["claim"]["verifiability"], "metadata")
        for pack in first["packs"]:
            self.assertFalse(any(item["kind"] == "diff" for item in pack["evidence"]))
            self.assertLessEqual(len(pack["graph"]["symbols"]), 1)
            self.assertLessEqual(len(pack["evidence"]), 10)

    def test_submission_rejects_fake_ids_and_incomplete_coverage(self):
        (self.repo / "two.md").write_text(
            "# 功能\n\n- 服务必须交付所有已经接受并完成校验的值。\n- 服务必须拒绝所有内容为空的非法交付请求。\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["two.md"], "paths": ["service.py"], "mode": "fast",
            })
            packet = build_context_packs(connection, self.repo, started["case_id"], None, "both", 10)
            with self.assertRaisesRegex(ValueError, "非法 claim_id"):
                submit_stage(connection, started["case_id"], "l3_review", json.dumps({
                    "claims": [{"claim_id": "UNKNOWN-REMAINING", "verdict": "uncertain"}],
                }))
            with self.assertRaisesRegex(ValueError, "覆盖率门禁失败"):
                submit_stage(connection, started["case_id"], "l3_review", json.dumps({
                    "claims": [{"claim_id": packet["packs"][0]["claim"]["claim_id"], "verdict": "uncertain"}],
                }))

    def test_l4_context_only_returns_l3_candidates(self):
        (self.repo / "two.md").write_text(
            "# 功能\n\n- 服务必须交付所有已经接受并完成校验的值。\n- 服务必须拒绝所有内容为空的非法交付请求。\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["two.md"], "paths": ["service.py"], "mode": "auto",
            })
            packet = build_context_packs(connection, self.repo, started["case_id"], None, "both", 10)
            first, second = packet["packs"]
            source_id = next(item["evidence_id"] for item in first["evidence"] if item["kind"] == "source")
            submit_stage(connection, started["case_id"], "l3_review", json.dumps({
                "claims": [
                    {"claim_id": first["claim"]["claim_id"], "verdict": "consistent", "evidence_ids": [source_id]},
                    {"claim_id": second["claim"]["claim_id"], "verdict": "uncertain"},
                ],
            }))
            advance(connection, started["case_id"])
            deep = build_context_packs(connection, self.repo, started["case_id"], None, "both", 10)
        self.assertEqual(deep["page"]["total"], 1)
        self.assertEqual(deep["packs"][0]["claim"]["claim_id"], second["claim"]["claim_id"])

    def test_python_index_includes_module_constants(self):
        (self.repo / "service.py").write_text(
            "LARGE_EXPENSE_THRESHOLD = 5000\n\ndef process(value):\n    return value\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            snapshot = build_or_update_index(connection, self.repo)
            row = connection.execute(
                "SELECT kind FROM symbols WHERE snapshot_id=? AND name='LARGE_EXPENSE_THRESHOLD'",
                (snapshot["snapshot_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "constant")

    def test_report_marks_bypassed_incomplete_coverage_as_incomplete(self):
        (self.repo / "two.md").write_text(
            "# 功能\n\n- 服务必须交付所有已经接受并完成校验的值。\n- 服务必须拒绝所有内容为空的非法交付请求。\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["two.md"], "paths": ["service.py"], "mode": "fast",
            })
            claim = connection.execute(
                "SELECT claim_id FROM claims WHERE case_id=? ORDER BY ordinal LIMIT 1",
                (started["case_id"],),
            ).fetchone()["claim_id"]
            connection.execute(
                "INSERT INTO stage_runs VALUES(?,?,?,?,?)",
                ("bypassed-run", started["case_id"], "l3_review",
                 json.dumps({"claims": [{"claim_id": claim, "verdict": "uncertain"}]}), "now"),
            )
            connection.execute(
                "UPDATE review_cases SET stage='ready_to_finish' WHERE case_id=?", (started["case_id"],)
            )
            connection.commit()
            finished = finish_case(connection, self.repo, started["case_id"])
            state = connection.execute(
                "SELECT status FROM review_cases WHERE case_id=?", (started["case_id"],)
            ).fetchone()["status"]
        self.assertEqual(finished["status"], "incomplete")
        self.assertEqual(state, "incomplete")

    def test_deep_mode_context_targets_all_claims_without_l3(self):
        (self.repo / "two.md").write_text(
            "# 功能\n\n- 服务必须交付所有已经接受并完成校验的值。\n- 服务必须拒绝所有内容为空的非法交付请求。\n",
            encoding="utf-8",
        )
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["two.md"], "paths": ["service.py"], "mode": "deep",
            })
            packet = build_context_packs(connection, self.repo, started["case_id"], None, "both", 10)
        self.assertEqual(started["next_action"]["action"], "l4_initial")
        self.assertEqual(packet["page"]["total"], 2)

    def test_connect_accepts_legacy_evidence_table_without_unused_column(self):
        state_dir = self.repo / ".spec-review"
        state_dir.mkdir()
        old_db = state_dir / "index.sqlite"
        with sqlite3.connect(old_db) as connection:
            connection.execute(
                "CREATE TABLE evidence ("
                "evidence_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,claim_id TEXT,kind TEXT NOT NULL,"
                "path TEXT,start_line INTEGER,end_line INTEGER,revision TEXT,content TEXT NOT NULL,"
                "metadata_json TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
        with connect(self.repo) as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(evidence)").fetchall()
            }
        self.assertNotIn("replacement_seq", columns)

    def test_case_requires_an_explicit_review_scope(self):
        with connect(self.repo) as connection:
            with self.assertRaisesRegex(ValueError, "审查范围不明确"):
                start_case(connection, self.repo, {
                    "docs": ["requirements.md"], "mode": "fast",
                })

    def test_repository_lock_fails_fast_with_owner_details(self):
        with repository_lock(self.repo, timeout=0):
            with self.assertRaisesRegex(RuntimeError, "正在执行"):
                with repository_lock(self.repo, timeout=0):
                    pass

    def test_root_directory_is_rejected_before_state_creation(self):
        with self.assertRaisesRegex(ValueError, "文件系统根目录"):
            _resolve_repo({"repo": "/"})

    def test_missing_repository_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少必填字段 repo"):
            _resolve_repo({})

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
