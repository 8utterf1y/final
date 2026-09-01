from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from pathlib import Path
from typing import Optional

from .util import now, stable_id


DEFAULT_PAGE_SIZE = 3
MAX_PAGE_SIZE = 10
MAX_SOURCE_EVIDENCE = 5
MAX_DIFF_EVIDENCE = 4
MAX_SOURCE_CHARS = 2400
MAX_GRAPH_EDGES = 24
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}")


def build_context_packs(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    claim_id: Optional[str],
    direction: str,
    max_nodes: int,
    cursor: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    query: Optional[str] = None,
) -> dict:
    """Return a bounded page of claim-specific evidence packs.

    The old implementation returned every claim multiplied by every repository seed.
    This implementation pages claims and ranks a small evidence set independently for
    each claim, keeping tool output bounded and preserving coverage metadata.
    """
    case = _case(connection, case_id)
    target_ids = _target_claim_ids(connection, case)
    all_claims = _claims(connection, case_id, target_ids)
    if claim_id:
        all_claims = [row for row in all_claims if row["claim_id"] == claim_id]
        if not all_claims:
            raise ValueError(f"需求声明不属于当前阶段或当前审查案例：{claim_id}")
        cursor = 0

    cursor = max(0, int(cursor))
    limit = min(MAX_PAGE_SIZE, max(1, int(limit)))
    page = all_claims[cursor:cursor + limit]
    seeds = connection.execute(
        "SELECT * FROM change_seeds WHERE case_id=? ORDER BY path,new_start", (case_id,)
    ).fetchall()

    packs = []
    for claim in page:
        if claim["verifiability"] == "metadata":
            packs.append({
                "claim": {
                    "claim_id": claim["claim_id"], "section": claim["section"],
                    "source_text": claim["source_text"], "statement": claim["statement"],
                    "verifiability": claim["verifiability"], "ordinal": claim["ordinal"],
                },
                "review_type": "comparison" if case["base_revision"] else "snapshot",
                "change_summary": [], "graph": {"symbols": [], "edges": [], "gaps": []},
                "evidence": [], "gaps": [],
            })
            continue
        ranked = _rank_seed_symbols(
            connection, repo, case["snapshot_id"], claim, seeds, query,
        )
        initial_symbols = [item["symbol_id"] for item in ranked if item["symbol_id"]][:max_nodes]
        graph = _bounded_graph(connection, case["snapshot_id"], initial_symbols, direction, max_nodes)
        source_ids = list(dict.fromkeys([*initial_symbols, *graph["symbols"]]))[:MAX_SOURCE_EVIDENCE]
        evidence = []

        if case["base_revision"]:
            for seed in _rank_diff_seeds(claim, seeds, query)[:MAX_DIFF_EVIDENCE]:
                if not seed["diff_text"].strip():
                    continue
                evidence.append(_persist_evidence(
                    connection, case, claim["claim_id"], "diff", seed["path"],
                    seed["new_start"], seed["new_start"] + max(seed["new_count"] - 1, 0),
                    seed["diff_text"], {"seed_id": seed["seed_id"], "change_type": seed["change_type"]},
                ))

        for symbol_id in source_ids:
            source = _source_for_symbol(connection, repo, case["snapshot_id"], symbol_id)
            if not source:
                continue
            evidence.append(_persist_evidence(
                connection, case, claim["claim_id"], "source", source["path"],
                source["start_line"], source["end_line"], source["content"],
                {
                    "symbol_id": symbol_id,
                    "symbol_name": source["name"],
                    "symbol_kind": source["kind"],
                    "precision": source["precision"],
                    "truncated": source["truncated"],
                },
            ))

        packs.append({
            "claim": {
                "claim_id": claim["claim_id"],
                "section": claim["section"],
                "source_text": claim["source_text"],
                "statement": claim["statement"],
                "verifiability": claim["verifiability"],
                "ordinal": claim["ordinal"],
            },
            "review_type": "comparison" if case["base_revision"] else "snapshot",
            "change_summary": [
                {
                    "seed_id": item["seed_id"], "path": item["path"],
                    "change_type": item["change_type"], "new_start": item["new_start"],
                    "symbol_id": item["symbol_id"], "relevance_score": item["score"],
                }
                for item in ranked[:MAX_SOURCE_EVIDENCE]
            ],
            "graph": graph,
            "evidence": evidence,
            "gaps": graph["gaps"],
        })

    connection.commit()
    next_cursor = cursor + len(page)
    return {
        "case_id": case_id,
        "stage": case["stage"],
        "snapshot_id": case["snapshot_id"],
        "review_type": "comparison" if case["base_revision"] else "snapshot",
        "page": {
            "cursor": cursor,
            "limit": limit,
            "returned": len(page),
            "total": len(all_claims),
            "next_cursor": next_cursor if next_cursor < len(all_claims) else None,
        },
        "packs": packs,
    }


def _claims(connection, case_id, target_ids):
    rows = connection.execute(
        "SELECT * FROM claims WHERE case_id=? ORDER BY ordinal", (case_id,)
    ).fetchall()
    if target_ids is None:
        return rows
    allowed = set(target_ids)
    return [row for row in rows if row["claim_id"] in allowed]


def _target_claim_ids(connection, case):
    if case["stage"] == "l3_review":
        return None
    row = connection.execute(
        "SELECT result_json FROM stage_runs WHERE case_id=? AND stage='l3_review'",
        (case["case_id"],),
    ).fetchone()
    if not row:
        return None if case["mode"] == "deep" else []
    result = json.loads(row["result_json"])
    items = result.get("claims") or result.get("results") or []
    return [
        item.get("claim_id") for item in items
        if isinstance(item, dict) and (item.get("verdict") or item.get("status")) in {"uncertain", "inconsistent"}
    ]


def _rank_seed_symbols(connection, repo, snapshot_id, claim, seeds, query):
    tokens = _tokens(" ".join(filter(None, [claim["statement"], claim["section"], query or ""])))
    rows = []
    seen = set()
    for seed in seeds:
        symbol_id = seed["symbol_id"]
        if not symbol_id or symbol_id in seen:
            continue
        seen.add(symbol_id)
        symbol = connection.execute(
            "SELECT s.*,f.path FROM symbols s JOIN files f USING(file_id) "
            "WHERE s.snapshot_id=? AND s.symbol_id=?",
            (snapshot_id, symbol_id),
        ).fetchone()
        if not symbol:
            continue
        source = _source_for_symbol(connection, repo, snapshot_id, symbol_id)
        haystack = " ".join([
            symbol["name"], symbol["qualified_name"], symbol["path"],
            source["content"][:4000] if source else "",
        ]).casefold()
        score = sum(4 if token in symbol["name"].casefold() else 1 for token in tokens if token in haystack)
        if symbol["kind"] in {"constant", "variable"}:
            score += 1
        if symbol["kind"] in {"function", "method"}:
            score += 0.5
        rows.append({
            "seed_id": seed["seed_id"], "path": seed["path"],
            "change_type": seed["change_type"], "new_start": seed["new_start"],
            "symbol_id": symbol_id, "score": score,
        })
    return sorted(rows, key=lambda item: (-item["score"], item["path"], item["new_start"] or 0))


def _rank_diff_seeds(claim, seeds, query):
    tokens = _tokens(" ".join(filter(None, [claim["statement"], claim["section"], query or ""])))
    ranked = []
    for seed in seeds:
        if not seed["diff_text"].strip():
            continue
        haystack = f"{seed['path']} {seed['diff_text']}".casefold()
        score = sum(1 for token in tokens if token in haystack)
        ranked.append((score, seed))
    return [item[1] for item in sorted(ranked, key=lambda item: (-item[0], item[1]["path"]))]


def _tokens(text):
    result = set()
    for value in TOKEN_RE.findall(text):
        normalized = value.casefold()
        if re.fullmatch(r"[\u4e00-\u9fff]+", normalized):
            result.update(normalized[index:index + 2] for index in range(max(0, len(normalized) - 1)))
        else:
            result.add(normalized)
            result.update(part for part in normalized.split("_") if len(part) >= 2)
    return result


def _bounded_graph(connection, snapshot_id, seeds, direction, max_nodes):
    bounded_seeds = list(dict.fromkeys(seeds))[:max_nodes]
    visited = set(bounded_seeds)
    queue = deque((symbol_id, 0) for symbol_id in bounded_seeds)
    edges = []
    gaps = []
    while queue and len(visited) <= max_nodes:
        current, depth = queue.popleft()
        clauses = []
        params = [snapshot_id]
        if direction in {"both", "callees"}:
            clauses.append("source_symbol_id=?")
            params.append(current)
        if direction in {"both", "callers"}:
            clauses.append("target_symbol_id=?")
            params.append(current)
        if not clauses:
            break
        rows = connection.execute(
            "SELECT * FROM edges WHERE snapshot_id=? AND (" + " OR ".join(clauses) + ") "
            "ORDER BY confidence DESC LIMIT 100", tuple(params),
        ).fetchall()
        for row in rows:
            edge = {key: row[key] for key in row.keys() if key != "snapshot_id"}
            edges.append(edge)
            if row["resolution_status"] in {"ambiguous", "unresolved"}:
                gaps.append({
                    "kind": "unresolved_edge", "edge_id": row["edge_id"],
                    "target_name": row["target_name"], "status": row["resolution_status"],
                })
            neighbor = row["target_symbol_id"] if row["source_symbol_id"] == current else row["source_symbol_id"]
            if neighbor and neighbor not in visited and len(visited) < max_nodes:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
    if queue or len(set(seeds)) > max_nodes:
        gaps.append({"kind": "budget_limit", "max_nodes": max_nodes, "seed_count": len(set(seeds))})
    return {
        "symbols": sorted(visited),
        "edges": edges[:MAX_GRAPH_EDGES],
        "gaps": gaps[:MAX_GRAPH_EDGES],
        "edges_truncated": max(0, len(edges) - MAX_GRAPH_EDGES),
    }


def _source_for_symbol(connection, repo, snapshot_id, symbol_id):
    row = connection.execute(
        "SELECT s.*,f.path FROM symbols s JOIN files f USING(file_id) "
        "WHERE s.snapshot_id=? AND s.symbol_id=?", (snapshot_id, symbol_id),
    ).fetchone()
    if not row:
        return None
    path = repo / row["path"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start, end = row["start_line"], row["end_line"]
    selected = lines[start - 1:end]
    rendered = []
    rendered_chars = 0
    for line in selected:
        added = len(line) + (1 if rendered else 0)
        if rendered and rendered_chars + added > MAX_SOURCE_CHARS:
            break
        rendered.append(line)
        rendered_chars += added
    actual_end = start + max(0, len(rendered) - 1)
    return {
        "path": row["path"], "name": row["name"], "kind": row["kind"],
        "start_line": start, "end_line": actual_end,
        "content": "\n".join(rendered), "precision": row["precision"],
        "truncated": actual_end < end,
    }


def _persist_evidence(connection, case, claim_id, kind, path, start, end, content, metadata):
    evidence_id = stable_id("EVID", case["case_id"], claim_id, kind, path, start, end, content)
    connection.execute(
        "INSERT OR IGNORE INTO evidence("
        "evidence_id,case_id,claim_id,kind,path,start_line,end_line,revision,content,metadata_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (evidence_id, case["case_id"], claim_id, kind, path, start, end,
         case["head_revision"], content, json.dumps(metadata, ensure_ascii=False), now()),
    )
    return {
        "evidence_id": evidence_id, "kind": kind, "path": path,
        "start_line": start, "end_line": end, "content": content, "metadata": metadata,
    }


def _case(connection, case_id):
    row = connection.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
    if not row:
        raise ValueError(f"未找到审查案例：{case_id}")
    return row
