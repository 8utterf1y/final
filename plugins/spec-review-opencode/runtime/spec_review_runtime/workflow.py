from __future__ import annotations

import fnmatch
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Optional

from .context import build_context_packs
from .documents import load_claim_candidates
from .github import prepare_pull_worktree, resolve_pull_request
from .indexer import build_or_update_index
from .scope import resolve_change_scope
from .util import json_dumps, now, stable_id


STAGES = {
    "l3_review", "l4_initial", "l4_challenge", "l4_investigate", "l4_converge",
}
VERDICTS = {"consistent", "inconsistent", "uncertain", "not_applicable"}
STAGE_INSTRUCTIONS = {
    "l3_review": "执行 L3 单次快速审查，输出候选不一致清单和是否需要升级。",
    "l4_initial": "执行 L4 初判，陈述期望、观察、候选差异、证据和未证实前提。",
    "l4_challenge": "执行 L4 质疑，尝试推翻候选问题并形成按优先级排序的证据缺口。",
    "l4_investigate": "执行 L4 取证，只围绕已有证据缺口沿调用链补齐必要证据。",
    "l4_converge": "执行 L4 收敛，去误报、合并重复、定级并形成最终结论。",
}


def start_case(connection: sqlite3.Connection, repo: Path, payload: dict[str, Any]) -> dict:
    docs = _string_list(payload.get("docs"), "docs", required=True)
    paths = _string_list(payload.get("paths"), "paths")
    sections = _string_list(payload.get("sections"), "sections")
    mode = str(payload.get("mode") or "auto")
    if mode not in {"fast", "deep", "auto"}:
        raise ValueError("mode 必须是 fast、deep 或 auto")
    base = _optional_string(payload.get("base"))
    requested_head = _optional_string(payload.get("head"))
    pr_url = _optional_string(payload.get("pr"))
    full_repo = payload.get("fullRepo") is True
    if pr_url and (base or requested_head):
        raise ValueError("使用 pr 时 base/head 由 GitHub 锁定，不能同时手工指定")
    if not pr_url and not base and not paths and not full_repo:
        raise ValueError("审查范围不明确：请提供 base、paths，或显式设置 fullRepo=true")

    analysis_repo = repo
    pull = None
    preparation = None
    if pr_url:
        pull = resolve_pull_request(pr_url)
        analysis_repo, preparation = prepare_pull_worktree(repo, pull)
        base = str(pull["base_sha"])
        head = str(pull["head_sha"])
    else:
        head = requested_head or "HEAD"

    snapshot = build_or_update_index(connection, analysis_repo)
    case_id = stable_id(
        "CASE", str(repo), snapshot["snapshot_id"], base or "", head,
        json_dumps(docs), json_dumps(paths), json_dumps(sections), mode, now(),
    )
    initial_stage = "l4_initial" if mode == "deep" else "l3_review"
    scope = {
        "documents": docs, "paths": paths, "sections": sections,
        "full_repository": full_repo,
        "review_type": "comparison" if base else "snapshot",
        "analysis_root": str(analysis_repo),
    }
    if pull:
        scope["pull_request"] = pull
        scope["git_preparation"] = preparation
        previous = _previous_pull_case(connection, pull)
        if previous:
            scope["incremental_review"] = {
                "parent_case_id": previous["case_id"],
                "previous_head_sha": previous["head_revision"],
                "head_changed": previous["head_revision"] != head,
                "index_reused_files": snapshot["files_reused"],
                "delta": _incremental_delta(analysis_repo, previous["head_revision"], head),
            }
    budget = {
        "max_context_nodes": 20, "max_investigation_nodes": 60,
        "max_claims_per_page": 10, "max_tool_rounds": 64,
    }
    timestamp = now()
    connection.execute(
        "INSERT INTO review_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, str(repo), snapshot["snapshot_id"], None, snapshot["revision"], mode,
         initial_stage, "active", json_dumps(scope), json_dumps(budget), timestamp, timestamp),
    )
    change_scope = resolve_change_scope(
        connection, analysis_repo, case_id, snapshot["snapshot_id"], base, head, paths,
    )
    scope.update(change_scope)
    connection.execute(
        "UPDATE review_cases SET base_revision=?,head_revision=?,scope_json=? WHERE case_id=?",
        (change_scope["base_revision"], change_scope["head_revision"], json_dumps(scope), case_id),
    )
    if change_scope["seed_count"] == 0 and paths:
        _seed_scoped_symbols(connection, case_id, snapshot["snapshot_id"], paths)
    claims = load_claim_candidates(connection, analysis_repo, case_id, docs, sections)
    connection.commit()
    return {
        "repo": str(repo),
        "case_id": case_id,
        "index": snapshot,
        "scope": change_scope,
        "pull_request": pull,
        "claims": len(claims),
        "review_type": change_scope["review_type"],
        "context_paging": {"cursor": 0, "limit": 3, "total": len(claims)},
        "next_action": action_packet(connection, case_id),
    }


def status(connection: sqlite3.Connection, case_id: Optional[str]) -> dict:
    case = _get_case(connection, case_id)
    counts = {
        "claims": connection.execute("SELECT count(*) FROM claims WHERE case_id=?", (case["case_id"],)).fetchone()[0],
        "seeds": connection.execute("SELECT count(*) FROM change_seeds WHERE case_id=?", (case["case_id"],)).fetchone()[0],
        "evidence": connection.execute("SELECT count(*) FROM evidence WHERE case_id=?", (case["case_id"],)).fetchone()[0],
        "stage_runs": connection.execute("SELECT count(*) FROM stage_runs WHERE case_id=?", (case["case_id"],)).fetchone()[0],
    }
    return {
        "repo": case["repo"], "case_id": case["case_id"],
        "mode": case["mode"], "stage": case["stage"],
        "status": case["status"], "snapshot_id": case["snapshot_id"], "counts": counts,
        "coverage": _coverage(connection, case),
        "next_action": action_packet(connection, case["case_id"]),
    }


def action_packet(connection: sqlite3.Connection, case_id: str) -> dict:
    case = _get_case(connection, case_id)
    stage = case["stage"]
    if case["status"] == "blocked":
        return {"action": "blocked", "case_id": case_id, "reason": stage}
    if stage == "ready_to_finish":
        return {"action": "finish", "case_id": case_id}
    if stage == "finished":
        return {"action": "done", "case_id": case_id}
    if stage not in STAGES:
        return {"action": "blocked", "case_id": case_id, "reason": f"无效的审查阶段：{stage}"}
    submitted = connection.execute(
        "SELECT 1 FROM stage_runs WHERE case_id=? AND stage=?", (case_id, stage)
    ).fetchone()
    if submitted:
        return {"action": "awaiting_next", "case_id": case_id, "stage": stage}
    return {
        "action": stage,
        "executor": "spec-review",
        "case_id": case_id,
        "stage": stage,
        "instructions": STAGE_INSTRUCTIONS[stage],
        "coverage": _coverage(connection, case),
    }


def submit_stage(connection: sqlite3.Connection, case_id: str, stage: str, raw_result: str) -> dict:
    case = _get_case(connection, case_id)
    if stage != case["stage"] or stage not in STAGES:
        raise ValueError(f"阶段结果被拒绝：当前阶段是 {case['stage']}，实际提交的是 {stage}")
    try:
        result = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        raise ValueError(f"result 不是有效的 JSON：{exc.msg}") from exc
    if not isinstance(result, dict):
        raise ValueError("阶段结果必须是 JSON 对象")
    _validate_stage_result(connection, case, stage, result)
    run_id = stable_id("RUN", case_id, stage)
    try:
        connection.execute(
            "INSERT INTO stage_runs VALUES(?,?,?,?,?)",
            (run_id, case_id, stage, json_dumps(result), now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"该阶段已经提交过结果：{stage}") from exc
    connection.execute("UPDATE review_cases SET updated_at=? WHERE case_id=?", (now(), case_id))
    connection.commit()
    return {"accepted": True, "case_id": case_id, "stage": stage, "next": "call_spec_review_next"}


def advance(connection: sqlite3.Connection, case_id: str) -> dict:
    case = _get_case(connection, case_id)
    stage = case["stage"]
    row = connection.execute(
        "SELECT result_json FROM stage_runs WHERE case_id=? AND stage=?", (case_id, stage)
    ).fetchone()
    if not row:
        raise ValueError(f"提交阶段结果前不能推进流程：{stage}")
    result = json.loads(row["result_json"])
    _validate_stage_result(connection, case, stage, result)
    next_stage = _next_stage(case["mode"], stage, result)
    connection.execute(
        "UPDATE review_cases SET stage=?,updated_at=? WHERE case_id=?", (next_stage, now(), case_id)
    )
    connection.commit()
    return {"case_id": case_id, "previous_stage": stage, "next_action": action_packet(connection, case_id)}


def context(
    connection: sqlite3.Connection, repo: Path, case_id: str, claim_id: Optional[str],
    gap_id: Optional[str], direction: str, max_nodes: int, cursor: int = 0,
    limit: int = 3, query: Optional[str] = None,
) -> dict:
    case = _get_case(connection, case_id)
    budget = json.loads(case["budget_json"])
    allowed = budget["max_investigation_nodes"] if case["stage"] == "l4_investigate" else budget["max_context_nodes"]
    bounded = min(max(1, int(max_nodes)), int(allowed))
    page_limit = min(max(1, int(limit)), int(budget["max_claims_per_page"]))
    stored_scope = json.loads(case["scope_json"])
    analysis_repo = Path(stored_scope.get("analysis_root") or repo)
    packet = build_context_packs(
        connection, analysis_repo, case_id, claim_id, direction, bounded,
        cursor=max(0, int(cursor)), limit=page_limit, query=query,
    )
    packet["requested_gap_id"] = gap_id
    visible_claim_ids = {pack["claim"]["claim_id"] for pack in packet["packs"]}
    packet["prior_stage_results"] = {
        row["stage"]: _filter_prior_result(json.loads(row["result_json"]), visible_claim_ids)
        for row in connection.execute(
            "SELECT stage,result_json FROM stage_runs WHERE case_id=? ORDER BY submitted_at", (case_id,)
        )
    }
    return packet


def _next_stage(mode: str, stage: str, result: dict) -> str:
    if stage == "l3_review":
        if mode == "fast":
            return "ready_to_finish"
        return "l4_initial" if _needs_deep_review(result) else "ready_to_finish"
    return {
        "l4_initial": "l4_challenge",
        "l4_challenge": "l4_investigate",
        "l4_investigate": "l4_converge",
        "l4_converge": "ready_to_finish",
    }[stage]


def _needs_deep_review(result: dict) -> bool:
    if result.get("escalate") is True:
        return True
    verdicts = []
    for item in result.get("claims", result.get("results", [])):
        if isinstance(item, dict):
            verdicts.append(item.get("verdict") or item.get("status"))
    return any(value in {"inconsistent", "uncertain"} for value in verdicts)


def _filter_prior_result(result: dict, visible_claim_ids: set[str]) -> dict:
    bounded = {
        key: value for key, value in result.items()
        if key not in {"claims", "results", "final_verdicts"}
    }
    for key in ("claims", "results", "final_verdicts"):
        if isinstance(result.get(key), list):
            bounded[key] = [
                item for item in result[key]
                if isinstance(item, dict) and item.get("claim_id") in visible_claim_ids
            ]
            break
    return bounded


def _stage_items(result: dict, stage: str) -> list[dict]:
    keys = ["final_verdicts", "claims", "results"] if stage == "l4_converge" else ["claims", "results"]
    for key in keys:
        value = result.get(key)
        if value is not None:
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise ValueError(f"{stage}.{key} 必须是对象数组")
            return value
    raise ValueError(f"{stage} 缺少逐条 claims/results 结果")


def _expected_claim_ids(connection, case, stage: str) -> list[str]:
    rows = connection.execute(
        "SELECT claim_id FROM claims WHERE case_id=? ORDER BY ordinal", (case["case_id"],)
    ).fetchall()
    all_ids = [row["claim_id"] for row in rows]
    if stage == "l3_review":
        return all_ids
    l3 = connection.execute(
        "SELECT result_json FROM stage_runs WHERE case_id=? AND stage='l3_review'", (case["case_id"],)
    ).fetchone()
    if not l3:
        return all_ids if case["mode"] == "deep" else []
    items = _stage_items(json.loads(l3["result_json"]), "l3_review")
    candidates = {
        item["claim_id"] for item in items
        if (item.get("verdict") or item.get("status")) in {"inconsistent", "uncertain"}
    }
    return [claim_id for claim_id in all_ids if claim_id in candidates]


def _validate_stage_result(connection, case, stage: str, result: dict) -> None:
    items = _stage_items(result, stage)
    expected = _expected_claim_ids(connection, case, stage)
    actual = []
    for item in items:
        claim_id = item.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.startswith("CLAIM-"):
            raise ValueError(f"{stage} 包含非法 claim_id：{claim_id!r}")
        if claim_id in actual:
            raise ValueError(f"{stage} 重复提交需求声明：{claim_id}")
        actual.append(claim_id)
        verdict = item.get("verdict") or item.get("status")
        if verdict not in VERDICTS:
            raise ValueError(f"{claim_id} 的 verdict 非法：{verdict!r}")
        evidence_ids = item.get("evidence_ids") or item.get("evidence") or []
        if not isinstance(evidence_ids, list) or not all(isinstance(value, str) for value in evidence_ids):
            raise ValueError(f"{claim_id}.evidence_ids 必须是字符串数组")
        for evidence_id in evidence_ids:
            owned = connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id=? AND case_id=? AND claim_id=?",
                (evidence_id, case["case_id"], claim_id),
            ).fetchone()
            if not owned:
                raise ValueError(f"证据不属于当前案例或声明：{evidence_id} -> {claim_id}")
        if verdict in {"consistent", "inconsistent"} and not evidence_ids:
            raise ValueError(f"{claim_id} 判定为 {verdict} 时必须引用代码证据")
    missing = [claim_id for claim_id in expected if claim_id not in actual]
    extra = [claim_id for claim_id in actual if claim_id not in set(expected)]
    if missing or extra:
        raise ValueError(
            f"{stage} 覆盖率门禁失败：expected={len(expected)} actual={len(actual)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )


def _coverage(connection, case) -> dict:
    coverage_stage = case["stage"]
    if coverage_stage not in STAGES:
        coverage_stage = "l4_converge" if connection.execute(
            "SELECT 1 FROM stage_runs WHERE case_id=? AND stage='l4_converge'", (case["case_id"],)
        ).fetchone() else "l3_review"
    expected = _expected_claim_ids(connection, case, coverage_stage)
    row = connection.execute(
        "SELECT result_json FROM stage_runs WHERE case_id=? AND stage=?",
        (case["case_id"], coverage_stage),
    ).fetchone()
    submitted = 0
    if row:
        try:
            submitted = len(_stage_items(json.loads(row["result_json"]), coverage_stage))
        except ValueError:
            submitted = 0
    return {"expected": len(expected), "submitted": submitted, "remaining": max(0, len(expected) - submitted)}


def _seed_scoped_symbols(connection, case_id: str, snapshot_id: str, patterns: list[str]) -> None:
    rows = connection.execute(
        "SELECT s.symbol_id,s.start_line,s.end_line,f.path FROM symbols s JOIN files f USING(file_id) "
        "WHERE s.snapshot_id=? ORDER BY f.path,s.start_line", (snapshot_id,)
    ).fetchall()
    for row in rows:
        if not any(fnmatch.fnmatch(row["path"], pattern) or row["path"].startswith(pattern.rstrip("*/")) for pattern in patterns):
            continue
        seed_id = stable_id("SEED", case_id, row["path"], row["symbol_id"])
        connection.execute(
            "INSERT OR IGNORE INTO change_seeds VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seed_id, case_id, row["path"], None, None, row["start_line"],
             row["end_line"] - row["start_line"] + 1, row["symbol_id"], "scoped", ""),
        )


def _get_case(connection, case_id: Optional[str]):
    if case_id:
        row = connection.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
    else:
        row = connection.execute("SELECT * FROM review_cases ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError(f"未找到审查案例：{case_id or '<最近一个>'}")
    return row


def _previous_pull_case(connection, pull: dict[str, Any]):
    rows = connection.execute(
        "SELECT case_id,head_revision,scope_json FROM review_cases "
        "WHERE status IN ('completed','incomplete') ORDER BY created_at DESC"
    ).fetchall()
    for row in rows:
        try:
            previous = json.loads(row["scope_json"]).get("pull_request") or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            previous.get("owner", "").lower() == str(pull["owner"]).lower()
            and previous.get("repo", "").lower() == str(pull["repo"]).lower()
            and previous.get("number") == pull["number"]
        ):
            return row
    return None


def _incremental_delta(repo: Path, previous_head: str, current_head: str) -> dict:
    if previous_head == current_head:
        return {"changed_files": [], "changed_file_count": 0, "note": "PR head 未变化；复用既有索引。"}
    try:
        output = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", f"{previous_head}..{current_head}", "--"],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {
            "changed_files": [], "changed_file_count": None,
            "note": "无法建立前次 head 到当前 head 的增量范围；本次仍执行完整 PR 审查。",
        }
    files = [line for line in output.splitlines() if line]
    return {
        "changed_files": files[:200], "changed_file_count": len(files),
        "truncated": len(files) > 200,
        "note": "索引按文件哈希增量复用；最终结论仍覆盖完整 PR 差异。",
    }


def _string_list(value, name: str, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise ValueError(f"缺少必填参数 {name}")
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{name} 必须是由非空字符串组成的数组")
    return [item.strip() for item in value]


def _optional_string(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("版本引用必须是非空字符串")
    return value.strip()
