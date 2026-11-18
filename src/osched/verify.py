"""Live-link verification ledger (business layer B1).

Submission != live link. A plan that reached `submitted`/`sent` is
re-visited N days later: fetch the directory page where the link should
now appear and look for the client's landing URL. Verdicts:

  live         link found -> plan.state='live' (the deliverable)
  missing      not found yet; retried up to MAX_ATTEMPTS times, then the
               plan goes to the human queue (maybe rejected, maybe wrong
               page — needs eyes)
  unreachable  network error -> stays due for retry (transient)
  bot-blocked  403/412/429 anti-bot -> retried later, never counted as
               failure (per the pool convention these are not dead)

Latest verdict per plan in link_verify; every check also lands in run_log
and the daily report picks up today's live count.

中文：实链确认——提交 N 天后回访目录页找落地 URL；live 即交付物；
missing 重试上限后进人工；bot-blocked 只是稍后再试。
"""
from __future__ import annotations

import json
import re
import urllib.parse

from . import probe as probe_mod

MAX_ATTEMPTS = 3        # missing-checks before human handoff
RETRY_AFTER = 2         # days between attempts


def _landing(plan_note: str, fallback: str) -> str:
    try:
        note = json.loads(plan_note or "{}")
        return note.get("landing") or fallback
    except json.JSONDecodeError:
        return fallback


def _page_contains_link(html: str, landing: str) -> bool:
    """Landing present on page: raw substring, URL-encoded variant, or any
    href carrying our host (covers /out?to=... redirect styles)."""
    if not landing:
        return False
    landing_l = landing.lower().rstrip("/")
    hay = html.lower().replace("&amp;", "&")
    if landing_l in hay:
        return True
    if urllib.parse.quote(landing_l, safe="") in hay:
        return True
    host = urllib.parse.urlparse(landing_l).netloc.lower()
    if not host:
        return False
    for m in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', hay):
        if host in urllib.parse.unquote(m.group(1)).lower():
            return True
    return False


def due_verify(conn, project_id: int | None = None, *, days_after: int = 3,
               limit: int = 0) -> list:
    """Plans old enough to verify, whose latest verdict allows a new check."""
    rows = conn.execute(
        """
        SELECT pl.*, p.domain AS pool_domain, p.entry_url, p.url
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.state IN ('submitted', 'sent')
          AND pl.executed_at <= datetime('now', ?)
          AND NOT EXISTS (
                SELECT 1 FROM link_verify v
                WHERE v.plan_id = pl.id AND v.result = 'live')
          AND (SELECT COUNT(*) FROM run_log
               WHERE action='verify' AND target LIKE '%plan#' || pl.id || '%'
                     AND result='missing') < ?
        ORDER BY pl.executed_at
        """,
        (f"-{days_after} days", MAX_ATTEMPTS),
    ).fetchall()
    if project_id:
        rows = [r for r in rows if r["project_id"] == project_id]
    return rows[:limit] if limit else rows


def check_one(conn, row) -> dict:
    """Re-visit one plan's directory page and look for the landing URL."""
    landing = _landing(row["result_note"], "")
    target = row["entry_url"] or row["url"]
    status, _final, html = probe_mod._fetch(target, timeout=15.0)

    if status in (403, 412, 429):
        outcome = {"result": "bot-blocked", "http": status,
                   "note": "anti-bot; retry later"}
    elif status == 0:
        outcome = {"result": "unreachable", "http": 0,
                   "note": "fetch error; retry later"}
    elif status >= 400:
        outcome = {"result": "unreachable", "http": status,
                   "note": f"http {status}"}
    elif html and _page_contains_link(html, landing):
        outcome = {"result": "live", "http": status, "note": "landing found"}
    else:
        outcome = {"result": "missing", "http": status,
                   "note": "landing not on page"}

    conn.execute(
        """INSERT INTO link_verify(plan_id, result, http_status, note)
           VALUES(?,?,?,?)
           ON CONFLICT(plan_id) DO UPDATE SET checked_at=datetime('now'),
             result=excluded.result, http_status=excluded.http_status,
             note=excluded.note""",
        (row["id"], outcome["result"], outcome["http"], outcome["note"]),
    )
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('verify', ?, ?, ?)",
        (f"{row['pool_domain']} plan#{row['id']}", outcome["result"],
         json.dumps({k: v for k, v in outcome.items() if k != "result"},
                    ensure_ascii=False)),
    )

    if outcome["result"] == "live":
        note = _safe_note(row["result_note"])
        note["live"] = True
        conn.execute(
            "UPDATE plan SET state='live', result_note=? WHERE id=?",
            (json.dumps(note, ensure_ascii=False), row["id"]),
        )
    elif outcome["result"] == "missing":
        attempts = conn.execute(
            """SELECT COUNT(*) AS n FROM run_log
               WHERE action='verify' AND target LIKE ?
                     AND result='missing'""",
            (f"%plan#{row['id']}%",),
        ).fetchone()["n"]
        if attempts >= MAX_ATTEMPTS:
            conn.execute(
                """UPDATE plan SET state='human', result_note=?
                   WHERE id=?""",
                (json.dumps({"note": f"live check exhausted "
                                     f"({attempts}x missing) -> human queue"},
                            ensure_ascii=False), row["id"]),
            )
            outcome["note"] += " -> exhausted, human queue"
    conn.commit()
    return outcome


def _safe_note(note_text: str) -> dict:
    try:
        return json.loads(note_text or "{}")
    except json.JSONDecodeError:
        return {}


def run_verify(conn, project_id: int | None = None, *, days_after: int = 3,
               limit: int = 0) -> dict:
    """Verify all due plans. Returns summary + per-row detail."""
    rows = due_verify(conn, project_id, days_after=days_after, limit=limit)
    stats = {"due": len(rows), "live": 0, "missing": 0, "unreachable": 0,
             "bot-blocked": 0, "rows": []}
    for row in rows:
        outcome = check_one(conn, row)
        stats[outcome["result"]] += 1
        stats["rows"].append({"plan_id": row["id"],
                              "domain": row["pool_domain"],
                              "result": outcome["result"],
                              "note": outcome["note"]})
    return stats


def live_count(conn, day: str) -> int:
    """Plans that flipped to live on a given day (for the daily report)."""
    return conn.execute(
        """SELECT COUNT(*) AS n FROM plan
           WHERE state='live' AND executed_at >= ?""",
        (day,),
    ).fetchone()["n"]
