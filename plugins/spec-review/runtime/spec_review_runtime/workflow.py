from __future__ import annotations

import fnmatch
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .context import build_context_packs
from .documents import load_claim_candidates
from .indexer import build_or_update_index
from .scope import resolve_change_scope
from .util import json_dumps, now, stable_id


STAGES = {
    "l3_review", "l4_initial", "l4_challenge", "l4_investigate", "l4_converge",
}
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
    full_repo = payload.get("fullRepo") is True
    if not base and not paths and not full_repo:
        raise ValueError("审查范围不明确：请提供 base、paths，或显式设置 fullRepo=true")

    snapshot = build_or_update_index(connection, repo)
    head = _optional_string(payload.get("head")) or "HEAD"
    case_id = stable_id(
        "CASE", str(repo), snapshot["snapshot_id"], base or "", head,
        json_dumps(docs), json_dumps(paths), json_dumps(sections), mode, now(),
    )
    initial_stage = "l4_initial" if mode == "deep" else "l3_review"
    scope = {
        "documents": docs, "paths": paths, "sections": sections,
        "full_repository": full_repo,
    }
    budget = {"max_context_nodes": 40, "max_investigation_nodes": 120, "max_tool_rounds": 8}
    timestamp = now()
    connection.execute(
        "INSERT INTO review_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, str(repo), snapshot["snapshot_id"], None, snapshot["revision"], mode,
         initial_stage, "active", json_dumps(scope), json_dumps(budget), timestamp, timestamp),
    )
    change_scope = resolve_change_scope(
        connection, repo, case_id, snapshot["snapshot_id"], base, head, paths,
    )
    connection.execute(
        "UPDATE review_cases SET base_revision=?,head_revision=? WHERE case_id=?",
        (change_scope["base_revision"], change_scope["head_revision"], case_id),
    )
    if change_scope["seed_count"] == 0 and paths:
        _seed_scoped_symbols(connection, case_id, snapshot["snapshot_id"], paths)
    claims = load_claim_candidates(connection, repo, case_id, docs, sections)
    connection.commit()
    return {
        "repo": str(repo),
        "case_id": case_id,
        "index": snapshot,
        "scope": change_scope,
        "claims": len(claims),
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
    next_stage = _next_stage(case["mode"], stage, result)
    connection.execute(
        "UPDATE review_cases SET stage=?,updated_at=? WHERE case_id=?", (next_stage, now(), case_id)
    )
    connection.commit()
    return {"case_id": case_id, "previous_stage": stage, "next_action": action_packet(connection, case_id)}


def context(
    connection: sqlite3.Connection, repo: Path, case_id: str, claim_id: Optional[str],
    gap_id: Optional[str], direction: str, max_nodes: int,
) -> dict:
    case = _get_case(connection, case_id)
    budget = json.loads(case["budget_json"])
    allowed = budget["max_investigation_nodes"] if case["stage"] == "l4_investigate" else budget["max_context_nodes"]
    bounded = min(max(1, int(max_nodes)), int(allowed))
    packet = build_context_packs(connection, repo, case_id, claim_id, direction, bounded)
    packet["requested_gap_id"] = gap_id
    packet["prior_stage_results"] = {
        row["stage"]: json.loads(row["result_json"])
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
