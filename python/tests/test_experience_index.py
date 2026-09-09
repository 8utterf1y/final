"""Tests for experience-card indexing and two-stage retrieval."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_PYTHON_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PYTHON_DIR))

import mini_claude.experience as experience_module  # noqa: E402
from mini_claude.experience import ExperienceManager, TaskJournal  # noqa: E402
from mini_claude.experience_index import ExperienceIndex, format_experience_hits  # noqa: E402
from mini_claude.tools import (  # noqa: E402
    CONCURRENCY_SAFE_TOOLS,
    READ_TOOLS,
    check_permission,
    execute_tool,
    tool_definitions,
)


class FakeEmbeddingProvider:
    model = "fake-experience-embedding-v1"
    dimensions = 3

    async def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "auth" in lowered or "token" in lowered:
            return [1.0, 0.0, 0.0]
        if "database" in lowered or "migration" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_query(text) for text in texts]


class RecordingEmbeddingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return await super().embed_query(text)


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    async def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embedding service unavailable")


def payload(title: str, keyword: str) -> dict:
    return {
        "title": title,
        "description": f"Use for {keyword} assertion failures in **/{keyword}.py and validate with pytest tests/test_{keyword}.py.",
        "persistence_action": "create",
        "start_turn_id": 1,
        "end_turn_id": 1,
        "scenario": {
            "applies_when": [f"{keyword} tests fail"],
            "not_applies_when": ["the module is unrelated"],
            "signals": [f"{keyword} assertion error"],
        },
        "problem": {"goal": f"fix {keyword}", "symptoms": [f"broken {keyword}"], "constraints": []},
        "diagnosis": {"root_cause": f"stale {keyword} state", "evidence_event_ids": []},
        "procedure": [
            {"action": f"refresh {keyword} state", "reason": "remove stale state", "checkpoint": "state is current"},
            {"action": "run focused tests", "reason": "verify the fix", "checkpoint": "tests pass"},
        ],
        "pitfalls": [],
        "validation": [{"method": f"pytest tests/test_{keyword}.py", "expected_result": "passes", "evidence_event_ids": []}],
        "related_file_patterns": [f"**/{keyword}.py"],
        "retrieval_queries": [f"{keyword} test failure"],
        "tags": ["experience", keyword],
        "quality_score": 6,
    }


class ExperienceIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "experiences"
        self.root.mkdir(parents=True)
        self.index = ExperienceIndex(root=self.root, embedding_provider=FakeEmbeddingProvider())

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def add(self, experience_id: str, data: dict) -> Path:
        path = self.root / f"{experience_id}-{data['title'].lower().replace(' ', '-')}.md"
        path.write_text(f"# {data['title']}\n\n## Procedure\n\nDetailed ordered workflow.", encoding="utf-8")
        await self.index.upsert(experience_id, path, data)
        return path

    async def test_hybrid_search_returns_card_and_not_full_markdown(self) -> None:
        await self.add("1700000000", payload("Fix auth token", "auth"))
        await self.add("1700000001", payload("Repair database migration", "database"))

        hits = await self.index.search("auth token assertion failure", top_k=2)

        self.assertEqual(hits[0].card.id, "1700000000")
        rendered = format_experience_hits("auth token assertion failure", hits)
        self.assertIn("call experience_show", rendered)
        self.assertIn("Path:", rendered)
        self.assertIn("Description:", rendered)
        self.assertNotIn("Detailed ordered workflow", rendered)

    async def test_embedding_uses_description_not_full_experience_content(self) -> None:
        provider = RecordingEmbeddingProvider()
        index = ExperienceIndex(root=self.root, embedding_provider=provider)
        data = payload("Fix auth token", "auth")
        path = self.root / "1700000003-fix-auth-token.md"
        path.write_text("# Fix auth token\n\n## Content\n\nDetailed ordered workflow.", encoding="utf-8")

        card = await index.upsert("1700000003", path, data)

        self.assertEqual(card.description, data["description"])
        self.assertEqual(provider.queries[0], data["description"])
        self.assertNotIn("refresh auth state", provider.queries[0])
        self.assertNotIn("Detailed ordered workflow", provider.queries[0])

    async def test_manager_save_writes_markdown_and_card_without_knowledge_document(self) -> None:
        journal = TaskJournal("session")
        journal.start_turn("Fix auth failure")
        journal.record("tool_result", "Updated auth.py", tool_name="edit_file", tool_input={"file_path": "auth.py"}, status="success")
        verify = journal.record("tool_result", "1 passed", tool_name="run_shell", tool_input={"command": "pytest"}, status="success")
        data = payload("Fix auth token", "auth")
        data["validation"][0]["evidence_event_ids"] = [verify.event_id]

        async def extractor(system: str, user: str, max_tokens: int) -> str:
            return json.dumps(data)

        manager = ExperienceManager(journal, experience_index=self.index)
        result = await manager.save(extractor=extractor)

        self.assertEqual(result.status, "saved")
        self.assertIsNotNone(result.card)
        self.assertTrue(result.path and result.path.exists())
        markdown = result.path.read_text(encoding="utf-8")
        self.assertIn("## Description\n\nUse for auth assertion failures", markdown)
        self.assertIn("## Content\n\n## Scenario", markdown)
        self.assertEqual(self.index.list_cards()[0].path, str(result.path))
        self.assertFalse((self.root / "knowledge.db").exists())

    async def test_search_then_show_reads_complete_markdown(self) -> None:
        path = await self.add("1700000000", payload("Fix auth token", "auth"))
        manager = ExperienceManager(TaskJournal("tool"), experience_index=self.index)
        previous_manager = experience_module._default_experience_manager
        previous_project = experience_module._default_experience_project
        experience_module._default_experience_manager = manager
        experience_module._default_experience_project = Path.cwd().resolve()
        try:
            search_result = await execute_tool("experience_search", {"query": "auth token failure"})
            show_result = await execute_tool("experience_show", {"experience_id": "1700000000"})
        finally:
            experience_module._default_experience_manager = previous_manager
            experience_module._default_experience_project = previous_project

        self.assertIn("id=1700000000", search_result)
        self.assertIn("Detailed ordered workflow", show_result)
        self.assertIn(str(path), show_result)

    async def test_lexical_retrieval_survives_embedding_outage(self) -> None:
        fallback_index = ExperienceIndex(root=self.root, embedding_provider=FailingEmbeddingProvider())
        path = self.root / "1700000002-fix-lint-rule.md"
        path.write_text("# Fix lint rule\n\nComplete workflow.", encoding="utf-8")
        await fallback_index.upsert("1700000002", path, payload("Fix lint rule", "lint"))

        hits = await fallback_index.search("lint assertion error")

        self.assertEqual(hits[0].card.id, "1700000002")
        self.assertIsNone(hits[0].vector_score)

    async def test_delete_removes_file_and_card(self) -> None:
        path = await self.add("1700000000", payload("Fix auth token", "auth"))
        manager = ExperienceManager(TaskJournal("session"), experience_index=self.index)

        result = manager.delete_entry("1700000000")

        self.assertTrue(result.index_removed)
        self.assertFalse(path.exists())
        self.assertEqual(self.index.list_cards(), [])


class ExperienceToolRegistrationTests(unittest.TestCase):
    def test_tools_are_deferred_read_only_and_concurrency_safe(self) -> None:
        for name in ("experience_search", "experience_show"):
            tool = next(item for item in tool_definitions if item["name"] == name)
            self.assertTrue(tool["deferred"])
            self.assertIn(name, READ_TOOLS)
            self.assertIn(name, CONCURRENCY_SAFE_TOOLS)
            self.assertEqual(check_permission(name, {}, "plan")["action"], "allow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
