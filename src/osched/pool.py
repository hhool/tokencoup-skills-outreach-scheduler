"""Import pipeline: adapters -> SourceRecord -> validation -> pool insert.

Dedupe key: (domain, url) — UNIQUE in the pool table. Re-importing the same
file updates last_seen/evidence but never duplicates rows. 中文：按（域名,URL）
去重；重复导入只刷新 last_seen 与证据，绝不产生重复行。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .adapters import SourceAdapter, Issue
from .models import SourceRecord


def import_source(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    max_errors: int = 50,
) -> dict:
    """Import one adapter's discovered files into the pool.

    Returns a stats dict: {files, rows, inserted, updated, skipped_errors,
    issues:[...]} — printed by the CLI, written to run_log.
    """
    stats = {
        "kind": adapter.kind,
        "files": 0,
        "rows": 0,
        "inserted": 0,
        "updated": 0,
        "invalid": 0,
        "issues": [],
    }

    for sfile in adapter.discover():
        stats["files"] += 1
        for rec in adapter.parse(sfile):
            stats["rows"] += 1

            issues = adapter.validate(rec)
            errors = [i for i in issues if i.severity == "error"]
            if errors:
                stats["invalid"] += 1
                for iss in errors:
                    stats["issues"].append(
                        f"{sfile.path.name}#row{rec.source.row_no}: {iss.message}"
                    )
                if len(stats["issues"]) >= max_errors:
                    return stats
                continue

            inserted = _upsert(conn, rec)
            if inserted:
                stats["inserted"] += 1
            else:
                stats["updated"] += 1

    conn.execute(
        "INSERT INTO run_log(action, target, result, note) VALUES(?,?,?,?)",
        (
            "import",
            adapter.kind,
            "ok",
            json.dumps({k: v for k, v in stats.items() if k != "issues"},
                       ensure_ascii=False),
        ),
    )
    conn.commit()
    return stats


def _upsert(conn: sqlite3.Connection, rec: SourceRecord) -> bool:
    """Insert or refresh one record. Returns True if newly inserted."""
    domain = rec.derive_domain()
    exists = conn.execute(
        "SELECT 1 FROM pool WHERE domain=? AND url=?", (domain, rec.url.strip())
    ).fetchone()
    if exists:
        conn.execute(
            """
            UPDATE pool SET source_kind=?, source_file=?, source_row=?,
                            evidence_json=?, hints_json=?, raw_json=?,
                            last_seen=datetime('now')
            WHERE domain=? AND url=?
            """,
            (
                rec.source.kind, rec.source.file, rec.source.row_no,
                json.dumps(asdict(rec.evidence), ensure_ascii=False),
                json.dumps(rec.hints, ensure_ascii=False),
                json.dumps(rec.raw, ensure_ascii=False),
                domain, rec.url.strip(),
            ),
        )
        return False
    conn.execute(
        """
        INSERT INTO pool(url, domain, source_kind, source_file, source_row,
                         evidence_json, hints_json, raw_json)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            rec.url.strip(), domain, rec.source.kind, rec.source.file,
            rec.source.row_no,
            json.dumps(asdict(rec.evidence), ensure_ascii=False),
            json.dumps(rec.hints, ensure_ascii=False),
            json.dumps(rec.raw, ensure_ascii=False),
        ),
    )
    return True
