from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import now


def finish_case(connection: sqlite3.Connection, repo: Path, case_id: str) -> dict:
    case = connection.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
    if not case:
        raise ValueError(f"未找到审查案例：{case_id}")
    if case["stage"] not in {"ready_to_finish", "finished"}:
        raise ValueError(f"审查案例尚不能生成报告；当前阶段为 {case['stage']}")
    runs = {
        row["stage"]: json.loads(row["result_json"])
        for row in connection.execute(
            "SELECT stage,result_json FROM stage_runs WHERE case_id=? ORDER BY submitted_at", (case_id,)
        )
    }
    final = runs.get("l4_converge") or runs.get("l3_review") or {}
    payload = {
        "schema_version": "0.1",
        "tool": "opencode-spec-review",
        "case_id": case_id,
        "repo": case["repo"],
        "base_revision": case["base_revision"],
        "head_revision": case["head_revision"],
        "mode": case["mode"],
        "scope": json.loads(case["scope_json"]),
        "result": final,
        "stage_results": runs,
        "generated_at": now(),
    }
    report_dir = repo / ".spec-review" / "reports" / case_id
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "review.json"
    markdown_path = report_dir / "review.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    connection.execute(
        "UPDATE review_cases SET stage='finished',status='completed',updated_at=? WHERE case_id=?",
        (now(), case_id),
    )
    connection.commit()
    return {
        "case_id": case_id,
        "status": "completed",
        "markdown_report": str(markdown_path),
        "json_report": str(json_path),
        "report": markdown_path.read_text(encoding="utf-8"),
    }


def _markdown(payload: dict) -> str:
    result = payload["result"] if isinstance(payload["result"], dict) else {}
    findings = result.get("findings") or result.get("issues") or result.get("claims") or []
    lines = [
        "# 需求—代码一致性审查报告",
        "",
        f"- 案例 ID：`{payload['case_id']}`",
        f"- 审查模式：`{payload['mode']}`",
        f"- 基准版本：`{payload['base_revision'] or '未提供'}`",
        f"- 目标版本：`{payload['head_revision']}`",
        "",
        "## 审查结论",
        "",
        str(result.get("summary") or result.get("verdict") or "审查已完成，具体发现见下文。"),
        "",
        "## 问题清单",
        "",
    ]
    if not findings:
        lines.append("没有提交需要报告的问题。")
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("claim_id") or f"问题 {index}"
        lines.extend([
            f"### {index}. {title}", "",
            f"- 判定：`{item.get('verdict') or item.get('status') or 'unknown'}`",
            f"- 严重级别：`{item.get('severity') or 'unspecified'}`",
            f"- 变更归因：`{item.get('attribution') or 'unattributed'}`",
            "",
            str(item.get("reasoning") or item.get("summary") or item.get("description") or ""),
            "",
        ])
        evidence = item.get("evidence_ids") or item.get("evidence") or []
        if evidence:
            lines.append("证据：" + ", ".join(f"`{value}`" for value in evidence))
            lines.append("")
    lines.extend([
        "## 审查范围", "", "```json",
        json.dumps(payload["scope"], ensure_ascii=False, indent=2), "```", "",
    ])
    return "\n".join(lines)
