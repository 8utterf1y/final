from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import connect
from .locking import repository_lock
from .report import finish_case
from .workflow import advance, context, start_case, status, submit_stage


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spec-review-runtime")
    parser.add_argument("operation", choices=["start", "status", "next", "context", "submit", "finish"])
    parser.add_argument("--payload", default="-", help="JSON 载荷文件路径；使用 - 表示从标准输入读取")
    args = parser.parse_args(argv)
    try:
        payload = _payload(args.payload)
        repo = _resolve_repo(payload)
        with repository_lock(repo):
            with connect(repo) as connection:
                result = _dispatch(connection, repo, args.operation, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _dispatch(connection, repo, operation, payload):
    if operation == "start":
        return start_case(connection, repo, payload)
    if operation == "status":
        return status(connection, payload.get("caseId"))
    if operation == "next":
        return advance(connection, _required(payload, "caseId"))
    if operation == "context":
        return context(
            connection, repo, _required(payload, "caseId"), payload.get("claimId"),
            payload.get("gapId"), str(payload.get("direction") or "both"),
            int(payload.get("maxNodes") or 40),
        )
    if operation == "submit":
        return submit_stage(
            connection, _required(payload, "caseId"), _required(payload, "stage"),
            _required(payload, "result"),
        )
    if operation == "finish":
        return finish_case(connection, repo, _required(payload, "caseId"))
    raise ValueError(f"未知操作：{operation}")


def _payload(value: str) -> dict:
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    payload = json.loads(text or "{}")
    if not isinstance(payload, dict):
        raise ValueError("载荷必须是 JSON 对象")
    return payload


def _resolve_repo(payload: dict) -> Path:
    value = payload.get("repo")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少必填字段 repo；请传入业务仓库的绝对路径")
    repo = Path(value).expanduser().resolve()
    if repo == Path("/"):
        raise ValueError("仓库路径解析为文件系统根目录 /。请在业务仓库根目录打开 OpenCode，或传入有效的仓库路径。")
    if not repo.is_dir():
        raise ValueError(f"仓库路径不是目录：{repo}")
    return repo


def _required(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"缺少必填字段 {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
