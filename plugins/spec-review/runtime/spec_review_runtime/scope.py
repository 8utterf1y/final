from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .indexer import git_revision
from .util import stable_id


HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?P<context>.*)$"
)


def resolve_change_scope(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    snapshot_id: str,
    base: Optional[str],
    head: Optional[str],
    path_filters: list[str],
) -> dict:
    resolved_head = git_revision(repo, head or "HEAD")
    resolved_base = git_revision(repo, base) if base else None
    if not base:
        return {
            "base_revision": None,
            "head_revision": resolved_head,
            "seed_count": 0,
            "note": "未提供基准版本；需求声明将通过可选的路径范围关联到代码。",
        }

    command = [
        "git", "-C", str(repo), "diff", "--no-ext-diff", "--no-color", "--unified=3",
        f"{base}...{head or 'HEAD'}", "--",
    ]
    command.extend(path_filters)
    try:
        diff = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"无法计算 Git 差异：{exc}") from exc

    seeds = list(_parse_diff(diff))
    for seed in seeds:
        seed["symbol_id"] = _symbol_at_line(
            connection, snapshot_id, seed["path"], seed["new_start"]
        )
        seed_id = stable_id(
            "SEED", case_id, seed["path"], seed["old_start"], seed["new_start"], seed["diff_text"]
        )
        connection.execute(
            "INSERT INTO change_seeds VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seed_id, case_id, seed["path"], seed["old_start"], seed["old_count"],
             seed["new_start"], seed["new_count"], seed["symbol_id"], seed["change_type"],
             seed["diff_text"]),
        )
    return {
        "base_revision": resolved_base,
        "head_revision": resolved_head,
        "seed_count": len(seeds),
    }


def _parse_diff(diff: str) -> Iterable[dict]:
    path = ""
    lines = diff.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ b/"):
            path = line[6:]
            index += 1
            continue
        match = HUNK_RE.match(line)
        if not match or not path:
            index += 1
            continue
        chunk = [line]
        index += 1
        while index < len(lines) and not lines[index].startswith(("@@ ", "diff --git ")):
            chunk.append(lines[index])
            index += 1
        additions = any(item.startswith("+") and not item.startswith("+++") for item in chunk[1:])
        deletions = any(item.startswith("-") and not item.startswith("---") for item in chunk[1:])
        change_type = "modified" if additions and deletions else "added" if additions else "deleted"
        yield {
            "path": path,
            "old_start": int(match.group(1)),
            "old_count": int(match.group(2) or "1"),
            "new_start": int(match.group(3)),
            "new_count": int(match.group(4) or "1"),
            "change_type": change_type,
            "diff_text": "\n".join(chunk),
        }


def _symbol_at_line(connection, snapshot_id: str, path: str, line: int):
    row = connection.execute(
        "SELECT s.symbol_id FROM symbols s JOIN files f USING(file_id) "
        "WHERE s.snapshot_id=? AND f.path=? AND s.start_line<=? AND s.end_line>=? "
        "ORDER BY s.end_line-s.start_line LIMIT 1",
        (snapshot_id, path, line, line),
    ).fetchone()
    return row["symbol_id"] if row else None
