from __future__ import annotations

import json
import re
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
    final, coverage = _assemble_final(connection, case_id, runs)
    completed = coverage["remaining"] == 0
    payload = {
        "schema_version": "0.3",
        "tool": "opencode-spec-review",
        "case_id": case_id,
        "repo": case["repo"],
        "base_revision": case["base_revision"],
        "head_revision": case["head_revision"],
        "mode": case["mode"],
        "scope": json.loads(case["scope_json"]),
        "result": final,
        "coverage": coverage,
        "stage_results": runs,
        "generated_at": now(),
    }
    report_dir = repo / ".spec-review" / "reports" / case_id
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "review.json"
    markdown_path = report_dir / "review.md"
    sarif_path = report_dir / "review.sarif"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(connection, case_id, payload), encoding="utf-8")
    sarif_path.write_text(
        json.dumps(_sarif(connection, case_id, payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    connection.execute(
        "UPDATE review_cases SET stage=?,status=?,updated_at=? WHERE case_id=?",
        ("finished" if completed else "coverage_incomplete",
         "completed" if completed else "incomplete", now(), case_id),
    )
    connection.commit()
    return {
        "case_id": case_id,
        "status": "completed" if completed else "incomplete",
        "markdown_report": str(markdown_path),
        "json_report": str(json_path),
        "sarif_report": str(sarif_path),
        "report": markdown_path.read_text(encoding="utf-8"),
    }


def _assemble_final(connection, case_id: str, runs: dict) -> tuple[dict, dict]:
    claim_rows = connection.execute(
        "SELECT claim_id,section,source_text,statement,ordinal FROM claims WHERE case_id=? ORDER BY ordinal",
        (case_id,),
    ).fetchall()
    l3 = _items(runs.get("l3_review") or {}, "l3_review")
    deep = _items(runs.get("l4_converge") or {}, "l4_converge")
    by_id = {item.get("claim_id"): item for item in l3 if isinstance(item.get("claim_id"), str)}
    by_id.update({item.get("claim_id"): item for item in deep if isinstance(item.get("claim_id"), str)})
    claims = []
    for row in claim_rows:
        item = by_id.get(row["claim_id"])
        if not item:
            continue
        normalized = dict(item)
        normalized["claim_id"] = row["claim_id"]
        normalized.setdefault("section", row["section"])
        normalized.setdefault("source_text", row["source_text"])
        normalized.setdefault("statement", row["statement"])
        normalized["verdict"] = normalized.get("verdict") or normalized.get("status")
        claims.append(normalized)
    expected_ids = [row["claim_id"] for row in claim_rows]
    submitted_ids = {item["claim_id"] for item in claims}
    missing = [claim_id for claim_id in expected_ids if claim_id not in submitted_ids]
    counts = {name: 0 for name in ("consistent", "inconsistent", "uncertain", "not_applicable")}
    for item in claims:
        if item["verdict"] in counts:
            counts[item["verdict"]] += 1
    coverage = {
        "expected": len(expected_ids), "submitted": len(submitted_ids),
        "remaining": len(missing), "missing_claim_ids": missing,
    }
    summary = (
        f"审查完成：consistent={counts['consistent']}，inconsistent={counts['inconsistent']}，"
        f"uncertain={counts['uncertain']}，not_applicable={counts['not_applicable']}。"
        if not missing else
        f"审查未完成：仅覆盖 {len(submitted_ids)}/{len(expected_ids)} 条需求，缺少 {len(missing)} 条。"
    )
    findings = [item for item in claims if item["verdict"] == "inconsistent"]
    root_findings = _root_findings(findings)
    return {
        "summary": summary,
        "verdict_counts": counts,
        "claims": claims,
        "findings": findings,
        "root_findings": root_findings,
    }, coverage


def _items(result: dict, stage: str) -> list[dict]:
    keys = ["final_verdicts", "claims", "results"] if stage == "l4_converge" else ["claims", "results"]
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _root_findings(findings: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in findings:
        groups.setdefault(_root_key(item), []).append(item)
    roots = []
    for index, items in enumerate(groups.values(), 1):
        primary = _select_primary_finding(items)
        claim_ids = [str(item.get("claim_id")) for item in items if item.get("claim_id")]
        evidence_ids = []
        for item in items:
            evidence_ids.extend(item.get("evidence_ids") or item.get("evidence") or [])
        unique_evidence = list(dict.fromkeys(str(value) for value in evidence_ids if value))
        root = dict(primary)
        root["root_id"] = primary.get("root_id") or f"ROOT-{index:03d}"
        root["affected_claim_ids"] = claim_ids
        root["affected_claim_count"] = len(claim_ids)
        root["evidence_ids"] = unique_evidence[:10]
        if len(items) > 1:
            root["summary"] = _root_summary(items, primary)
        roots.append(root)
    return roots


def _root_key(item: dict) -> str:
    explicit = item.get("root_cause_id") or item.get("root_id") or item.get("issue_id")
    if explicit:
        return f"id:{explicit}"
    text = " ".join(str(item.get(key) or "") for key in ("title", "reasoning", "reason", "summary", "description"))
    normalized = text.casefold()
    code_markers = [
        r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)",
        r"(claim_for_payment)",
        r"(PaymentWorker\.process|PayoutWorker\.process)",
        r"(CANCELLED\s*[-=]?>\s*PAYING)",
    ]
    markers = []
    for pattern in code_markers:
        markers.extend(match.group(1).casefold() for match in re.finditer(pattern, text))
    if markers:
        return "markers:" + "|".join(sorted(set(markers)))
    tokens = re.findall(r"[a-z_][a-z0-9_]{2,}", normalized)
    stop = {"claim", "evid", "consistent", "inconsistent", "critical", "introduced", "unattributed"}
    meaningful = [token for token in tokens if token not in stop][:8]
    return "text:" + "|".join(meaningful)


def _select_primary_finding(items: list[dict]) -> dict:
    return sorted(
        items,
        key=lambda item: (
            _severity_rank(item.get("severity")),
            -len(item.get("evidence_ids") or item.get("evidence") or []),
            str(item.get("claim_id") or ""),
        ),
    )[0]


def _severity_rank(value) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(str(value or "").lower(), 5)


def _root_summary(items: list[dict], primary: dict) -> str:
    base = str(
        primary.get("summary") or primary.get("reasoning") or primary.get("reason") or primary.get("description") or ""
    ).strip()
    suffix = f"（同一根因影响 {len(items)} 条需求声明，已在逐声明结果中保留覆盖明细。）"
    return f"{base}{suffix}" if base else suffix


def _markdown(connection: sqlite3.Connection, case_id: str, payload: dict) -> str:
    result = payload["result"] if isinstance(payload["result"], dict) else {}
    findings = result.get("findings")
    if not isinstance(findings, list):
        findings = result.get("issues") or result.get("claims") or []
    root_findings = result.get("root_findings")
    if not isinstance(root_findings, list):
        root_findings = findings
    counts = result.get("verdict_counts") or {}
    scope_paths = payload["scope"].get("paths") or []
    scope_text = "、".join(str(path) for path in scope_paths) if scope_paths else (
        "全仓" if payload["scope"].get("full_repository") else "未限定路径"
    )
    lines = [
        "# 需求—代码一致性审查报告",
        "",
        f"案例：`{payload['case_id']}`  ",
        f"范围：{scope_text}  ",
        f"覆盖率：{payload['coverage']['submitted']}/{payload['coverage']['expected']}  ",
        "",
        "## 审查结论",
        "",
    ]
    pull = payload["scope"].get("pull_request")
    if isinstance(pull, dict):
        lines[2:2] = [
            f"MR/PR：[{pull.get('owner')}/{pull.get('repo')}#{pull.get('number')}]({pull.get('html_url')})  ",
            f"分支：`{pull.get('base_ref')}` <- `{pull.get('head_ref')}`  ",
        ]
    if not root_findings:
        lines.extend([
            "未发现需求未完成或实现不一致项。",
            "",
            f"统计：consistent={counts.get('consistent', 0)}，uncertain={counts.get('uncertain', 0)}，not_applicable={counts.get('not_applicable', 0)}。",
            "",
            "详细证据和逐条覆盖结果见 `review.json`。",
        ])
        return "\n".join(lines) + "\n"
    lines.extend([
        f"发现 {len(root_findings)} 个需要处理的问题，影响 {counts.get('inconsistent', len(findings))} 条需求声明。",
        "",
        "## 未完成或不一致项",
        "",
    ])
    for index, item in enumerate(root_findings, 1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or _short_requirement(item) or f"问题 {index}"
        requirement = _requirement_text(item)
        reason = str(
            item.get("user_summary") or item.get("summary") or item.get("reasoning") or
            item.get("reason") or item.get("description") or ""
        ).strip()
        evidence = _evidence_locations(connection, case_id, item.get("evidence_ids") or item.get("evidence") or [])
        lines.extend([
            f"### {index}. {title}", "",
            f"- 对应需求：{requirement}",
            f"- 未完成点：{reason or '实现与需求存在不一致，详见结构化报告。'}",
            f"- 严重级别：{item.get('severity') or 'unspecified'}",
        ])
        if evidence:
            lines.append("- 关键证据：" + "；".join(evidence[:5]))
        affected = item.get("affected_claim_ids") or []
        if affected:
            lines.append(f"- 影响范围：{len(affected)} 条需求声明")
        if isinstance(item.get("suggested_patch"), str) and item["suggested_patch"].strip():
            lines.extend(["", "建议 Patch（不会自动应用）：", "", "```diff", item["suggested_patch"].rstrip(), "```"])
        lines.append("")
    lines.extend([
        "## 备注",
        "",
        "逐条覆盖结果、证据 ID 和审查阶段记录保留在 `review.json`；本报告只展示需要用户处理的问题。",
    ])
    return "\n".join(lines) + "\n"


def _requirement_text(item: dict) -> str:
    section = str(item.get("section") or "").strip()
    statement = str(item.get("statement") or item.get("source_text") or "").strip()
    if section and statement:
        return f"{section}：{statement}"
    return section or statement or str(item.get("claim_id") or "未提供")


def _short_requirement(item: dict) -> str:
    text = re.sub(r"\s+", " ", _requirement_text(item))
    return text[:80] + ("..." if len(text) > 80 else "")


def _evidence_locations(connection: sqlite3.Connection, case_id: str, evidence_ids: list) -> list[str]:
    locations = []
    for evidence_id in evidence_ids:
        row = connection.execute(
            "SELECT path,start_line,end_line FROM evidence WHERE case_id=? AND evidence_id=?",
            (case_id, str(evidence_id)),
        ).fetchone()
        if not row or not row["path"]:
            continue
        line = ""
        if row["start_line"]:
            line = f":{row['start_line']}"
            if row["end_line"] and row["end_line"] != row["start_line"]:
                line += f"-{row['end_line']}"
        locations.append(f"`{row['path']}{line}`")
    return list(dict.fromkeys(locations))


def _sarif(connection: sqlite3.Connection, case_id: str, payload: dict) -> dict:
    rules = []
    results = []
    for finding in payload["result"].get("findings", []):
        claim_id = str(finding.get("claim_id") or "SPEC-REVIEW")
        title = str(finding.get("title") or claim_id)
        rules.append({
            "id": claim_id,
            "name": "RequirementCodeInconsistency",
            "shortDescription": {"text": title},
        })
        evidence = None
        for evidence_id in finding.get("evidence_ids") or finding.get("evidence") or []:
            row = connection.execute(
                "SELECT path,start_line,end_line FROM evidence WHERE case_id=? AND evidence_id=?",
                (case_id, evidence_id),
            ).fetchone()
            if row and row["path"] and row["start_line"]:
                evidence = row
                break
        result = {
            "ruleId": claim_id,
            "level": _sarif_level(finding.get("severity")),
            "message": {"text": str(
                finding.get("reasoning") or finding.get("reason") or finding.get("summary") or title
            )},
            "properties": {
                "case_id": case_id,
                "attribution": finding.get("attribution") or "unattributed",
            },
        }
        if evidence:
            result["locations"] = [{
                "physicalLocation": {
                    "artifactLocation": {"uri": evidence["path"]},
                    "region": {
                        "startLine": evidence["start_line"],
                        "endLine": evidence["end_line"] or evidence["start_line"],
                    },
                }
            }]
        results.append(result)
    unique_rules = {rule["id"]: rule for rule in rules}
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "opencode-spec-review", "version": "0.4.0",
                "informationUri": "https://github.com/",
                "rules": list(unique_rules.values()),
            }},
            "results": results,
        }],
    }


def _sarif_level(severity) -> str:
    return {
        "critical": "error", "high": "error", "medium": "warning",
        "low": "note", "info": "note",
    }.get(str(severity or "").lower(), "warning")
