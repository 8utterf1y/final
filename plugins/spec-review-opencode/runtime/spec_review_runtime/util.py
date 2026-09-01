from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:length]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def resolve_inside(repo: Path, value: str, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    path = path.resolve() if path.is_absolute() else (repo / path).resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"路径不存在：{path}")
    return path
