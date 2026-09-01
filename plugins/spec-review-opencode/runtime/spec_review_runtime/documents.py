from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .util import now, resolve_inside, sha256_bytes, stable_id


TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+] |\d+[.)]\s+)(.+)$")
METADATA_SECTION_RE = re.compile(
    r"(?:文档信息|基本信息|修订记录|版本历史|变更历史|目录|document\s+information|revision\s+history)",
    re.IGNORECASE,
)
METADATA_FIELD_RE = re.compile(
    r"^(?:文档编号|文档版本|版本|状态|负责人|作者|适用系统|业务域|最后更新|更新日期|创建日期)\s*[:：]",
    re.IGNORECASE,
)


def load_claim_candidates(
    connection: sqlite3.Connection,
    repo: Path,
    case_id: str,
    doc_values: list[str],
    selected_sections: list[str],
) -> list[dict]:
    documents = _expand_documents(repo, doc_values)
    if not documents:
        raise ValueError("没有找到受支持的需求文档")
    candidates: list[dict] = []
    ordinal = 0
    for path in documents:
        data = path.read_bytes()
        document_id = stable_id("DOC", str(path), sha256_bytes(data))
        connection.execute(
            "INSERT OR REPLACE INTO documents VALUES(?,?,?,?)",
            (document_id, str(path), sha256_bytes(data), now()),
        )
        text = data.decode("utf-8", errors="replace")
        for section, source_text in _blocks(text):
            if selected_sections and not _section_selected(section, selected_sections):
                continue
            statement = _candidate_statement(source_text)
            if not statement:
                continue
            ordinal += 1
            claim_id = stable_id("CLAIM", case_id, document_id, section, source_text)
            verifiability = _verifiability(section, source_text)
            row = {
                "claim_id": claim_id,
                "document": str(path),
                "section": section,
                "source_text": source_text,
                "statement": statement,
                "verifiability": verifiability,
                "ordinal": ordinal,
            }
            connection.execute(
                "INSERT INTO claims VALUES(?,?,?,?,?,?,?,?)",
                (claim_id, case_id, document_id, section, source_text, statement, verifiability, ordinal),
            )
            candidates.append(row)
    if not candidates:
        raise ValueError("选定范围内的需求文档不包含可审查的章节块")
    return candidates


def _expand_documents(repo: Path, values: list[str]) -> list[Path]:
    result: list[Path] = []
    for value in values:
        path = resolve_inside(repo, value)
        if path.is_dir():
            result.extend(
                item for item in sorted(path.rglob("*"))
                if item.is_file() and item.suffix.lower() in TEXT_SUFFIXES
            )
        elif path.suffix.lower() in TEXT_SUFFIXES:
            result.append(path)
        else:
            raise ValueError(f"不支持的需求文档：{path}")
    return list(dict.fromkeys(result))


def _blocks(text: str):
    section = "概述"
    paragraph: list[str] = []

    def flush():
        nonlocal paragraph
        if paragraph:
            value = " ".join(item.strip() for item in paragraph).strip()
            paragraph = []
            if value:
                return section, value
        return None

    for raw in text.splitlines():
        heading = HEADING_RE.match(raw.strip())
        if heading:
            item = flush()
            if item:
                yield item
            section = heading.group(2).strip()
            continue
        listed = LIST_RE.match(raw)
        if listed:
            item = flush()
            if item:
                yield item
            value = listed.group(1).strip()
            if value:
                yield section, value
            continue
        if not raw.strip():
            item = flush()
            if item:
                yield item
            continue
        paragraph.append(raw)
    item = flush()
    if item:
        yield item


def _candidate_statement(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) < 12:
        return ""
    # 此处只提取候选声明，不使用规范性关键词做硬分类。L3/L4 会取得完整章节块，
    # 再判断它是否能够通过代码证据验证。
    return cleaned[:1200]


def _verifiability(section: str, text: str) -> str:
    """Pre-classify obvious document metadata without discarding the source block."""
    if METADATA_SECTION_RE.search(section) or METADATA_FIELD_RE.search(text.strip()):
        return "metadata"
    return "candidate"


def _section_selected(section: str, selected: list[str]) -> bool:
    normalized = section.casefold()
    return any(value.casefold() in normalized or normalized in value.casefold() for value in selected)
