"""Tests for experience persistence pure functions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

from mini_claude.experience import (  # noqa: E402
    ExperienceManager,
    TaskJournal,
    render_experience_markdown,
    summarize_tool_result,
    validate_experience_payload,
)


class ExperienceTests(unittest.TestCase):
    def _journal_with_verified_edit(self) -> TaskJournal:
        journal = TaskJournal("session")
        journal.start_turn("Fix the auth test failure")
        journal.record("tool_call", "app.py", tool_name="read_file", tool_input={"file_path": "app.py"})
        journal.record("tool_result", "contents", tool_name="read_file", tool_input={"file_path": "app.py"}, status="success")
        journal.record("tool_call", "app.py", tool_name="edit_file", tool_input={"file_path": "app.py"})
        edit = journal.record("tool_result", "Updated app.py", tool_name="edit_file", tool_input={"file_path": "app.py"}, status="success")
        journal.record("tool_call", "pytest", tool_name="run_shell", tool_input={"command": "pytest"})
        test = journal.record("tool_result", "2 passed", tool_name="run_shell", tool_input={"command": "pytest"}, status="success")
        self.assertNotEqual(edit.event_id, test.event_id)
        return journal

    def test_validate_removes_missing_evidence_and_scores_verified_flow(self) -> None:
        journal = self._journal_with_verified_edit()
        run_event = next(event for event in journal.events if event.tool_name == "run_shell" and event.status == "success")
        payload = {
            "title": "Fix auth test workflow",
            "persistence_action": "create",
            "start_turn_id": 1,
            "end_turn_id": 1,
            "scenario": {"applies_when": ["auth tests fail"], "not_applies_when": [], "signals": ["pytest failure"]},
            "problem": {"goal": "fix test", "symptoms": [], "constraints": []},
            "diagnosis": {"root_cause": "missing branch", "evidence_event_ids": ["missing"]},
            "procedure": [{"action": "read, edit, test", "reason": "verify locally", "checkpoint": "pytest passes"}],
            "pitfalls": [],
            "validation": [{"method": "pytest", "expected_result": "passes", "evidence_event_ids": [run_event.event_id]}],
            "related_file_patterns": ["app.py"],
            "retrieval_queries": ["auth test failure"],
            "tags": ["experience", "testing"],
        }

        normalized, warnings = validate_experience_payload(payload, journal.events)

        self.assertEqual(normalized["persistence_action"], "create")
        self.assertNotIn("missing", normalized["diagnosis"]["evidence_event_ids"])
        self.assertNotIn("unverified", normalized["tags"])
        self.assertGreaterEqual(normalized["quality_score"], 4)
        self.assertTrue(any("Removed evidence ids" in warning for warning in warnings))

    def test_validate_marks_unverified_without_successful_shell_evidence(self) -> None:
        journal = TaskJournal("session")
        journal.start_turn("Edit a file")
        journal.record("tool_result", "Updated app.py", tool_name="edit_file", tool_input={"file_path": "app.py"}, status="success")
        payload = {
            "title": "Edit only",
            "persistence_action": "create",
            "start_turn_id": 1,
            "end_turn_id": 1,
            "procedure": [{"action": "edit", "reason": "change", "checkpoint": "file updated"}],
            "validation": [],
            "tags": ["experience"],
        }

        normalized, warnings = validate_experience_payload(payload, journal.events)

        self.assertIn("unverified", normalized["tags"])
        self.assertTrue(any("No successful verification" in warning for warning in warnings))

    def test_low_value_scope_is_skipped(self) -> None:
        journal = TaskJournal("session")
        journal.start_turn("What is this repo?")
        payload = {"title": "No tool work", "persistence_action": "create", "start_turn_id": 1, "end_turn_id": 1}

        normalized, warnings = validate_experience_payload(payload, journal.events)

        self.assertEqual(normalized["persistence_action"], "skip")
        self.assertTrue(any("little reusable activity" in warning for warning in warnings))

    def test_redacts_secrets_in_tool_inputs_and_markdown(self) -> None:
        journal = TaskJournal("session")
        journal.start_turn("Use token sk-testsecret123456")
        event = journal.record(
            "tool_result",
            "Authorization: Bearer abc.def",
            tool_name="run_shell",
            tool_input={"password": "plain", "command": "echo sk-testsecret123456"},
            status="success",
        )
        self.assertIn("[REDACTED]", event.summary)
        self.assertEqual(event.tool_input["password"], "[REDACTED]")
        self.assertIn("[REDACTED]", event.tool_input["command"])

    def test_render_contains_retrieval_metadata(self) -> None:
        journal = self._journal_with_verified_edit()
        run_event = next(event for event in journal.events if event.tool_name == "run_shell" and event.status == "success")
        normalized, _ = validate_experience_payload({
            "title": "Reusable test fix",
            "persistence_action": "create",
            "start_turn_id": 1,
            "end_turn_id": 1,
            "scenario": {"applies_when": ["pytest fails"], "not_applies_when": [], "signals": []},
            "problem": {"goal": "fix pytest", "symptoms": [], "constraints": []},
            "diagnosis": {"root_cause": "code issue", "evidence_event_ids": []},
            "procedure": [{"action": "Run tests", "reason": "verify", "checkpoint": "green"}],
            "validation": [{"method": "pytest", "expected_result": "passes", "evidence_event_ids": [run_event.event_id]}],
            "tags": ["experience"],
        }, journal.events)

        markdown = render_experience_markdown(normalized)

        self.assertIn("# Reusable test fix", markdown)
        self.assertIn("## Retrieval", markdown)
        self.assertIn("Quality score", markdown)

    def test_tool_result_status_detection(self) -> None:
        self.assertEqual(summarize_tool_result("run_shell", "Command failed (exit code 1)")[0], "failure")
        self.assertEqual(summarize_tool_result("run_shell", "3 passed")[0], "success")

    def test_list_show_and_delete_entries_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ExperienceManager(TaskJournal("session"), project_root=Path(tmp))
            manager.root = Path(tmp) / "experiences"
            manager.root.mkdir()
            path = manager.root / "1700000000-fix-auth-test.md"
            path.write_text("# Fix auth test\n\nBody", encoding="utf-8")

            entries = manager.list_entries()
            self.assertEqual(entries[0].id, "1700000000")
            self.assertEqual(entries[0].title, "Fix auth test")

            entry, content = manager.read_entry("1700000000")
            self.assertEqual(entry.path, path)
            self.assertIn("Body", content)

            result = manager.delete_entry("1700000000")
            self.assertTrue(result.deleted)
            self.assertFalse(path.exists())

    def test_ambiguous_experience_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ExperienceManager(TaskJournal("session"), project_root=Path(tmp))
            manager.root = Path(tmp) / "experiences"
            manager.root.mkdir()
            (manager.root / "1700000000-fix-auth.md").write_text("# First", encoding="utf-8")
            (manager.root / "1700000001-fix-auth.md").write_text("# Second", encoding="utf-8")

            with self.assertRaises(ValueError):
                manager.get_entry("fix-auth")


if __name__ == "__main__":
    unittest.main(verbosity=2)
