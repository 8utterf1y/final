"""Project-scoped retrieval index for reusable task experiences."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingProvider, OpenAICompatibleEmbeddingProvider


RRF_K = 60
MAX_CANDIDATES = 20
MAX_MANIFEST_CARDS = 12
MAX_MANIFEST_CHARS = 4_000


@dataclass(frozen=True)
class ExperienceCard:
    id: str
    title: str
    path: str
    description: str
    applies_when: tuple[str, ...]
    not_applies_when: tuple[str, ...]
    signals: tuple[str, ...]
    problem_goal: str
    root_cause: str
    solution_summary: str
    validation_methods: tuple[str, ...]
    file_patterns: tuple[str, ...]
    retrieval_queries: tuple[str, ...]
    tags: tuple[str, ...]
    quality_score: int
    embedding_model: str | None
    updated_at: str


@dataclass(frozen=True)
class ExperienceHit:
    card: ExperienceCard
    score: float
    vector_score: float | None
    rrf_score: float


class ExperienceIndex:
    """Indexes compact experience cards while Markdown remains the full source."""

    def __init__(
        self,
        *,
        root: Path,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.root = root.resolve()
        self.db_path = self.root / "experience-index.db"
        self._embedding_provider = embedding_provider
        self._schema_initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _provider(self) -> EmbeddingProvider:
        if self._embedding_provider is None:
            self._embedding_provider = OpenAICompatibleEmbeddingProvider.from_env()
        return self._embedding_provider

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS experience_cards (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    card_text TEXT NOT NULL,
                    applies_when TEXT NOT NULL,
                    not_applies_when TEXT NOT NULL,
                    signals TEXT NOT NULL,
                    problem_goal TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    solution_summary TEXT NOT NULL,
                    validation_methods TEXT NOT NULL,
                    file_patterns TEXT NOT NULL,
                    retrieval_queries TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    quality_score INTEGER NOT NULL,
                    embedding_model TEXT,
                    embedding_dimensions INTEGER,
                    embedding BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            self._ensure_column(conn, "experience_cards", "description", "TEXT NOT NULL DEFAULT ''")
            try:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS experience_cards_fts USING fts5(
                        experience_id UNINDEXED,
                        title,
                        card_text,
                        tokenize='trigram'
                    )"""
                )
            except sqlite3.OperationalError:
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS experience_cards_fts USING fts5(
                        experience_id UNINDEXED,
                        title,
                        card_text
                    )"""
                )

    def _ensure_schema(self) -> None:
        if not self._schema_initialized:
            self._init_schema()
            self._schema_initialized = True

    async def upsert(self, experience_id: str, path: Path, payload: dict[str, Any]) -> ExperienceCard:
        self._ensure_schema()
        path = path.resolve()
        if path.parent != self.root or path.suffix.lower() != ".md":
            raise ValueError("Experience path must be a Markdown file in the project experience directory.")
        fields = _card_fields(payload)
        card_text = _card_text(fields)
        vector: list[float] | None = None
        model: str | None = None
        try:
            provider = self._provider()
            vector = await provider.embed_query(card_text)
            model = provider.model
        except Exception:
            # A lexical card remains useful when an embedding service is temporarily unavailable.
            vector = None
            model = None
        now = _now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM experience_cards WHERE id = ?", (experience_id,)
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            conn.execute(
                """INSERT OR REPLACE INTO experience_cards (
                    id, title, path, card_text, applies_when, not_applies_when, signals,
                    description, problem_goal, root_cause, solution_summary, validation_methods,
                    file_patterns, retrieval_queries, tags, quality_score,
                    embedding_model, embedding_dimensions, embedding, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    experience_id,
                    fields["title"],
                    str(path),
                    card_text,
                    _json(fields["applies_when"]),
                    _json(fields["not_applies_when"]),
                    _json(fields["signals"]),
                    fields["description"],
                    fields["problem_goal"],
                    fields["root_cause"],
                    fields["solution_summary"],
                    _json(fields["validation_methods"]),
                    _json(fields["file_patterns"]),
                    _json(fields["retrieval_queries"]),
                    _json(fields["tags"]),
                    fields["quality_score"],
                    model,
                    len(vector) if vector else None,
                    _pack_vector(vector) if vector else None,
                    created_at,
                    now,
                ),
            )
            conn.execute("DELETE FROM experience_cards_fts WHERE experience_id = ?", (experience_id,))
            conn.execute(
                "INSERT INTO experience_cards_fts (experience_id, title, card_text) VALUES (?, ?, ?)",
                (experience_id, fields["title"], card_text),
            )
        card = self.get(experience_id)
        if card is None:
            raise RuntimeError("Experience card disappeared after indexing.")
        return card

    def get(self, experience_id: str) -> ExperienceCard | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM experience_cards WHERE id = ?", (experience_id,)).fetchone()
        return _row_to_card(row) if row else None

    def list_cards(self) -> list[ExperienceCard]:
        self._ensure_schema()
        with self._connect() as conn:
                rows = conn.execute("SELECT * FROM experience_cards ORDER BY updated_at DESC").fetchall()
        return [_row_to_card(row) for row in rows]

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def remove(self, experience_id: str) -> bool:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM experience_cards WHERE id = ?", (experience_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM experience_cards_fts WHERE experience_id = ?", (experience_id,))
            conn.execute("DELETE FROM experience_cards WHERE id = ?", (experience_id,))
        return True

    async def search(self, query: str, *, top_k: int = 3) -> list[ExperienceHit]:
        self._ensure_schema()
        query = query.strip()
        if not query:
            return []
        top_k = max(1, min(top_k, 5))
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM experience_cards").fetchall()
        rows = [row for row in rows if Path(str(row["path"])).is_file()]
        if not rows:
            return []

        vector_ranked: list[tuple[str, float]] = []
        try:
            provider = self._provider()
            query_vector = await provider.embed_query(query)
            for row in rows:
                blob = row["embedding"]
                if blob is None or row["embedding_model"] != provider.model:
                    continue
                score = _cosine_similarity(query_vector, _unpack_vector(blob))
                if score >= 0.18:
                    vector_ranked.append((str(row["id"]), score))
            vector_ranked.sort(key=lambda item: item[1], reverse=True)
            vector_ranked = vector_ranked[:MAX_CANDIDATES]
        except Exception:
            vector_ranked = []

        lexical_ids = self._lexical_search(query)
        combined: dict[str, float] = {}
        vector_scores = dict(vector_ranked)
        for rank, (experience_id, _) in enumerate(vector_ranked, start=1):
            combined[experience_id] = combined.get(experience_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, experience_id in enumerate(lexical_ids, start=1):
            combined[experience_id] = combined.get(experience_id, 0.0) + 1.0 / (RRF_K + rank)
        if not combined:
            return []

        by_id = {str(row["id"]): row for row in rows}
        terms = _search_terms(query)
        max_rrf = max(combined.values())
        ranked: list[tuple[str, float]] = []
        for experience_id, rrf_score in combined.items():
            row = by_id[experience_id]
            card_terms = _search_terms(str(row["card_text"]))
            coverage = len(terms.intersection(card_terms)) / max(1, len(terms))
            vector_score = max(0.0, vector_scores.get(experience_id, 0.0))
            exact = 1.0 if query.lower() in str(row["card_text"]).lower() else 0.0
            score = 0.50 * vector_score + 0.30 * (rrf_score / max_rrf) + 0.15 * coverage + 0.05 * exact
            ranked.append((experience_id, score))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return [
            ExperienceHit(
                card=_row_to_card(by_id[experience_id]),
                score=score,
                vector_score=vector_scores.get(experience_id),
                rrf_score=combined[experience_id],
            )
            for experience_id, score in ranked[:top_k]
        ]

    def _lexical_search(self, query: str) -> list[str]:
        expression = _fts_expression(query)
        if not expression:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT experience_id FROM experience_cards_fts
                       WHERE experience_cards_fts MATCH ? ORDER BY bm25(experience_cards_fts)
                       LIMIT ?""",
                    (expression, MAX_CANDIDATES),
                ).fetchall()
            return [str(row["experience_id"]) for row in rows]
        except sqlite3.OperationalError:
            return []

    def build_manifest(self) -> str:
        cards = [card for card in self.list_cards() if Path(card.path).is_file()]
        if not cards:
            return ""
        lines = [
            "\n\n# Experience Index",
            "Reusable task experiences are available. Use experience_search for similar symptoms or workflows, then experience_show with a returned ID before applying one.",
        ]
        for card in cards[:MAX_MANIFEST_CARDS]:
            hint = card.applies_when[0] if card.applies_when else card.problem_goal
            lines.append(f"- {card.id}: {card.title}" + (f" — {hint[:180]}" if hint else ""))
            if len("\n".join(lines)) > MAX_MANIFEST_CHARS:
                lines[-1] = "[... experience index truncated ...]"
                break
        if len(cards) > MAX_MANIFEST_CARDS:
            lines.append(f"[... {len(cards) - MAX_MANIFEST_CARDS} more experiences ...]")
        return "\n".join(lines)[:MAX_MANIFEST_CHARS]


def format_experience_hits(query: str, hits: list[ExperienceHit]) -> str:
    lines = [
        '<experience-results untrusted="true">',
        f"Query: {_escape_tag(query)}",
        "These are candidate experience cards, not current-task facts. Check applicability, then call experience_show with an ID to read the complete workflow.",
    ]
    if not hits:
        lines.append("No relevant experience found.")
    for index, hit in enumerate(hits, start=1):
        card = hit.card
        lines.extend([
            f"[{index}] id={card.id} score={hit.score:.4f}",
            f"Title: {_escape_tag(card.title)}",
            f"Path: {_escape_tag(card.path)}",
            f"Description: {_escape_tag(card.description or '(unspecified)')}",
            f"Applies when: {_escape_tag('; '.join(card.applies_when) or '(unspecified)')}",
            f"Signals: {_escape_tag('; '.join(card.signals) or '(unspecified)')}",
            f"Solution summary: {_escape_tag(card.solution_summary or '(read full experience)')}",
            f"Validation: {_escape_tag('; '.join(card.validation_methods) or '(unspecified)')}",
        ])
    lines.append("</experience-results>")
    return "\n".join(lines)


def format_experience_body(card: ExperienceCard, content: str) -> str:
    safe = content.replace("</experience>", "&lt;/experience&gt;")
    return (
        f'<experience id="{_escape_tag(card.id)}" untrusted="true">\n'
        "This is historical process guidance. Verify its applicability against the current code and validate all changes.\n"
        f"Source: {_escape_tag(card.path)}\n\n{safe}\n</experience>"
    )


def _card_fields(payload: dict[str, Any]) -> dict[str, Any]:
    scenario = payload.get("scenario") or {}
    problem = payload.get("problem") or {}
    diagnosis = payload.get("diagnosis") or {}
    procedure = payload.get("procedure") or []
    validation = payload.get("validation") or []
    fields = {
        "title": str(payload.get("title") or "Reusable coding experience"),
        "description": str(payload.get("description") or "").strip(),
        "applies_when": _strings(scenario.get("applies_when")),
        "not_applies_when": _strings(scenario.get("not_applies_when")),
        "signals": _strings(scenario.get("signals")) + _strings(problem.get("symptoms")),
        "problem_goal": str(problem.get("goal") or ""),
        "root_cause": str(diagnosis.get("root_cause") or ""),
        "solution_summary": " ".join(
            str(item.get("action") or "") for item in procedure[:4] if isinstance(item, dict)
        ).strip(),
        "validation_methods": [
            str(item.get("method") or "") for item in validation[:6]
            if isinstance(item, dict) and item.get("method")
        ],
        "file_patterns": _strings(payload.get("related_file_patterns")),
        "retrieval_queries": _strings(payload.get("retrieval_queries")),
        "tags": _strings(payload.get("tags")),
        "quality_score": int(payload.get("quality_score") or 0),
    }
    if not fields["description"]:
        fields["description"] = _description_from_fields(fields)
    return fields


def _card_text(fields: dict[str, Any]) -> str:
    return str(fields["description"]).strip()


def _description_from_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    if fields["applies_when"]:
        parts.append("Applies when " + "; ".join(fields["applies_when"][:2]))
    elif fields["problem_goal"]:
        parts.append("Goal: " + fields["problem_goal"])
    if fields["signals"]:
        parts.append("Signals: " + "; ".join(fields["signals"][:4]))
    if fields["file_patterns"]:
        parts.append("Files: " + "; ".join(fields["file_patterns"][:4]))
    if fields["validation_methods"]:
        parts.append("Validate with " + "; ".join(fields["validation_methods"][:3]))
    if fields["retrieval_queries"]:
        parts.append("Queries: " + "; ".join(fields["retrieval_queries"][:3]))
    return " ".join(parts).strip() or str(fields["title"]).strip() or "Reusable coding experience."


def _row_to_card(row: sqlite3.Row) -> ExperienceCard:
    return ExperienceCard(
        id=str(row["id"]),
        title=str(row["title"]),
        path=str(row["path"]),
        description=str(row["description"] or row["card_text"]),
        applies_when=_json_tuple(row["applies_when"]),
        not_applies_when=_json_tuple(row["not_applies_when"]),
        signals=_json_tuple(row["signals"]),
        problem_goal=str(row["problem_goal"]),
        root_cause=str(row["root_cause"]),
        solution_summary=str(row["solution_summary"]),
        validation_methods=_json_tuple(row["validation_methods"]),
        file_patterns=_json_tuple(row["file_patterns"]),
        retrieval_queries=_json_tuple(row["retrieval_queries"]),
        tags=_json_tuple(row["tags"]),
        quality_score=int(row["quality_score"]),
        embedding_model=str(row["embedding_model"]) if row["embedding_model"] else None,
        updated_at=str(row["updated_at"]),
    )


def _fts_expression(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_./:-]{2,}|[\u3400-\u9fff]{2,}", query.lower())
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:12])


def _search_terms(text: str) -> set[str]:
    latin = re.findall(r"[a-z0-9_./:-]{2,}", text.lower())
    cjk = re.findall(r"[\u3400-\u9fff]{2,}", text)
    return set(latin + cjk)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_tuple(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return -1.0
    return dot / (left_norm * right_norm)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _escape_tag(value: str) -> str:
    return value.replace("<", "&lt;").replace(">", "&gt;")
