"""Document loaders for the local knowledge base."""

from __future__ import annotations

import csv
import html
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path


TEXT_SUFFIXES = {".md", ".markdown", ".mdown", ".txt", ".text", ".csv", ".json", ".jsonl", ".html", ".htm", ".xml"}
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown"}
MARKITDOWN_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".rtf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | MARKITDOWN_SUFFIXES
MAX_PARSED_CHARS = 5 * 1024 * 1024


@dataclass(frozen=True)
class ParsedKnowledgeDocument:
    text: str
    title: str | None
    mime_type: str
    parser: str
    markdown: bool
    metadata: dict[str, object] = field(default_factory=dict)


class DocumentParseError(RuntimeError):
    """Raised when a knowledge document cannot be parsed into text."""


def parse_knowledge_file(path: Path) -> ParsedKnowledgeDocument:
    source = path.expanduser().resolve()
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported knowledge file. Supported formats: {supported}")

    if source.stat().st_size == 0:
        raise DocumentParseError("No text content found in knowledge source.")

    if suffix in TEXT_SUFFIXES:
        return _parse_text_like(source, suffix)
    return _parse_with_markitdown(source)


def _parse_text_like(path: Path, suffix: str) -> ParsedKnowledgeDocument:
    raw = path.read_text(encoding="utf-8-sig")
    mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    parser = "builtin-text"
    markdown = suffix in MARKDOWN_SUFFIXES
    frontmatter_metadata = _extract_frontmatter_metadata(raw) if markdown else {}
    text = _strip_frontmatter(raw) if markdown else raw

    if suffix in {".html", ".htm"}:
        text = _html_to_markdown(raw)
        parser = "builtin-html"
        markdown = True
    elif suffix == ".csv":
        text = _csv_to_text(raw)
        parser = "builtin-csv"
        markdown = False
    elif suffix in {".json", ".jsonl"}:
        text = _json_to_text(raw, jsonl=suffix == ".jsonl")
        parser = "builtin-json"
        markdown = False
    elif suffix == ".xml":
        text = _html_to_text(raw)
        parser = "builtin-xml"
        markdown = False

    cleaned = clean_extracted_text(text)
    if len(cleaned) > MAX_PARSED_CHARS:
        cleaned = cleaned[:MAX_PARSED_CHARS]
    metadata = {"source_suffix": suffix}
    metadata.update(frontmatter_metadata)
    title = _infer_title(path, cleaned, markdown=markdown)
    if suffix in {".csv", ".json", ".jsonl", ".xml"}:
        title = path.stem.replace("-", " ").replace("_", " ")
    return ParsedKnowledgeDocument(
        text=cleaned,
        title=title,
        mime_type=mime_type,
        parser=parser,
        markdown=markdown,
        metadata=metadata,
    )


def _parse_with_markitdown(path: Path) -> ParsedKnowledgeDocument:
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError as exc:
        raise DocumentParseError(
            f"{path.suffix.lower()} parsing requires the optional RAG parser dependency. "
            "Install it with: pip install -e '.[rag]'"
        ) from exc

    try:
        converted = MarkItDown().convert(str(path))
    except Exception as exc:  # pragma: no cover - depends on optional parser internals
        raise DocumentParseError(f"Failed to parse {path.name}: {exc}") from exc

    text = getattr(converted, "text_content", None) or getattr(converted, "markdown", None) or str(converted)
    cleaned = clean_extracted_text(text)
    if len(cleaned) > MAX_PARSED_CHARS:
        cleaned = cleaned[:MAX_PARSED_CHARS]
    return ParsedKnowledgeDocument(
        text=cleaned,
        title=_infer_title(path, cleaned, markdown=True),
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        parser="markitdown",
        markdown=True,
        metadata={"source_suffix": path.suffix.lower()},
    )


def clean_extracted_text(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)
    t = re.sub(r"(?m)^image\d+\.(png|jpe?g|gif|bmp|webp)\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"https?://\S+?\.(png|jpe?g|gif|bmp|webp)(\?\S*)?", "", t, flags=re.IGNORECASE)
    t = re.sub(r"file:(//)?\S+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?m)^\s*[-_*=]{3,}\s*$", "", t)
    t = re.sub(r"(?m)[ \t]+$", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _html_to_markdown(raw: str) -> str:
    # Head metadata (especially <title>) is not document body evidence and
    # otherwise becomes a tiny heading-less chunk before the first <h1>.
    text = re.sub(r"(?is)<head\b[^>]*>.*?</head>", " ", raw)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    for level in range(1, 7):
        text = re.sub(
            rf"(?is)<h{level}[^>]*>(.*?)</h{level}>",
            lambda match, level=level: f"\n{'#' * level} {_strip_tags(match.group(1)).strip()}\n",
            text,
        )
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|section|article|li|tr|table)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _html_to_text(raw: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", _html_to_markdown(raw), flags=re.MULTILINE)


def _strip_tags(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def _csv_to_text(raw: str) -> str:
    rows = []
    for row in csv.reader(raw.splitlines()):
        if row:
            rows.append(" | ".join(cell.strip() for cell in row if cell.strip()))
    return "\n".join(rows)


def _json_to_text(raw: str, *, jsonl: bool) -> str:
    try:
        if jsonl:
            values = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            values = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(values, ensure_ascii=False, indent=2)


def _infer_title(path: Path, text: str, *, markdown: bool) -> str | None:
    if markdown:
        for line in text.splitlines():
            match = re.match(r"^#\s+(.+?)\s*$", line)
            if match:
                return match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return path.stem


def _extract_frontmatter_metadata(raw: str) -> dict[str, object]:
    """Extract small routing hints without depending on a YAML package."""
    if not raw.startswith("---\n"):
        return {}
    end = raw.find("\n---", 4)
    if end < 0:
        return {}
    result: dict[str, object] = {}
    for line in raw[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip().strip('"\'')
        if key in {"title", "description"} and value:
            result[key] = value
        elif key in {"tags", "keywords"} and value:
            result["tags"] = [
                item.strip().strip('"\'')
                for item in value.strip("[]").split(",")
                if item.strip()
            ]
    return result


def _strip_frontmatter(raw: str) -> str:
    if not raw.startswith("---\n"):
        return raw
    end = raw.find("\n---", 4)
    if end < 0:
        return raw
    return raw[end + 4:].lstrip("\n")
