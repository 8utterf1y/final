from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_info (
  version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_info(version) VALUES (2);

CREATE TABLE IF NOT EXISTS index_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  sequence INTEGER UNIQUE NOT NULL,
  revision TEXT NOT NULL,
  created_at TEXT NOT NULL,
  files_total INTEGER NOT NULL,
  files_parsed INTEGER NOT NULL,
  files_reused INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  file_id TEXT PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  language TEXT,
  source_role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_versions (
  snapshot_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  parser_backend TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, file_id),
  FOREIGN KEY(snapshot_id) REFERENCES index_snapshots(snapshot_id),
  FOREIGN KEY(file_id) REFERENCES files(file_id)
);

CREATE TABLE IF NOT EXISTS symbols (
  snapshot_id TEXT NOT NULL,
  symbol_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  name TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  signature TEXT,
  backend TEXT NOT NULL,
  precision TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, symbol_id),
  FOREIGN KEY(snapshot_id) REFERENCES index_snapshots(snapshot_id),
  FOREIGN KEY(file_id) REFERENCES files(file_id)
);
CREATE INDEX IF NOT EXISTS symbols_lookup_idx
  ON symbols(snapshot_id, name, qualified_name);

CREATE TABLE IF NOT EXISTS edges (
  snapshot_id TEXT NOT NULL,
  edge_id TEXT NOT NULL,
  source_symbol_id TEXT,
  target_symbol_id TEXT,
  target_name TEXT,
  edge_type TEXT NOT NULL,
  file_id TEXT NOT NULL,
  line INTEGER NOT NULL,
  resolver TEXT NOT NULL,
  confidence REAL NOT NULL,
  resolution_status TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, edge_id),
  FOREIGN KEY(snapshot_id) REFERENCES index_snapshots(snapshot_id),
  FOREIGN KEY(file_id) REFERENCES files(file_id)
);
CREATE INDEX IF NOT EXISTS edges_source_idx
  ON edges(snapshot_id, source_symbol_id);
CREATE INDEX IF NOT EXISTS edges_target_idx
  ON edges(snapshot_id, target_symbol_id, target_name);

CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  loaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  section TEXT NOT NULL,
  source_text TEXT NOT NULL,
  statement TEXT NOT NULL,
  verifiability TEXT NOT NULL,
  ordinal INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_case_idx ON claims(case_id, ordinal);

CREATE TABLE IF NOT EXISTS review_cases (
  case_id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  base_revision TEXT,
  head_revision TEXT NOT NULL,
  mode TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  budget_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(snapshot_id) REFERENCES index_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS change_seeds (
  seed_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  path TEXT NOT NULL,
  old_start INTEGER,
  old_count INTEGER,
  new_start INTEGER,
  new_count INTEGER,
  symbol_id TEXT,
  change_type TEXT NOT NULL,
  diff_text TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES review_cases(case_id)
);
CREATE INDEX IF NOT EXISTS seeds_case_idx ON change_seeds(case_id);

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  claim_id TEXT,
  kind TEXT NOT NULL,
  path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  revision TEXT,
  content TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES review_cases(case_id)
);
CREATE INDEX IF NOT EXISTS evidence_case_claim_idx
  ON evidence(case_id, claim_id);

CREATE TABLE IF NOT EXISTS stage_runs (
  run_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  result_json TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  UNIQUE(case_id, stage),
  FOREIGN KEY(case_id) REFERENCES review_cases(case_id)
);
"""


def database_path(repo: Path) -> Path:
    state_dir = repo / ".spec-review"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "index.sqlite"


def connect(repo: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path(repo)), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("SELECT max(version) FROM schema_info").fetchone()[0]
    if version > 2:
        raise RuntimeError(f"索引数据库版本 {version} 高于当前运行时支持的版本 2")
    connection.commit()
