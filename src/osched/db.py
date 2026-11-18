"""SQLite pool storage (DESIGN.md §3).

Pool is the single source of truth for this system. Schema is created
idempotently; all DB files live under data_root (gitignored, never committed).
中文：池是本系统唯一真相源；库文件一律在 data_root 下，gitignore，永不入库。
"""
from __future__ import annotations

import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  url            TEXT NOT NULL,
  domain         TEXT NOT NULL,
  source_kind    TEXT NOT NULL,
  source_file    TEXT NOT NULL,
  source_row     INTEGER NOT NULL,
  evidence_json  TEXT NOT NULL DEFAULT '{}',
  hints_json     TEXT NOT NULL DEFAULT '{}',
  raw_json       TEXT NOT NULL DEFAULT '{}',
  -- probe stage (filled later; unprobed until then)
  entry_url      TEXT,
  has_submit_form INTEGER NOT NULL DEFAULT 0,
  captcha_type   TEXT NOT NULL DEFAULT 'unknown',
  contact_email  TEXT,
  probe_state    TEXT NOT NULL DEFAULT 'unprobed',
  -- classification (filled later)
  site_type      TEXT,
  submit_type    TEXT,
  wave           INTEGER,
  category       TEXT,
  dr             REAL,
  language       TEXT,
  geo            TEXT,
  status         TEXT NOT NULL DEFAULT 'active',
  first_seen     TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain, url)
);

CREATE TABLE IF NOT EXISTS project (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  domain      TEXT NOT NULL UNIQUE,
  tags_json   TEXT NOT NULL DEFAULT '[]',
  status      TEXT NOT NULL DEFAULT 'active',
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS match (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    INTEGER NOT NULL REFERENCES project(id),
  pool_id       INTEGER NOT NULL REFERENCES pool(id),
  score         REAL NOT NULL DEFAULT 0,
  score_json    TEXT NOT NULL DEFAULT '{}',
  state         TEXT NOT NULL DEFAULT 'pending',
  confirmed_at  TEXT,
  UNIQUE(project_id, pool_id)
);

CREATE TABLE IF NOT EXISTS plan (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id     INTEGER NOT NULL REFERENCES project(id),
  match_id       INTEGER NOT NULL REFERENCES match(id),
  scheduled_date TEXT NOT NULL,
  channel        TEXT NOT NULL,
  template_id    TEXT,
  state          TEXT NOT NULL DEFAULT 'queued',
  captcha_solved INTEGER,
  captcha_cost   REAL NOT NULL DEFAULT 0,
  executed_at    TEXT,
  result_note    TEXT,
  UNIQUE(match_id, scheduled_date)
);

-- Per-project content assets (DESIGN.md §5a): TDK, site-summary, category,
-- sender identity copy. Fallback chain lives in the consumers.
CREATE TABLE IF NOT EXISTS project_asset (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES project(id),
  kind       TEXT NOT NULL,          -- site-title | site-summary | category | ...
  content    TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'manual',   -- manual | probe | template
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, kind)
);

-- Live-link verification (business layer B1): submission != live link.
-- Latest verdict per plan; full check history lives in run_log.
CREATE TABLE IF NOT EXISTS link_verify (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id     INTEGER NOT NULL REFERENCES plan(id),
  checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
  result      TEXT NOT NULL,     -- live | missing | unreachable | bot-blocked
  http_status INTEGER,
  note        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_link_verify_plan ON link_verify(plan_id);

CREATE TABLE IF NOT EXISTS run_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL DEFAULT (datetime('now')),
  action  TEXT NOT NULL,
  target  TEXT,
  result  TEXT,
  note    TEXT
);

-- Submit-path cache (DESIGN.md §4c): discovered submit endpoints are
-- first-class assets — recorded once, reused across submissions, kept fresh.
-- One canonical entry per domain (the best endpoint found so far).
CREATE TABLE IF NOT EXISTS probe_cache (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  domain         TEXT NOT NULL UNIQUE,
  entry_url      TEXT NOT NULL,
  form_signature TEXT,
  captcha_type   TEXT,
  contact_email  TEXT,
  success_count  INTEGER NOT NULL DEFAULT 0,
  last_probed    TEXT NOT NULL DEFAULT (datetime('now')),
  last_verified  TEXT,
  state          TEXT NOT NULL DEFAULT 'valid',
  history_json   TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_run_log_ts ON run_log(ts);
CREATE INDEX IF NOT EXISTS idx_probe_cache_state ON probe_cache(state);

CREATE INDEX IF NOT EXISTS idx_pool_domain ON pool(domain);
CREATE INDEX IF NOT EXISTS idx_pool_status ON pool(status);
CREATE INDEX IF NOT EXISTS idx_pool_probe ON pool(probe_state);
CREATE INDEX IF NOT EXISTS idx_match_state ON match(state);
CREATE INDEX IF NOT EXISTS idx_plan_date ON plan(scheduled_date, state);
"""


def connect(data_root: pathlib.Path) -> sqlite3.Connection:
    """Open (and initialize) the pool DB under data_root. WAL mode."""
    db_path = data_root / "pool.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
