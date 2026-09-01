from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from spec_review_runtime.context import build_context_packs
from spec_review_runtime.db import connect
from spec_review_runtime.github import parse_pr_url
from spec_review_runtime.publishing import fix_preview, publish_case, publish_preview
from spec_review_runtime.report import finish_case
from spec_review_runtime.workflow import advance, start_case, submit_stage


class FakeGitHubClient:
    token = "test-token"

    def __init__(self, pull):
        self.pull = pull
        self.reviews = []
        self.statuses = []

    def get_pull(self, owner, repo, number):
        return {
            "html_url": self.pull["html_url"], "title": "Test", "draft": False, "state": "open",
            "base": {"ref": self.pull["base_ref"], "sha": self.pull["base_sha"],
                     "repo": {"full_name": self.pull["base_repo"]}},
            "head": {"ref": self.pull["head_ref"], "sha": self.pull["head_sha"],
                     "repo": {"full_name": self.pull["head_repo"]}},
        }

    def create_review(self, owner, repo, number, payload):
        self.reviews.append(payload)
        return {"id": 101, "html_url": "https://github.com/acme/demo/pull/7#review-101"}

    def create_commit_status(self, owner, repo, sha, payload):
        self.statuses.append(payload)
        return {"id": 202, "url": "https://api.github.com/status/202"}


class GitHubIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self._git("init")
        self._git("config", "user.email", "spec-review@example.test")
        self._git("config", "user.name", "Spec Review Test")
        (self.repo / "service.py").write_text(
            "def deliver(value):\n    return value\n\ndef process(value):\n    return deliver(value)\n",
            encoding="utf-8",
        )
        (self.repo / "requirements.md").write_text(
            "# Processing\n\nThe service must reject empty values.\n", encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base = self._git("rev-parse", "HEAD").strip()
        (self.repo / "service.py").write_text(
            "def deliver(value):\n    return value\n\ndef process(value):\n    if value is None:\n        return None\n    return deliver(value)\n",
            encoding="utf-8",
        )
        self._git("add", "service.py")
        self._git("commit", "-m", "head")
        self.head = self._git("rev-parse", "HEAD").strip()
        self._git("remote", "add", "origin", "https://github.com/acme/demo.git")
        self.pull = {
            "owner": "acme", "repo": "demo", "number": 7,
            "html_url": "https://github.com/acme/demo/pull/7",
            "title": "Guard empty values", "draft": False, "state": "open",
            "base_ref": "main", "base_sha": self.base, "base_repo": "acme/demo",
            "head_ref": "feature", "head_sha": self.head, "head_repo": "acme/demo",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_parse_pr_url(self):
        parsed = parse_pr_url("https://github.com/acme/demo/pull/7/files")
        self.assertEqual(parsed["number"], 7)
        with self.assertRaises(ValueError):
            parse_pr_url("https://example.com/acme/demo/pull/7")

    def test_pr_start_locks_commits_and_uses_detached_worktree(self):
        with connect(self.repo) as connection, patch(
            "spec_review_runtime.workflow.resolve_pull_request", return_value=self.pull,
        ):
            started = start_case(connection, self.repo, {
                "docs": ["requirements.md"], "pr": self.pull["html_url"], "mode": "fast",
            })
            row = connection.execute(
                "SELECT base_revision,head_revision,scope_json FROM review_cases WHERE case_id=?",
                (started["case_id"],),
            ).fetchone()
        scope = json.loads(row["scope_json"])
        self.assertEqual(row["base_revision"], self.base)
        self.assertEqual(row["head_revision"], self.head)
        self.assertEqual(scope["pull_request"]["number"], 7)
        self.assertTrue(Path(scope["analysis_root"]).is_dir())
        self.assertTrue(scope["analysis_root"].endswith(self.head[:12]))

    def test_publish_preview_real_publish_and_idempotency(self):
        case_id = self._completed_pr_case("uncertain")
        client = FakeGitHubClient(self.pull)
        with connect(self.repo) as connection:
            preview = publish_preview(connection, self.repo, case_id)
            self.assertFalse(preview["writes_remote"])
            self.assertEqual(preview["locked_head_sha"], self.head)
            first = publish_case(
                connection, self.repo, case_id, self.head, dry_run=False, client=client,
            )
            second = publish_case(
                connection, self.repo, case_id, self.head, dry_run=False, client=client,
            )
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(len(client.statuses), 1)
        self.assertFalse(first["published"]["review"]["idempotent"])
        self.assertTrue(second["published"]["review"]["idempotent"])

    def test_publish_rejects_changed_head(self):
        case_id = self._completed_pr_case("uncertain")
        changed = dict(self.pull)
        changed["head_sha"] = "f" * 40
        with connect(self.repo) as connection:
            with self.assertRaisesRegex(RuntimeError, "head 已从"):
                publish_case(
                    connection, self.repo, case_id, self.head,
                    dry_run=False, client=FakeGitHubClient(changed),
                )

    def test_finish_generates_sarif_and_fix_preview_never_applies_patch(self):
        case_id = self._completed_pr_case("inconsistent", suggested_patch=(
            "diff --git a/service.py b/service.py\n"
            "--- a/service.py\n+++ b/service.py\n"
            "@@ -5,3 +5,3 @@ def process(value):\n"
            "-    if value is None:\n+    if value is None or value == \"\":\n"
            "         return None\n     return deliver(value)\n"
        ))
        original = (self.repo / "service.py").read_text(encoding="utf-8")
        with connect(self.repo) as connection:
            preview = fix_preview(connection, self.repo, case_id)
        self.assertTrue(preview["applies_cleanly"])
        self.assertFalse(preview["writes_business_code"])
        self.assertEqual((self.repo / "service.py").read_text(encoding="utf-8"), original)
        self.assertTrue((self.repo / ".spec-review" / "reports" / case_id / "review.sarif").exists())

    def _completed_pr_case(self, verdict, suggested_patch=None):
        with connect(self.repo) as connection:
            started = start_case(connection, self.repo, {
                "docs": ["requirements.md"], "base": self.base, "head": self.head, "mode": "fast",
            })
            packet = build_context_packs(connection, self.repo, started["case_id"], None, "both", 20)
            item = {
                "claim_id": packet["packs"][0]["claim"]["claim_id"],
                "verdict": verdict,
                "reason": "测试结论",
            }
            if verdict in {"consistent", "inconsistent"}:
                item["evidence_ids"] = [
                    next(e["evidence_id"] for e in packet["packs"][0]["evidence"] if e["kind"] == "source")
                ]
            if suggested_patch:
                item["suggested_patch"] = suggested_patch
            submit_stage(connection, started["case_id"], "l3_review", json.dumps({"claims": [item]}))
            advance(connection, started["case_id"])
            scope = json.loads(connection.execute(
                "SELECT scope_json FROM review_cases WHERE case_id=?", (started["case_id"],)
            ).fetchone()["scope_json"])
            scope["pull_request"] = self.pull
            scope["analysis_root"] = str(self.repo)
            connection.execute(
                "UPDATE review_cases SET scope_json=? WHERE case_id=?",
                (json.dumps(scope), started["case_id"]),
            )
            connection.commit()
            finish_case(connection, self.repo, started["case_id"])
            return started["case_id"]

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
