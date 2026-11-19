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

import hashlib
import json
import re
import urllib.parse

from . import probe as probe_mod
from .guard import sync_quota_used

MAX_ATTEMPTS = 3        # missing-checks before human handoff
RETRY_AFTER = 2         # days between attempts
RECHECK_AFTER_DAYS = 14  # live links are re-visited on this cadence

ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
REL_RE = re.compile(r"rel\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _find_anchor(html: str, landing: str) -> dict:
    """Locate the anchor that carries the landing URL and record its rel
    attribute and anchor text — the difference between 'we think it is live'
    and a receipt a client can forward to their boss."""
    landing_l = landing.lower().rstrip("/")
    host = urllib.parse.urlparse(landing_l).netloc.lower()
    for m in ANCHOR_RE.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = HREF_RE.search(attrs)
        if not hm:
            continue
        href = urllib.parse.unquote(hm.group(1)).lower().replace("&amp;", "&")
        if landing_l not in href and not (host and host in href):
            continue
        rm = REL_RE.search(attrs)
        text = re.sub(r"<[^>]+>", " ", inner)
        return {"href": hm.group(1),
                "rel": rm.group(1).lower().strip() if rm else "",
                "text": " ".join(text.split())[:120]}
    return {}


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
    """Plans old enough to verify, whose latest verdict allows a new check.

    Two branches: first-time confirmation for submitted/sent plans, and the
    recheck cadence for plans already live — the survival curve needs
    periodic revisits, not just one confirmation.
    """
    rows = conn.execute(
        """
        SELECT pl.*, p.domain AS pool_domain, p.entry_url, p.url
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE (
          (pl.state IN ('submitted', 'sent')
            AND pl.executed_at <= datetime('now', ?)
            AND NOT EXISTS (
                  SELECT 1 FROM link_verify v
                  WHERE v.plan_id = pl.id AND v.result = 'live')
            AND (SELECT COUNT(*) FROM run_log
                 WHERE action='verify' AND target LIKE '%plan#' || pl.id || '%'
                       AND result='missing') < ?)
          OR
          (pl.state = 'live'
            AND COALESCE((SELECT MAX(checked_at) FROM link_check
                          WHERE plan_id = pl.id), '1970-01-01')
                <= datetime('now', ?))
        )
        ORDER BY pl.executed_at
        """,
        (f"-{days_after} days", MAX_ATTEMPTS,
         f"-{RECHECK_AFTER_DAYS} days"),
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

    anchor = {}
    page_sha = ""
    if outcome["result"] == "live" and html:
        anchor = _find_anchor(html, landing)
        page_sha = hashlib.sha256(
            html.encode("utf-8", "replace")).hexdigest()

    conn.execute(
        """INSERT INTO link_verify(plan_id, result, http_status, note,
                                   rel, anchor_text, page_sha256)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(plan_id) DO UPDATE SET checked_at=datetime('now'),
             result=excluded.result, http_status=excluded.http_status,
             note=excluded.note, rel=excluded.rel,
             anchor_text=excluded.anchor_text,
             page_sha256=excluded.page_sha256""",
        (row["id"], outcome["result"], outcome["http"], outcome["note"],
         anchor.get("rel", ""), anchor.get("text", ""), page_sha),
    )
    # Append-only event: the survival curve is built from these rows.
    conn.execute(
        """INSERT INTO link_check(plan_id, result, http_status, rel,
                                  anchor_text, anchor_href, page_sha256, note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (row["id"], outcome["result"], outcome["http"],
         anchor.get("rel", ""), anchor.get("text", ""),
         anchor.get("href", ""), page_sha, outcome["note"]),
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
        # A delivered link consumes a credit — mirror it for the portal and
        # for the scheduler's credit ceiling (guard.allowance).
        sync_quota_used(conn, row["project_id"])
    elif outcome["result"] == "missing":
        # Exhaustion counter: for plans already live, count only missings
        # AFTER the first live verdict — pre-confirmation misses must not
        # yank a delivered link to the human queue on one recheck miss.
        like = f"%plan#{row['id']}%"
        first_live = conn.execute(
            """SELECT MIN(id) AS i FROM run_log
               WHERE action='verify' AND result='live' AND target LIKE ?""",
            (like,)).fetchone()["i"]
        q = ("SELECT COUNT(*) AS n FROM run_log WHERE action='verify' "
             "AND target LIKE ? AND result='missing'")
        params = [like]
        if first_live:
            q += " AND id > ?"
            params.append(first_live)
        attempts = conn.execute(q, params).fetchone()["n"]
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
