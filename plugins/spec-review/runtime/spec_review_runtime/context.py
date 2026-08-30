from __future__ import annotations

import json
import sqlite3
from collections import deque
from pathlib import Path
from typing import Optional

from .util import now, stable_id


def build_context_packs(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    claim_id: Optional[str],
    direction: str,
    max_nodes: int,
) -> dict:
    case = _case(connection, case_id)
    claims = connection.execute(
        "SELECT * FROM claims WHERE case_id=? " + ("AND claim_id=? " if claim_id else "") + "ORDER BY ordinal",
        (case_id, claim_id) if claim_id else (case_id,),
    ).fetchall()
    if claim_id and not claims:
        raise ValueError(f"需求声明不属于当前审查案例：{claim_id}")
    seeds = connection.execute(
        "SELECT * FROM change_seeds WHERE case_id=? ORDER BY path,new_start", (case_id,)
    ).fetchall()
    seed_symbols = [row["symbol_id"] for row in seeds if row["symbol_id"]]
    graph = _bounded_graph(connection, case["snapshot_id"], seed_symbols, direction, max_nodes)
    source_cache: dict[str, dict] = {}
    evidence_rows = []
    for symbol_id in graph["symbols"]:
        source = _source_for_symbol(connection, repo, case["snapshot_id"], symbol_id)
        if source:
            source_cache[symbol_id] = source

    packs = []
    for claim in claims:
        evidence = []
        for seed in seeds:
            evidence.append(_persist_evidence(
                connection, case, claim["claim_id"], "diff", seed["path"],
                seed["new_start"], seed["new_start"] + max(seed["new_count"] - 1, 0),
                seed["diff_text"], {"seed_id": seed["seed_id"], "change_type": seed["change_type"]},
            ))
        for symbol_id, source in source_cache.items():
            evidence.append(_persist_evidence(
                connection, case, claim["claim_id"], "source", source["path"],
                source["start_line"], source["end_line"], source["content"],
                {"symbol_id": symbol_id, "precision": source["precision"]},
            ))
        packs.append({
            "claim": {
                "claim_id": claim["claim_id"], "section": claim["section"],
                "source_text": claim["source_text"], "statement": claim["statement"],
                "verifiability": claim["verifiability"],
            },
            "change_summary": [
                {"seed_id": row["seed_id"], "path": row["path"], "change_type": row["change_type"],
                 "new_start": row["new_start"], "symbol_id": row["symbol_id"]}
                for row in seeds
            ],
            "graph": graph,
            "evidence": evidence,
            "gaps": graph["gaps"],
        })
    connection.commit()
    return {
        "case_id": case_id,
        "stage": case["stage"],
        "snapshot_id": case["snapshot_id"],
        "packs": packs,
    }


def _bounded_graph(connection, snapshot_id, seeds, direction, max_nodes):
    visited = set(seeds)
    queue = deque((symbol_id, 0) for symbol_id in seeds)
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
            "ORDER BY confidence DESC LIMIT 100",
            tuple(params),
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
    if len(visited) >= max_nodes:
        gaps.append({"kind": "budget_limit", "max_nodes": max_nodes})
    return {"symbols": sorted(visited), "edges": edges, "gaps": gaps}


def _source_for_symbol(connection, repo, snapshot_id, symbol_id):
    row = connection.execute(
        "SELECT s.*,f.path FROM symbols s JOIN files f USING(file_id) "
        "WHERE s.snapshot_id=? AND s.symbol_id=?", (snapshot_id, symbol_id)
    ).fetchone()
    if not row:
        return None
    path = repo / row["path"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start, end = row["start_line"], row["end_line"]
    return {
        "path": row["path"], "start_line": start, "end_line": end,
        "content": "\n".join(lines[start - 1:end]), "precision": row["precision"],
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
        "start_line": start, "end_line": end, "content": content,
        "metadata": metadata,
    }


def _case(connection, case_id):
    row = connection.execute("SELECT * FROM review_cases WHERE case_id=?", (case_id,)).fetchone()
    if not row:
        raise ValueError(f"未找到审查案例：{case_id}")
    return row
