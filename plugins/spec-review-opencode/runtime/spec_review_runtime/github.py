from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)(?:[/?#].*)?$"
)


def parse_pr_url(url: str) -> dict[str, Any]:
    if not isinstance(url, str):
        raise ValueError("pr 必须是 GitHub Pull Request URL")
    match = PR_URL_RE.match(url.strip())
    if not match:
        raise ValueError("PR URL 格式无效；期望 https://github.com/<owner>/<repo>/pull/<number>")
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "number": int(match.group("number")),
        "html_url": f"https://github.com/{match.group('owner')}/{match.group('repo')}/pull/{match.group('number')}",
    }


class GitHubClient:
    def __init__(self, token: Optional[str] = None, api_url: Optional[str] = None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.api_url = (api_url or os.environ.get("SPEC_REVIEW_GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "opencode-spec-review",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.api_url}{path}", data=body, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"GitHub API {method} {path} 失败：HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 GitHub API：{exc.reason}") from exc
        if not raw:
            return {}
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("GitHub API 返回了非对象响应")
        return result

    def get_pull(self, owner: str, repo: str, number: int) -> dict:
        return self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def create_review(self, owner: str, repo: str, number: int, payload: dict) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", payload)

    def create_check_run(self, owner: str, repo: str, payload: dict) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/check-runs", payload)

    def create_commit_status(self, owner: str, repo: str, sha: str, payload: dict) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/statuses/{sha}", payload)

    def upload_sarif(self, owner: str, repo: str, payload: dict) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/code-scanning/sarifs", payload)

    def create_pull(self, owner: str, repo: str, payload: dict) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/pulls", payload)


def resolve_pull_request(url: str, client: Optional[GitHubClient] = None) -> dict[str, Any]:
    parsed = parse_pr_url(url)
    data = (client or GitHubClient()).get_pull(parsed["owner"], parsed["repo"], parsed["number"])
    try:
        base = data["base"]
        head = data["head"]
        result = {
            **parsed,
            "html_url": data.get("html_url") or parsed["html_url"],
            "title": data.get("title") or "",
            "draft": bool(data.get("draft")),
            "state": data.get("state") or "unknown",
            "base_ref": base["ref"],
            "base_sha": base["sha"],
            "base_repo": base["repo"]["full_name"],
            "head_ref": head["ref"],
            "head_sha": head["sha"],
            "head_repo": head["repo"]["full_name"],
        }
    except (KeyError, TypeError) as exc:
        raise RuntimeError("GitHub PR 响应缺少 base/head 仓库或 Commit 信息") from exc
    for key in ("base_sha", "head_sha"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(result[key])):
            raise RuntimeError(f"GitHub PR 返回的 {key} 不是完整 Commit SHA")
    return result


def prepare_pull_worktree(repo: Path, pull: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    _require_git_repository(repo)
    remote = _select_remote(repo, str(pull["base_repo"]))
    fetched: list[str] = []
    if not _has_commit(repo, pull["base_sha"]):
        _git(repo, "fetch", "--no-tags", remote, pull["base_sha"])
        fetched.append("base")
    if not _has_commit(repo, pull["head_sha"]):
        _git(repo, "fetch", "--no-tags", remote, f"refs/pull/{pull['number']}/head")
        if not _has_commit(repo, pull["head_sha"]):
            raise RuntimeError("已 fetch PR head，但本地仍找不到 GitHub 返回的 head SHA")
        fetched.append("head")

    worktree = repo / ".spec-review" / "worktrees" / str(pull["head_sha"][:12])
    if worktree.exists():
        current = _git(worktree, "rev-parse", "HEAD").strip()
        if current != pull["head_sha"]:
            raise RuntimeError(f"PR 分析 worktree 已存在但指向其他 Commit：{worktree}")
        reused = True
    else:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "--detach", str(worktree), pull["head_sha"])
        reused = False
    return worktree, {"remote": remote, "fetched": fetched, "worktree_reused": reused}


def current_pull_head(pull: dict[str, Any], client: Optional[GitHubClient] = None) -> str:
    refreshed = resolve_pull_request(pull["html_url"], client)
    return str(refreshed["head_sha"])


def _require_git_repository(repo: Path) -> None:
    try:
        value = _git(repo, "rev-parse", "--is-inside-work-tree").strip()
    except RuntimeError as exc:
        raise ValueError(f"业务目录不是 Git 仓库：{repo}") from exc
    if value != "true":
        raise ValueError(f"业务目录不是 Git worktree：{repo}")


def _select_remote(repo: Path, expected_full_name: str) -> str:
    remotes = _git(repo, "remote").splitlines()
    for remote in remotes:
        url = _git(repo, "remote", "get-url", remote).strip()
        if _remote_full_name(url) == expected_full_name.lower():
            return remote
    raise ValueError(
        f"本地仓库 remote 与 PR 基准仓库不匹配：期望 {expected_full_name}；"
        "请在对应仓库中运行，或添加指向该仓库的 remote。"
    )


def _remote_full_name(url: str) -> Optional[str]:
    match = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?$", url.strip(), re.IGNORECASE)
    return match.group(1).lower() if match else None


def _has_commit(repo: Path, sha: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, text=True,
    )
    return process.returncode == 0


def _git(repo: Path, *args: str, input_text: Optional[str] = None) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], input=input_text, check=True,
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeError(f"Git 命令失败：git {' '.join(args)}\n{detail.strip()}") from exc
