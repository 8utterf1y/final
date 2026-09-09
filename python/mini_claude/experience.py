"""Experience persistence for project-scoped engineering workflows.

The journal is intentionally separate from chat history: context compression may
rewrite messages, but reusable experience needs an immutable event trail.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Any

from .experience_index import (
    ExperienceCard,
    ExperienceIndex,
    format_experience_body,
    format_experience_hits,
)


MAX_TEXT_CHARS = 1200
MAX_TOOL_RESULT_CHARS = 1000
MAX_RANGE_EVENTS = 80
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([^\s'\"`]+)"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+"),
]

Extractor = Callable[[str, str, int], Awaitable[str]]


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    turn_id: int
    event_type: str
    timestamp: str
    summary: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    status: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ExperienceSaveResult:
    status: str
    title: str
    card: ExperienceCard | None
    path: Path | None
    action: str
    quality_score: int
    scope: tuple[int, int] | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperienceEntry:
    id: str
    path: Path
    title: str
    card_id: str | None = None


@dataclass(frozen=True)
class ExperienceDeleteResult:
    deleted: bool
    entry: ExperienceEntry | None
    index_removed: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class TaskJournal:
    session_id: str
    events: list[TaskEvent] = field(default_factory=list)
    current_turn_id: int = 0
    checkpoint_event_id: str | None = None

    def start_turn(self, user_message: str) -> None:
        self.current_turn_id += 1
        self.record("user_message", _summarize_text(user_message, MAX_TEXT_CHARS))

    def record(
        self,
        event_type: str,
        summary: str,
        *,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=uuid.uuid4().hex[:12],
            turn_id=self.current_turn_id,
            event_type=event_type,
            timestamp=_now(),
            summary=_redact(_summarize_text(summary, MAX_TEXT_CHARS)),
            tool_name=tool_name,
            tool_input=_redact_json(tool_input) if tool_input else None,
            status=status,
            duration_ms=duration_ms,
        )
        self.events.append(event)
        return event

    def clear(self) -> None:
        self.events.clear()
        self.current_turn_id = 0
        self.checkpoint_event_id = None

    def candidate_events(self, from_turn: int | None = None) -> list[TaskEvent]:
        events = self.events
        if self.checkpoint_event_id and from_turn is None:
            idx = next((i for i, e in enumerate(events) if e.event_id == self.checkpoint_event_id), -1)
            if idx >= 0:
                events = events[idx + 1:]
        if from_turn is not None:
            events = [event for event in events if event.turn_id >= from_turn]
        return events[-MAX_RANGE_EVENTS:]

    def checkpoint(self, event_id: str | None = None) -> None:
        if self.events:
            self.checkpoint_event_id = event_id or self.events[-1].event_id


class ExperienceManager:
    def __init__(
        self,
        journal: TaskJournal,
        *,
        project_root: Path | None = None,
        experience_index: ExperienceIndex | None = None,
    ) -> None:
        self.journal = journal
        self.project_root = (project_root or Path.cwd()).resolve()
        default_root = Path.home() / ".mini-claude" / "projects" / _project_hash(self.project_root) / "experiences"
        self.root = experience_index.root if experience_index else default_root
        self.index = experience_index or ExperienceIndex(root=self.root)

    async def save(
        self,
        *,
        extractor: Extractor | None,
        from_turn: int | None = None,
    ) -> ExperienceSaveResult:
        events = self.journal.candidate_events(from_turn=from_turn)
        if not _has_substantive_activity(events):
            return ExperienceSaveResult(
                status="skipped",
                title="No reusable experience detected",
                card=None,
                path=None,
                action="skip",
                quality_score=0,
                scope=None,
                warnings=("No code edit, shell verification, or tool failure was found in the selected scope.",),
            )

        payload: dict[str, Any] | None = None
        warnings: list[str] = []
        if extractor:
            try:
                raw = await extractor(EXPERIENCE_EXTRACTOR_SYSTEM, _build_extractor_user_message(events), 2400)
                payload = _parse_json_object(raw)
            except Exception as exc:
                warnings.append(f"Extractor failed: {exc}")
        if payload is None:
            payload = _fallback_payload(events)
            warnings.append("Saved from deterministic fallback because model extraction was unavailable.")

        normalized, validation_warnings = validate_experience_payload(payload, events)
        warnings.extend(validation_warnings)
        if normalized["persistence_action"] == "skip":
            return ExperienceSaveResult(
                status="skipped",
                title=normalized["title"],
                card=None,
                path=None,
                action="skip",
                quality_score=normalized["quality_score"],
                scope=(normalized["start_turn_id"], normalized["end_turn_id"]),
                warnings=tuple(warnings),
            )

        related_versions = self._related_versions(normalized["title"])
        if related_versions:
            normalized["persistence_action"] = "merge"
            normalized["related_versions"] = [path.name for path in related_versions[:5]]
        markdown = render_experience_markdown(normalized)
        path = self._write_markdown(normalized["title"], markdown)
        try:
            card = await self.index.upsert(_experience_id(path), path, normalized)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self.journal.checkpoint()
        return ExperienceSaveResult(
            status="saved",
            title=normalized["title"],
            card=card,
            path=path,
            action=normalized["persistence_action"],
            quality_score=normalized["quality_score"],
            scope=(normalized["start_turn_id"], normalized["end_turn_id"]),
            warnings=tuple(warnings),
        )

    def _write_markdown(self, title: str, markdown: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        filename = _slugify(title)[:80] or "experience"
        final_path = self.root / f"{time.time_ns()}-{filename}.md"
        fd, tmp_name = tempfile.mkstemp(prefix=".experience-", suffix=".md", dir=str(self.root))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        os.replace(tmp_name, final_path)
        return final_path

    def _related_versions(self, title: str) -> list[Path]:
        if not self.root.exists():
            return []
        filename = _slugify(title)[:80] or "experience"
        return sorted(self.root.glob(f"*-{filename}.md"), reverse=True)

    def list_entries(self) -> list[ExperienceEntry]:
        if not self.root.exists():
            return []
        cards = {str(Path(card.path).resolve()): card.id for card in self.index.list_cards()}
        return [
            ExperienceEntry(
                id=_experience_id(path),
                path=path,
                title=_read_markdown_title(path),
                card_id=cards.get(str(path.resolve())),
            )
            for path in sorted(self.root.glob("*.md"), reverse=True)
        ]

    def get_entry(self, raw_id: str) -> ExperienceEntry:
        matches = self._match_entries(raw_id)
        if not matches:
            raise ValueError(f"Unknown experience: {raw_id}")
        if len(matches) > 1:
            names = ", ".join(entry.id for entry in matches[:5])
            raise ValueError(f"Ambiguous experience id '{raw_id}'. Matches: {names}")
        return matches[0]

    def read_entry(self, raw_id: str) -> tuple[ExperienceEntry, str]:
        entry = self.get_entry(raw_id)
        return entry, entry.path.read_text(encoding="utf-8")

    def delete_entry(self, raw_id: str) -> ExperienceDeleteResult:
        entry = self.get_entry(raw_id)
        warnings: list[str] = []
        index_removed = self.index.remove(entry.card_id or entry.id)
        if entry.card_id and not index_removed:
            warnings.append(f"Experience card not found: {entry.card_id}")
        try:
            entry.path.unlink()
        except FileNotFoundError:
            warnings.append("Experience file was already missing.")
        return ExperienceDeleteResult(
            deleted=True,
            entry=entry,
            index_removed=index_removed,
            warnings=tuple(warnings),
        )

    def _match_entries(self, raw_id: str) -> list[ExperienceEntry]:
        query = raw_id.strip()
        if not query:
            raise ValueError("Experience id is required.")
        entries = self.list_entries()
        exact = [
            entry for entry in entries
            if query in {entry.id, entry.path.name, entry.path.stem}
            or (entry.card_id and query == entry.card_id)
        ]
        if exact:
            return exact
        return [
            entry for entry in entries
            if entry.id.startswith(query)
            or entry.path.name.startswith(query)
            or query.lower() in entry.path.stem.lower()
        ]


EXPERIENCE_EXTRACTOR_SYSTEM = """You extract reusable engineering experience from a structured coding-agent task journal.
Return one JSON object only. Do not invent evidence.

Required JSON shape:
{
  "title": "short reusable title",
  "description": "one concise retrieval description covering when to use it, key symptoms, file patterns, and validation commands",
  "persistence_action": "create|merge|skip",
  "start_turn_id": 1,
  "end_turn_id": 2,
  "scenario": {
    "applies_when": ["..."],
    "not_applies_when": ["..."],
    "signals": ["..."]
  },
  "problem": {
    "goal": "...",
    "symptoms": ["..."],
    "constraints": ["..."]
  },
  "diagnosis": {
    "root_cause": "...",
    "evidence_event_ids": ["event id"]
  },
  "procedure": [
    {"action": "...", "reason": "...", "checkpoint": "..."}
  ],
  "pitfalls": [
    {"symptom": "...", "cause": "...", "correction": "..."}
  ],
  "validation": [
    {"method": "...", "expected_result": "...", "evidence_event_ids": ["event id"]}
  ],
  "related_file_patterns": ["..."],
  "retrieval_queries": ["..."],
  "tags": ["experience", "coding-agent", "..."]
}

Use skip when there is no reusable workflow, no evidence, or the task is still unresolved."""


def _build_extractor_user_message(events: list[TaskEvent]) -> str:
    return json.dumps(
        {
            "instructions": [
                "Select a contiguous turn range from this event list.",
                "Prefer reusable enterprise coding workflows, debugging procedures, validation patterns, or project conventions.",
                "Keep the answer concise and evidence-bound.",
            ],
            "events": [_event_to_dict(event) for event in events],
        },
        ensure_ascii=False,
        indent=2,
    )


def validate_experience_payload(payload: dict[str, Any], events: list[TaskEvent]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    event_by_id = {event.event_id: event for event in events}
    turns = sorted({event.turn_id for event in events})
    min_turn, max_turn = (turns[0], turns[-1]) if turns else (0, 0)

    start_turn = _coerce_int(payload.get("start_turn_id"), min_turn)
    end_turn = _coerce_int(payload.get("end_turn_id"), max_turn)
    if start_turn < min_turn or end_turn > max_turn or start_turn > end_turn:
        warnings.append("Invalid model-selected scope; reset to candidate event range.")
        start_turn, end_turn = min_turn, max_turn

    scoped = [event for event in events if start_turn <= event.turn_id <= end_turn]
    payload = dict(payload)
    payload["title"] = _clean_scalar(payload.get("title")) or "Reusable coding experience"
    payload["persistence_action"] = _normalize_action(payload.get("persistence_action"))
    payload["start_turn_id"] = start_turn
    payload["end_turn_id"] = end_turn
    payload["scenario"] = _normalize_mapping(payload.get("scenario"), {
        "applies_when": [],
        "not_applies_when": [],
        "signals": [],
    })
    payload["problem"] = _normalize_mapping(payload.get("problem"), {
        "goal": "",
        "symptoms": [],
        "constraints": [],
    })
    payload["diagnosis"] = _normalize_mapping(payload.get("diagnosis"), {
        "root_cause": "",
        "evidence_event_ids": [],
    })
    payload["procedure"] = _normalize_steps(payload.get("procedure"))
    payload["pitfalls"] = _normalize_pitfalls(payload.get("pitfalls"))
    payload["validation"] = _normalize_validation(payload.get("validation"))
    payload["related_file_patterns"] = _clean_list(payload.get("related_file_patterns"), limit=12)
    payload["retrieval_queries"] = _clean_list(payload.get("retrieval_queries"), limit=8)
    payload["tags"] = _normalize_tags(payload.get("tags"))
    payload["description"] = _experience_description(payload)

    referenced = _referenced_event_ids(payload)
    missing = sorted(event_id for event_id in referenced if event_id not in event_by_id)
    if missing:
        warnings.append("Removed evidence ids not present in the task journal.")
        _remove_missing_event_ids(payload, set(missing))

    if not _has_validation_evidence(payload, event_by_id):
        warnings.append("No successful verification evidence was found; saved as unverified.")
        payload["tags"] = _append_unique(payload["tags"], "unverified")

    if not _has_substantive_activity(scoped):
        warnings.append("Selected scope has little reusable activity; marked skip.")
        payload["persistence_action"] = "skip"

    quality = score_experience(scoped, payload)
    payload["quality_score"] = quality
    if quality < 2:
        payload["persistence_action"] = "skip"
    return payload, warnings


def score_experience(events: list[TaskEvent], payload: dict[str, Any]) -> int:
    score = 0
    if any(event.tool_name in {"write_file", "edit_file"} and event.status == "success" for event in events):
        score += 2
    if any(event.tool_name == "run_shell" and event.status == "success" for event in events):
        score += 2
    if any(event.status == "failure" for event in events):
        score += 1
    if payload.get("procedure"):
        score += 1
    if payload.get("validation"):
        score += 1
    if "unverified" in payload.get("tags", []):
        score -= 2
    return max(0, score)


def render_experience_markdown(payload: dict[str, Any]) -> str:
    tags = _normalize_tags(payload.get("tags"))
    title = _clean_scalar(payload.get("title")) or "Reusable coding experience"
    description = _experience_description(payload)
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "tags:",
        *[f"  - {tag}" for tag in tags],
        f"description: {json.dumps(description, ensure_ascii=False)}",
        "---",
        "",
        f"# {title}",
        "",
        "## Description",
        "",
        description,
        "",
        "## Content",
        "",
        "## Scenario",
        _bullets(payload["scenario"].get("applies_when"), "Applies when"),
        _bullets(payload["scenario"].get("not_applies_when"), "Does not apply when"),
        _bullets(payload["scenario"].get("signals"), "Signals"),
        "",
        "## Problem",
        f"- Goal: {_clean_scalar(payload['problem'].get('goal'))}",
        _bullets(payload["problem"].get("symptoms"), "Symptoms"),
        _bullets(payload["problem"].get("constraints"), "Constraints"),
        "",
        "## Diagnosis",
        f"- Root cause: {_clean_scalar(payload['diagnosis'].get('root_cause'))}",
        _bullets(payload["diagnosis"].get("evidence_event_ids"), "Evidence events"),
        "",
        "## Procedure",
    ]
    for index, step in enumerate(payload.get("procedure") or [], start=1):
        lines.append(f"{index}. {_clean_scalar(step.get('action'))}")
        lines.append(f"   - Reason: {_clean_scalar(step.get('reason'))}")
        lines.append(f"   - Checkpoint: {_clean_scalar(step.get('checkpoint'))}")
    lines.extend(["", "## Pitfalls"])
    for pitfall in payload.get("pitfalls") or []:
        lines.append(f"- Symptom: {_clean_scalar(pitfall.get('symptom'))}")
        lines.append(f"  Cause: {_clean_scalar(pitfall.get('cause'))}")
        lines.append(f"  Correction: {_clean_scalar(pitfall.get('correction'))}")
    lines.extend(["", "## Validation"])
    for item in payload.get("validation") or []:
        lines.append(f"- Method: {_clean_scalar(item.get('method'))}")
        lines.append(f"  Expected: {_clean_scalar(item.get('expected_result'))}")
        lines.append(f"  Evidence events: {', '.join(_clean_list(item.get('evidence_event_ids'), limit=8))}")
    lines.extend([
        "",
        "## Retrieval",
        _bullets(payload.get("related_file_patterns"), "File patterns"),
        _bullets(payload.get("retrieval_queries"), "Queries"),
        "",
        "## Metadata",
        f"- Scope: turns {payload['start_turn_id']} to {payload['end_turn_id']}",
        f"- Quality score: {payload['quality_score']}",
        f"- Persistence action: {payload['persistence_action']}",
    ])
    related_versions = _clean_list(payload.get("related_versions"), limit=5)
    if related_versions:
        lines.append(f"- Related versions: {', '.join(related_versions)}")
    lines.append("")
    return "\n".join(line for line in lines if line is not None)


def _fallback_payload(events: list[TaskEvent]) -> dict[str, Any]:
    turns = sorted({event.turn_id for event in events})
    tool_events = [event for event in events if event.tool_name]
    files = sorted({
        str(event.tool_input.get("file_path"))
        for event in tool_events
        if event.tool_input and event.tool_input.get("file_path")
    })[:8]
    commands = [event.summary for event in tool_events if event.tool_name == "run_shell"][:3]
    payload = {
        "title": "Recovered coding workflow",
        "persistence_action": "create",
        "start_turn_id": turns[0] if turns else 0,
        "end_turn_id": turns[-1] if turns else 0,
        "scenario": {
            "applies_when": ["A similar coding task needs the same investigation, edit, and validation pattern."],
            "not_applies_when": ["The current task has no overlapping files, tools, or symptoms."],
            "signals": [event.summary for event in events if event.event_type in {"tool_failure", "permission_denied"}][:4],
        },
        "problem": {
            "goal": next((event.summary for event in events if event.event_type == "user_message"), ""),
            "symptoms": [],
            "constraints": [],
        },
        "diagnosis": {
            "root_cause": "See the recorded evidence events before reusing this workflow.",
            "evidence_event_ids": [event.event_id for event in tool_events[:5]],
        },
        "procedure": [
            {"action": "Review the related files and reproduce the observed behavior.", "reason": "Avoid applying the pattern without local evidence.", "checkpoint": "Relevant files and failure mode are confirmed."},
            {"action": "Apply the same edit strategy in the matching scope.", "reason": "The prior task converged through this tool sequence.", "checkpoint": "Code edit is complete."},
            {"action": "Run the same validation commands.", "reason": "Reuse only when validation passes.", "checkpoint": "; ".join(commands) or "Run project tests."},
        ],
        "pitfalls": [],
        "validation": [
            {
                "method": event.summary,
                "expected_result": "Command succeeds.",
                "evidence_event_ids": [event.event_id],
            }
            for event in tool_events
            if event.tool_name == "run_shell" and event.status == "success"
        ][:3],
        "related_file_patterns": files,
        "retrieval_queries": files + [event.summary for event in events if event.event_type == "user_message"][:2],
        "tags": ["experience", "coding-agent", "unverified"],
    }
    payload["description"] = _experience_description(payload)
    return payload


def _has_substantive_activity(events: list[TaskEvent]) -> bool:
    return any(
        event.tool_name in {"write_file", "edit_file", "run_shell"}
        or event.event_type in {"tool_failure", "permission_denied"}
        for event in events
    )


def _has_validation_evidence(payload: dict[str, Any], event_by_id: dict[str, TaskEvent]) -> bool:
    for item in payload.get("validation") or []:
        for event_id in item.get("evidence_event_ids") or []:
            event = event_by_id.get(event_id)
            if event and event.tool_name == "run_shell" and event.status == "success":
                return True
    return False


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Extractor output must be a JSON object.")
    return value


def _event_to_dict(event: TaskEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "turn_id": event.turn_id,
        "event_type": event.event_type,
        "timestamp": event.timestamp,
        "summary": event.summary,
        "tool_name": event.tool_name,
        "tool_input": event.tool_input,
        "status": event.status,
        "duration_ms": event.duration_ms,
    }


def summarize_tool_input(name: str, inp: dict[str, Any]) -> str:
    if name in {"read_file", "write_file", "edit_file"}:
        return str(inp.get("file_path") or "")
    if name == "grep_search":
        return f"{inp.get('pattern', '')} in {inp.get('path', '.')}"
    if name == "list_files":
        return str(inp.get("path") or ".")
    if name == "run_shell":
        return str(inp.get("command") or "")
    if name == "knowledge_search":
        return str(inp.get("query") or "")
    return json.dumps(_redact_json(inp), ensure_ascii=False)[:MAX_TEXT_CHARS]


def summarize_tool_result(tool_name: str, result: str) -> tuple[str, str]:
    lowered = result.lower()
    failure_prefixes = ("error:", "warning:", "command failed", "command timed out", "action denied", "user denied")
    status = "failure" if lowered.startswith(failure_prefixes) else "success"
    return status, _summarize_text(result, MAX_TOOL_RESULT_CHARS)


def _summarize_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _redact(text: str) -> str:
    out = text
    for pattern in SENSITIVE_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]" if m.lastindex and m.lastindex >= 1 else "[REDACTED]", out)
    return out


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if re.search(r"(?i)(api[_-]?key|token|secret|password|authorization)", str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_json(item)
        return redacted
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _normalize_action(value: object) -> str:
    action = str(value or "create").strip().lower()
    return action if action in {"create", "merge", "skip"} else "create"


def _normalize_mapping(value: object, defaults: dict[str, Any]) -> dict[str, Any]:
    out = dict(defaults)
    if isinstance(value, dict):
        out.update(value)
    for key, item in out.items():
        if isinstance(item, list):
            out[key] = _clean_list(item)
        elif not isinstance(item, dict):
            out[key] = _clean_scalar(item)
    return out


def _normalize_steps(value: object) -> list[dict[str, str]]:
    steps = value if isinstance(value, list) else []
    out = []
    for item in steps[:8]:
        if isinstance(item, dict):
            out.append({
                "action": _clean_scalar(item.get("action")),
                "reason": _clean_scalar(item.get("reason")),
                "checkpoint": _clean_scalar(item.get("checkpoint")),
            })
    return [step for step in out if step["action"]]


def _normalize_pitfalls(value: object) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else []
    out = []
    for item in items[:6]:
        if isinstance(item, dict):
            out.append({
                "symptom": _clean_scalar(item.get("symptom")),
                "cause": _clean_scalar(item.get("cause")),
                "correction": _clean_scalar(item.get("correction")),
            })
    return [item for item in out if item["symptom"] or item["correction"]]


def _normalize_validation(value: object) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    out = []
    for item in items[:6]:
        if isinstance(item, dict):
            out.append({
                "method": _clean_scalar(item.get("method")),
                "expected_result": _clean_scalar(item.get("expected_result")),
                "evidence_event_ids": _clean_list(item.get("evidence_event_ids"), limit=8),
            })
    return [item for item in out if item["method"]]


def _normalize_tags(value: object) -> list[str]:
    tags = ["experience"]
    tags.extend(_clean_list(value, limit=12))
    return _append_unique([_slugify(tag)[:40] for tag in tags if tag], "coding-agent")[:14]


def _clean_list(value: object, *, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    values = [_clean_scalar(item) for item in raw]
    return list(dict.fromkeys(item for item in values if item))[:limit]


def _clean_scalar(value: object) -> str:
    return _redact(_summarize_text(str(value or ""), 500))


def _referenced_event_ids(payload: dict[str, Any]) -> set[str]:
    ids = set(_clean_list(payload.get("diagnosis", {}).get("evidence_event_ids"), limit=20))
    for item in payload.get("validation") or []:
        ids.update(_clean_list(item.get("evidence_event_ids"), limit=20))
    return ids


def _remove_missing_event_ids(payload: dict[str, Any], missing: set[str]) -> None:
    diagnosis = payload.get("diagnosis") or {}
    diagnosis["evidence_event_ids"] = [item for item in diagnosis.get("evidence_event_ids", []) if item not in missing]
    for item in payload.get("validation") or []:
        item["evidence_event_ids"] = [eid for eid in item.get("evidence_event_ids", []) if eid not in missing]


def _append_unique(values: list[str], value: str) -> list[str]:
    return values if value in values else [*values, value]


def _bullets(values: object, label: str) -> str:
    cleaned = _clean_list(values, limit=8)
    if not cleaned:
        return f"- {label}: (none)"
    return "\n".join(f"- {label}: {item}" for item in cleaned)


def _frontmatter_description(payload: dict[str, Any]) -> str:
    return _experience_description(payload)


def _experience_description(payload: dict[str, Any]) -> str:
    explicit = _clean_scalar(payload.get("description"))
    if explicit:
        return explicit
    scenario = payload.get("scenario") or {}
    problem = payload.get("problem") or {}
    validation = payload.get("validation") or []
    parts: list[str] = []
    applies = _clean_list(scenario.get("applies_when"), limit=2)
    signals = _clean_list(scenario.get("signals"), limit=3) + _clean_list(problem.get("symptoms"), limit=2)
    files = _clean_list(payload.get("related_file_patterns"), limit=4)
    commands = [
        _clean_scalar(item.get("method"))
        for item in validation[:3]
        if isinstance(item, dict) and item.get("method")
    ]
    goal = _clean_scalar(problem.get("goal"))
    if applies:
        parts.append("Applies when " + "; ".join(applies))
    elif goal:
        parts.append("Goal: " + goal)
    if signals:
        parts.append("Signals: " + "; ".join(signals[:4]))
    if files:
        parts.append("Files: " + "; ".join(files))
    if commands:
        parts.append("Validate with " + "; ".join(commands))
    return _summarize_text(" ".join(parts) or _clean_scalar(payload.get("title")) or "Reusable coding experience.", 900)


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _slugify(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\u3400-\u9fff._-]+", "-", text)
    return text.strip("-._")


def _experience_id(path: Path) -> str:
    match = re.match(r"^(\d+)-(.+)$", path.stem)
    return match.group(1) if match else path.stem


def _read_markdown_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _project_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


_default_experience_manager: ExperienceManager | None = None
_default_experience_project: Path | None = None


def get_experience_manager() -> ExperienceManager:
    global _default_experience_manager, _default_experience_project
    project = Path.cwd().resolve()
    if _default_experience_manager is None or _default_experience_project != project:
        _default_experience_manager = ExperienceManager(TaskJournal("experience-tools"), project_root=project)
        _default_experience_project = project
    return _default_experience_manager


def build_experience_prompt_section() -> str:
    try:
        manager = get_experience_manager()
        if not manager.root.exists():
            return ""
        return manager.index.build_manifest()
    except Exception:
        return ""


async def execute_experience_search(inp: dict[str, Any]) -> str:
    query = str(inp.get("query") or "").strip()
    if not query:
        return "Error: experience_search requires a non-empty query."
    try:
        top_k = int(inp.get("top_k", 3))
    except (TypeError, ValueError):
        return "Error: experience_search top_k must be an integer."
    manager = get_experience_manager()
    hits = await manager.index.search(query, top_k=top_k)
    return format_experience_hits(query, hits)


def execute_experience_show(inp: dict[str, Any]) -> str:
    experience_id = str(inp.get("experience_id") or "").strip()
    if not experience_id:
        return "Error: experience_show requires an experience_id."
    manager = get_experience_manager()
    try:
        entry, content = manager.read_entry(experience_id)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
    card = manager.index.get(entry.card_id or entry.id)
    if card is None:
        return (
            f'<experience id="{entry.id}" untrusted="true">\n'
            "This historical experience has no retrieval card. Verify it against the current code before reuse.\n"
            f"Source: {entry.path}\n\n{content.replace('</experience>', '&lt;/experience&gt;')}\n"
            "</experience>"
        )
    return format_experience_body(card, content)
