from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

from .github import GitHubClient, resolve_pull_request
from .util import json_dumps, now, stable_id


def publish_preview(connection: sqlite3.Connection, repo: Path, case_id: str) -> dict:
    case, scope, pull, report = _publication_context(connection, repo, case_id)
    changed = _changed_right_lines(Path(scope["analysis_root"]), case["base_revision"], case["head_revision"])
    comments = _inline_comments(connection, case_id, report, changed)
    counts = report["result"]["verdict_counts"]
    root_findings = report["result"].get("root_findings") or report["result"].get("findings") or []
    body = _review_body(report, len(comments))
    review_payload = {
        "commit_id": case["head_revision"],
        "body": body,
        "event": "COMMENT",
        "comments": comments,
    }
    conclusion = "failure" if counts.get("inconsistent", 0) else (
        "neutral" if counts.get("uncertain", 0) else "success"
    )
    return {
        "case_id": case_id,
        "pull_request": pull,
        "locked_head_sha": case["head_revision"],
        "review": review_payload,
        "review_payload_hash": _payload_hash(review_payload),
        "inline_comments": len(comments),
        "summary_only_findings": max(0, counts.get("inconsistent", 0) - len(comments)),
        "check": {
            "name": "spec-review/consistency",
            "conclusion": conclusion,
            "details_url": pull["html_url"],
        },
        "sarif_report": str(_report_dir(repo, case_id) / "review.sarif"),
        "root_findings": len(root_findings),
        "writes_remote": False,
        "next_step": "确认预览后调用 spec_review_publish，并显式传入 expectedHeadSha。",
    }


def publish_case(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    expected_head_sha: str,
    dry_run: bool = True,
    event: str = "COMMENT",
    check_mode: str = "commit-status",
    upload_sarif: bool = False,
    client: Optional[GitHubClient] = None,
) -> dict:
    preview = publish_preview(connection, repo, case_id)
    stored_head = preview["locked_head_sha"]
    if expected_head_sha != stored_head:
        raise ValueError("expectedHeadSha 与案例锁定的 head SHA 不一致")
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        raise ValueError("event 只能是 COMMENT 或 REQUEST_CHANGES")
    if check_mode not in {"none", "commit-status", "check-run"}:
        raise ValueError("checkMode 只能是 none、commit-status 或 check-run")
    preview["review"]["event"] = event
    preview["review_payload_hash"] = _payload_hash(preview["review"])
    if dry_run:
        preview["dry_run"] = True
        return preview

    github = client or GitHubClient()
    if not github.token:
        raise ValueError("真实发布需要设置 GITHUB_TOKEN；publish-preview 不需要写权限")
    pull = preview["pull_request"]
    current = resolve_pull_request(pull["html_url"], github)
    if current["head_sha"] != stored_head:
        raise RuntimeError(
            f"拒绝发布：PR head 已从 {stored_head} 变为 {current['head_sha']}，请重新审查"
        )

    results = {}
    results["review"] = _publish_once(
        connection, case_id, "pr-review", stored_head, preview["review"],
        lambda: github.create_review(pull["owner"], pull["repo"], pull["number"], preview["review"]),
    )
    if check_mode == "commit-status":
        state = "failure" if preview["check"]["conclusion"] == "failure" else "success"
        status_payload = {
            "state": state,
            "context": "spec-review/consistency",
            "description": _status_description(preview),
            "target_url": pull["html_url"],
        }
        results["check"] = _publish_once(
            connection, case_id, "commit-status", stored_head, status_payload,
            lambda: github.create_commit_status(pull["owner"], pull["repo"], stored_head, status_payload),
        )
    elif check_mode == "check-run":
        check_payload = {
            "name": "spec-review/consistency",
            "head_sha": stored_head,
            "status": "completed",
            "conclusion": preview["check"]["conclusion"],
            "details_url": pull["html_url"],
            "output": {
                "title": "需求—代码一致性审查",
                "summary": preview["review"]["body"][:65000],
            },
        }
        results["check"] = _publish_once(
            connection, case_id, "check-run", stored_head, check_payload,
            lambda: github.create_check_run(pull["owner"], pull["repo"], check_payload),
        )

    if upload_sarif:
        sarif_path = Path(preview["sarif_report"])
        sarif_payload = {
            "commit_sha": stored_head,
            "ref": f"refs/pull/{pull['number']}/head",
            "sarif": base64.b64encode(gzip.compress(sarif_path.read_bytes())).decode("ascii"),
            "tool_name": "opencode-spec-review",
        }
        results["sarif"] = _publish_once(
            connection, case_id, "sarif", stored_head, sarif_payload,
            lambda: github.upload_sarif(pull["owner"], pull["repo"], sarif_payload),
        )
    return {**preview, "dry_run": False, "published": results}


def fix_preview(connection: sqlite3.Connection, repo: Path, case_id: str) -> dict:
    case, scope, pull, report = _publication_context(connection, repo, case_id)
    patches = []
    for finding in report["result"].get("findings", []):
        patch = finding.get("suggested_patch")
        if isinstance(patch, str) and patch.strip():
            patches.append({"claim_id": finding.get("claim_id"), "patch": patch.strip() + "\n"})
    combined = "".join(item["patch"] for item in patches)
    if combined:
        _validate_patch(combined)
        _git(Path(scope["analysis_root"]), "apply", "--check", "-", input_text=combined)
    return {
        "case_id": case_id,
        "pull_request": pull,
        "locked_head_sha": case["head_revision"],
        "suggestions": patches,
        "combined_patch": combined,
        "patch_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "applies_cleanly": bool(combined),
        "writes_business_code": False,
        "next_step": (
            "人工检查补丁后，显式调用 spec_review_create_fix_pr 并传 confirmation=CREATE_FIX_PR。"
            if combined else "报告中没有可用的 suggested_patch。"
        ),
    }


def create_fix_pr(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    expected_head_sha: str,
    confirmation: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    client: Optional[GitHubClient] = None,
) -> dict:
    if confirmation != "CREATE_FIX_PR":
        raise ValueError("创建 Fix PR 需要 confirmation 精确等于 CREATE_FIX_PR")
    preview = fix_preview(connection, repo, case_id)
    if not preview["combined_patch"]:
        raise ValueError("没有建议 Patch，不能创建 Fix PR")
    if expected_head_sha != preview["locked_head_sha"]:
        raise ValueError("expectedHeadSha 与案例锁定的 head SHA 不一致")
    pull = preview["pull_request"]
    if pull["head_repo"].lower() != pull["base_repo"].lower():
        raise ValueError("当前版本只允许为同仓库 PR 创建 Fix PR；fork PR 仅输出建议 Patch")
    github = client or GitHubClient()
    if not github.token:
        raise ValueError("创建 Fix PR 需要设置 GITHUB_TOKEN")
    current = resolve_pull_request(pull["html_url"], github)
    if current["head_sha"] != expected_head_sha:
        raise RuntimeError("拒绝创建 Fix PR：原 PR head 已变化，请重新审查")

    payload_fingerprint = {
        "head": expected_head_sha, "patch": preview["patch_sha256"],
        "title": title or "fix: apply spec-review suggestions",
    }
    existing = _existing_publication(connection, case_id, "fix-pr", expected_head_sha, _payload_hash(payload_fingerprint))
    if existing:
        return {"case_id": case_id, "idempotent": True, "publication": existing}

    branch = f"spec-review/fix-pr-{pull['number']}-{case_id[-8:].lower()}"
    worktree = repo / ".spec-review" / "fix-worktrees" / case_id
    if worktree.exists():
        raise RuntimeError(f"Fix worktree 已存在，请先人工核对后处理：{worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(worktree), expected_head_sha)
    _git(worktree, "apply", "-", input_text=preview["combined_patch"])
    _git(worktree, "add", "--all")
    _git(worktree, "commit", "-m", "fix: apply spec-review suggestions")
    remote = str((json.loads(_case(connection, case_id)["scope_json"]).get("git_preparation") or {}).get("remote") or "origin")
    _git(worktree, "push", "-u", remote, branch)
    pr_payload = {
        "title": title or "fix: apply spec-review suggestions",
        "head": branch,
        "base": pull["head_ref"],
        "body": body or f"由 spec-review 案例 `{case_id}` 的建议 Patch 生成；已由人工确认创建。",
    }
    response = github.create_pull(pull["owner"], pull["repo"], pr_payload)
    publication = _record_publication(
        connection, case_id, "fix-pr", expected_head_sha,
        _payload_hash(payload_fingerprint), response,
    )
    return {
        "case_id": case_id, "idempotent": False, "branch": branch,
        "fix_worktree": str(worktree), "publication": publication,
    }


def _publication_context(connection, repo: Path, case_id: str):
    case = _case(connection, case_id)
    if case["status"] != "completed" or case["stage"] != "finished":
        raise ValueError("只有覆盖率完整且已完成的案例可以发布")
    scope = json.loads(case["scope_json"])
    pull = scope.get("pull_request")
    if not isinstance(pull, dict):
        raise ValueError("该案例不是通过 --pr 创建，不能回写 GitHub PR")
    report_path = _report_dir(repo, case_id) / "review.json"
    if not report_path.exists():
        raise ValueError("找不到 review.json；请先调用 spec_review_finish")
    return case, scope, pull, json.loads(report_path.read_text(encoding="utf-8"))


def _changed_right_lines(repo: Path, base: str, head: str) -> dict[str, set[int]]:
    diff = _git(repo, "diff", "--no-ext-diff", "--no-color", "--unified=0", f"{base}...{head}", "--")
    changed: dict[str, set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            changed.setdefault(path, set())
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if match and path:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            changed[path].update(range(start, start + count))
    return changed


def _inline_comments(connection, case_id: str, report: dict, changed: dict[str, set[int]]) -> list[dict]:
    comments = []
    seen = set()
    for finding in report["result"].get("findings", []):
        for evidence_id in finding.get("evidence_ids") or finding.get("evidence") or []:
            row = connection.execute(
                "SELECT path,start_line,content FROM evidence WHERE case_id=? AND evidence_id=?",
                (case_id, evidence_id),
            ).fetchone()
            if not row or not row["path"] or row["start_line"] not in changed.get(row["path"], set()):
                continue
            key = (row["path"], row["start_line"], finding.get("claim_id"))
            if key in seen:
                continue
            seen.add(key)
            comments.append({
                "path": row["path"], "line": row["start_line"], "side": "RIGHT",
                "body": _comment_body(finding),
            })
            break
        if len(comments) >= 25:
            break
    return comments


def _comment_body(finding: dict) -> str:
    title = finding.get("title") or finding.get("claim_id") or "一致性问题"
    severity = finding.get("severity") or "unspecified"
    reason = finding.get("reasoning") or finding.get("reason") or finding.get("summary") or ""
    return f"**spec-review：{title}** (`{severity}`)\n\n{reason}"[:65000]


def _review_body(report: dict, inline_count: int) -> str:
    counts = report["result"]["verdict_counts"]
    coverage = report["coverage"]
    root_count = len(report["result"].get("root_findings") or report["result"].get("findings") or [])
    return (
        "## 需求—代码一致性审查\n\n"
        f"案例：`{report['case_id']}`  \n"
        f"锁定 Commit：`{report['head_revision']}`  \n"
        f"覆盖率：{coverage['submitted']}/{coverage['expected']}  \n"
        f"结论：consistent={counts['consistent']}，inconsistent={counts['inconsistent']}，"
        f"uncertain={counts['uncertain']}，not_applicable={counts['not_applicable']}。  \n"
        f"根因问题：{root_count} 个；不一致声明：{counts['inconsistent']} 条。  \n"
        f"已生成 {inline_count} 条可定位到本次 Diff 的行内评论。\n\n"
        "此结果由确定性审查状态机生成；无法定位到 Diff 新行的问题保留在审查摘要中。"
    )


def _status_description(preview: dict) -> str:
    check = preview["check"]["conclusion"]
    return {
        "failure": "发现需求—代码不一致",
        "neutral": "审查完成，但存在证据不足的声明",
        "success": "未发现可定案的不一致",
    }[check]


def _publish_once(connection, case_id, kind, head_sha, payload, publisher):
    payload_hash = _payload_hash(payload)
    existing = _existing_publication(connection, case_id, kind, head_sha, payload_hash)
    if existing:
        return {"idempotent": True, **existing}
    response = publisher()
    return {"idempotent": False, **_record_publication(connection, case_id, kind, head_sha, payload_hash, response)}


def _existing_publication(connection, case_id, kind, head_sha, payload_hash):
    row = connection.execute(
        "SELECT * FROM publications WHERE case_id=? AND kind=? AND head_sha=? AND payload_hash=? AND status='published'",
        (case_id, kind, head_sha, payload_hash),
    ).fetchone()
    return dict(row) if row else None


def _record_publication(connection, case_id, kind, head_sha, payload_hash, response):
    publication_id = stable_id("PUB", case_id, kind, head_sha, payload_hash)
    remote_id = response.get("id") or response.get("node_id")
    remote_url = response.get("html_url") or response.get("url")
    connection.execute(
        "INSERT OR IGNORE INTO publications VALUES(?,?,?,?,?,?,?,?,?,?)",
        (publication_id, case_id, kind, head_sha, payload_hash, "published",
         str(remote_id) if remote_id is not None else None, remote_url,
         json_dumps(response), now()),
    )
    connection.commit()
    return {
        "publication_id": publication_id, "kind": kind,
        "remote_id": remote_id, "remote_url": remote_url,
    }


def _validate_patch(patch: str) -> None:
    if len(patch.encode("utf-8")) > 200_000:
        raise ValueError("建议 Patch 超过 200KB 安全上限")
    paths = re.findall(r"^\+\+\+ b/(.+)$", patch, re.MULTILINE)
    if not paths or "GIT binary patch" in patch:
        raise ValueError("suggested_patch 必须是非二进制 unified diff")
    for value in paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
            raise ValueError(f"建议 Patch 包含不安全路径：{value}")


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def _case(connection, case_id):
    row = connection.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
    if not row:
        raise ValueError(f"未找到审查案例：{case_id}")
    return row


def _report_dir(repo: Path, case_id: str) -> Path:
    return repo / ".spec-review" / "reports" / case_id


def _git(repo: Path, *args: str, input_text: Optional[str] = None) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], input=input_text, check=True,
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(f"Git 命令失败：git {' '.join(args)}\n{detail.strip()}") from exc
