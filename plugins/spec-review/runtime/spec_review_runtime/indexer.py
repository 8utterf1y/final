from __future__ import annotations

import ast
import hashlib
import importlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .util import now, sha256_bytes, stable_id


LANGUAGES = {
    ".py": "python", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".hpp": "cpp", ".go": "go", ".rs": "rust", ".java": "java",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
}
SKIP_DIRS = {
    ".git", ".spec-review", ".specdiff", "node_modules", ".venv", "venv",
    "__pycache__", "dist", "build", "target", "vendor",
}


@dataclass(frozen=True)
class SymbolFact:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str
    backend: str
    precision: str


@dataclass(frozen=True)
class EdgeFact:
    edge_id: str
    source_symbol_id: Optional[str]
    target_name: str
    edge_type: str
    line: int
    resolver: str
    confidence: float
    resolution_status: str


def build_or_update_index(connection: sqlite3.Connection, repo: Path) -> dict:
    previous = connection.execute(
        "SELECT * FROM index_snapshots ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(previous["sequence"]) + 1 if previous else 1
    revision = git_revision(repo)
    snapshot_id = stable_id("SNAP", revision, sequence, now())
    files = list(_source_files(repo))
    previous_versions = {}
    if previous:
        previous_versions = {
            row["path"]: row
            for row in connection.execute(
                "SELECT f.path, f.file_id, fv.* FROM file_versions fv "
                "JOIN files f USING(file_id) WHERE fv.snapshot_id=?",
                (previous["snapshot_id"],),
            )
        }

    parsed = reused = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO index_snapshots VALUES(?,?,?,?,?,?,?)",
            (snapshot_id, sequence, revision, now(), len(files), 0, 0),
        )
        for path in files:
            rel = path.relative_to(repo).as_posix()
            data = path.read_bytes()
            digest = sha256_bytes(data)
            language = LANGUAGES[path.suffix.lower()]
            file_id = stable_id("FILE", rel)
            connection.execute(
                "INSERT INTO files(file_id,path,language,source_role) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET language=excluded.language, source_role=excluded.source_role",
                (file_id, rel, language, _source_role(rel)),
            )
            old = previous_versions.get(rel)
            if old and old["sha256"] == digest:
                _reuse_file_facts(connection, previous["snapshot_id"], snapshot_id, file_id)
                connection.execute(
                    "INSERT INTO file_versions VALUES(?,?,?,?,?,?,?)",
                    (snapshot_id, file_id, digest, len(data), _line_count(data), old["parser_backend"], old["parse_status"]),
                )
                reused += 1
                continue

            symbols, edges, backend, status = _parse_file(path, rel, language, data)
            connection.execute(
                "INSERT INTO file_versions VALUES(?,?,?,?,?,?,?)",
                (snapshot_id, file_id, digest, len(data), _line_count(data), backend, status),
            )
            _insert_facts(connection, snapshot_id, file_id, symbols, edges)
            parsed += 1

        _resolve_edges(connection, snapshot_id)
        connection.execute(
            "UPDATE index_snapshots SET files_parsed=?, files_reused=? WHERE snapshot_id=?",
            (parsed, reused, snapshot_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "snapshot_id": snapshot_id,
        "revision": revision,
        "files_total": len(files),
        "files_parsed": parsed,
        "files_reused": reused,
    }


def git_revision(repo: Path, revision: str = "HEAD") -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", revision],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "working-tree"


def _source_files(repo: Path) -> Iterable[Path]:
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LANGUAGES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(repo).parts):
            continue
        yield path


def _parse_file(path: Path, rel: str, language: str, data: bytes):
    tree_sitter_result = _parse_tree_sitter(rel, language, data)
    if tree_sitter_result is not None:
        return (*tree_sitter_result, "tree_sitter", "ok")
    if language == "python":
        try:
            return (*_parse_python(rel, data.decode("utf-8", errors="replace")), "python_ast", "ok")
        except SyntaxError:
            return [], [], "python_ast", "syntax_error"
    # 后续发行包会按平台加入 Tree-sitter 语法模块。在此之前，不支持的源码只登记
    # 文件信息，不会伪装成已经解析出的语义事实。
    return [], [], "tree_sitter_pending", "backend_unavailable"


def _parse_tree_sitter(rel: str, language: str, data: bytes):
    """原生 Tree-sitter 语法模块可用时，使用 Aider 风格标签查询解析源码。

    生产发行包会携带这些原生模块；源码检出环境会有意降级，运行时不会下载依赖。
    """
    module_name = {
        "python": "tree_sitter_python", "c": "tree_sitter_c", "cpp": "tree_sitter_cpp",
        "go": "tree_sitter_go", "rust": "tree_sitter_rust", "java": "tree_sitter_java",
        "javascript": "tree_sitter_javascript", "typescript": "tree_sitter_typescript",
    }.get(language)
    query_path = Path(__file__).resolve().parents[1] / "queries" / f"{language}-tags.scm"
    if not module_name or not query_path.exists():
        return None
    try:
        from tree_sitter import Language, Parser
        grammar = importlib.import_module(module_name)
        if language == "typescript":
            capsule = grammar.language_typescript()
        else:
            capsule = grammar.language()
        ts_language = Language(capsule)
        tree = Parser(ts_language).parse(data)
        captures = ts_language.query(query_path.read_text(encoding="utf-8")).captures(tree.root_node)
    except (ImportError, OSError, AttributeError, ValueError):
        return None

    capture_rows = [(node, tag) for tag, nodes in captures.items() for node in nodes]
    definitions = [
        (node, tag) for node, tag in capture_rows if tag.startswith("definition.")
    ]
    symbols: list[SymbolFact] = []
    definition_nodes = []
    for node, tag in capture_rows:
        if not tag.startswith("name.definition."):
            continue
        kind = tag.split(".")[-1]
        name = data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        containers = [
            candidate for candidate, candidate_tag in definitions
            if candidate_tag.endswith(kind)
            and candidate.start_byte <= node.start_byte <= node.end_byte <= candidate.end_byte
        ]
        container = min(containers, key=lambda item: item.end_byte - item.start_byte) if containers else node
        qualified = name
        signature = ""
        symbol = SymbolFact(
            symbol_id=stable_id("SYM", language, rel, qualified, signature, node.start_point[0] + 1),
            name=name, qualified_name=qualified, kind=kind,
            start_line=node.start_point[0] + 1, end_line=container.end_point[0] + 1,
            signature=signature, backend="tree_sitter", precision="syntax",
        )
        symbols.append(symbol)
        definition_nodes.append((container, symbol))

    edges: list[EdgeFact] = []
    for node, tag in capture_rows:
        if not tag.startswith("name.reference.") or tag.split(".")[-1] != "call":
            continue
        name = data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        callers = [
            (container, symbol) for container, symbol in definition_nodes
            if symbol.kind in {"function", "method"}
            and container.start_byte <= node.start_byte <= node.end_byte <= container.end_byte
        ]
        caller = min(callers, key=lambda item: item[0].end_byte - item[0].start_byte)[1] if callers else None
        line = node.start_point[0] + 1
        edges.append(EdgeFact(
            edge_id=stable_id(
                "EDGE", rel, line, node.start_byte,
                caller.symbol_id if caller else "module", name, "calls",
            ),
            source_symbol_id=caller.symbol_id if caller else None, target_name=name,
            edge_type="calls", line=line, resolver="tree_sitter_tag",
            confidence=0.45, resolution_status="unresolved",
        ))
    return symbols, edges


def _parse_python(rel: str, text: str) -> tuple[list[SymbolFact], list[EdgeFact]]:
    tree = ast.parse(text)
    symbols: list[SymbolFact] = []
    edges: list[EdgeFact] = []
    symbol_for_node: dict[int, SymbolFact] = {}

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.callers: list[Optional[SymbolFact]] = []

        def _symbol(self, node, name: str, kind: str) -> SymbolFact:
            qualified = ".".join([*self.scope, name])
            signature = _python_signature(node)
            fact = SymbolFact(
                symbol_id=stable_id("SYM", "python", rel, qualified, signature),
                name=name,
                qualified_name=qualified,
                kind=kind,
                start_line=int(getattr(node, "lineno", 1)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                signature=signature,
                backend="python_ast",
                precision="syntax",
            )
            symbols.append(fact)
            symbol_for_node[id(node)] = fact
            return fact

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            fact = self._symbol(node, node.name, "class")
            self.scope.append(node.name)
            self.callers.append(fact)
            self.generic_visit(node)
            self.callers.pop()
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node) -> None:
            kind = "method" if self.scope and any(s.kind == "class" for s in symbols if s.qualified_name == ".".join(self.scope)) else "function"
            fact = self._symbol(node, node.name, kind)
            self.scope.append(node.name)
            self.callers.append(fact)
            self.generic_visit(node)
            self.callers.pop()
            self.scope.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = _call_name(node.func)
            if name:
                caller = self.callers[-1] if self.callers else None
                edge_id = stable_id(
                    "EDGE", rel, getattr(node, "lineno", 1), getattr(node, "col_offset", 0),
                    caller.symbol_id if caller else "module", name, "calls",
                )
                edges.append(EdgeFact(
                    edge_id=edge_id,
                    source_symbol_id=caller.symbol_id if caller else None,
                    target_name=name,
                    edge_type="calls",
                    line=int(getattr(node, "lineno", 1)),
                    resolver="python_ast_name",
                    confidence=0.45,
                    resolution_status="unresolved",
                ))
            self.generic_visit(node)

    Collector().visit(tree)
    return symbols, edges


def _python_signature(node) -> str:
    args = getattr(node, "args", None)
    if args is None:
        return ""
    names = [item.arg for item in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return f"({','.join(names)})"


def _call_name(node) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _insert_facts(connection, snapshot_id, file_id, symbols, edges) -> None:
    unique_symbols = {item.symbol_id: item for item in symbols}
    connection.executemany(
        "INSERT OR IGNORE INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(snapshot_id, item.symbol_id, file_id, item.name, item.qualified_name, item.kind,
          item.start_line, item.end_line, item.signature, item.backend, item.precision)
         for item in unique_symbols.values()],
    )
    unique_edges = {item.edge_id: item for item in edges}
    connection.executemany(
        "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(snapshot_id, item.edge_id, item.source_symbol_id, None, item.target_name,
          item.edge_type, file_id, item.line, item.resolver, item.confidence,
          item.resolution_status) for item in unique_edges.values()],
    )


def _reuse_file_facts(connection, old_snapshot, new_snapshot, file_id) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO symbols SELECT ?,symbol_id,file_id,name,qualified_name,kind,start_line,end_line,signature,backend,precision "
        "FROM symbols WHERE snapshot_id=? AND file_id=?",
        (new_snapshot, old_snapshot, file_id),
    )
    connection.execute(
        "INSERT OR IGNORE INTO edges SELECT ?,edge_id,source_symbol_id,target_symbol_id,target_name,edge_type,file_id,line,resolver,confidence,resolution_status "
        "FROM edges WHERE snapshot_id=? AND file_id=?",
        (new_snapshot, old_snapshot, file_id),
    )


def _resolve_edges(connection, snapshot_id) -> None:
    edges = connection.execute(
        "SELECT rowid,* FROM edges WHERE snapshot_id=? AND edge_type='calls'", (snapshot_id,)
    ).fetchall()
    for edge in edges:
        candidates = connection.execute(
            "SELECT symbol_id FROM symbols WHERE snapshot_id=? AND (name=? OR qualified_name=?)",
            (snapshot_id, edge["target_name"], edge["target_name"]),
        ).fetchall()
        if len(candidates) == 1:
            connection.execute(
                "UPDATE edges SET target_symbol_id=?, confidence=?, resolution_status='probable' WHERE rowid=?",
                (candidates[0]["symbol_id"], 0.70, edge["rowid"]),
            )
        elif len(candidates) > 1:
            connection.execute(
                "UPDATE edges SET resolution_status='ambiguous', confidence=? WHERE rowid=?",
                (0.35, edge["rowid"]),
            )


def _source_role(rel: str) -> str:
    parts = {part.lower() for part in Path(rel).parts}
    if parts & {"test", "tests", "testing"}:
        return "test"
    if parts & {"example", "examples", "demo"}:
        return "example"
    if parts & {"generated", "gen"}:
        return "generated"
    return "production"


def _line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
