"""Multi-client operations view (business.md B5) — pure query layer.

One table per client (project): pipeline-stage counts, quota usage today,
last-execution recency; plus SLA-style alerts:

  - no submissions for N days (default 14, project `sla.max_days_idle`)
  - human queue growing beyond a threshold (default 10, `sla.max_human`)

中文：多客户运营视图（B5）——每客户一行全阶段计数 + 今日配额占用 +
空闲告警/人工队列告警；纯查询，配置里 sla 块可选。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

DEFAULT_MAX_DAYS_IDLE = 14
DEFAULT_MAX_HUMAN = 10


def _sla(project: dict, key: str, default: int) -> int:
    return int((project.get("sla") or {}).get(key, default))


def _project_rows(conn, projects: list[dict]) -> list[dict]:
    out = []
    for pr in projects:
        pid = pr["_id"]
        counts = {r["state"]: r["n"] for r in conn.execute(
            """SELECT state, COUNT(*) AS n FROM plan
               WHERE project_id=? GROUP BY state""", (pid,)).fetchall()}
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM plan WHERE project_id=? AND "
            "scheduled_date=?", (pid, date.today().isoformat()),
        ).fetchone()["n"]
        last = conn.execute(
            """SELECT MAX(executed_at) AS t FROM plan
               WHERE project_id=? AND executed_at IS NOT NULL""",
            (pid,)).fetchone()["t"]
        pending_matches = conn.execute(
            "SELECT COUNT(*) AS n FROM match WHERE project_id=? AND "
            "state='pending'", (pid,)).fetchone()["n"]
        out.append({
            "domain": pr["domain"], "counts": counts, "due_today": today,
            "last_executed": last, "pending_matches": pending_matches,
            "max_days_idle": _sla(pr, "max_days_idle", DEFAULT_MAX_DAYS_IDLE),
            "max_human": _sla(pr, "max_human", DEFAULT_MAX_HUMAN),
        })
    return out


def build_overview(conn, projects: list[dict]) -> tuple[str, list[str]]:
    rows = _project_rows(conn, projects)
    lines = [f"{'project':<30} {'pend':>5} {'queued':>7} {'sub/sent':>9} "
             f"{'live':>5} {'human+cua':>10} {'fail':>5} {'today':>6} "
             f"last executed"]
    alerts: list[str] = []
    for r in rows:
        c = r["counts"]
        human = c.get("human", 0) + c.get("cua", 0)
        done = c.get("submitted", 0) + c.get("sent", 0)
        last = (r["last_executed"] or "never")[:16]
        lines.append(
            f"{r['domain']:<30.30} {r['pending_matches']:>5} "
            f"{c.get('queued', 0):>7} {done:>9} {c.get('live', 0):>5} "
            f"{human:>10} {c.get('failed', 0):>5} {r['due_today']:>6} "
            f"{last}")
        # SLA alerts
        if r["last_executed"]:
            idle_days = (datetime.utcnow() - datetime.strptime(
                r["last_executed"][:19], "%Y-%m-%d %H:%M:%S")).days
            if idle_days >= r["max_days_idle"]:
                alerts.append(f"{r['domain']}: no submissions for "
                              f"{idle_days}d (>= {r['max_days_idle']}d)")
        elif r["counts"].get("queued", 0) or r["counts"].get("pending", 0):
            alerts.append(f"{r['domain']}: nothing ever executed "
                          "(pipeline not run yet?)")
        if human > r["max_human"]:
            alerts.append(f"{r['domain']}: human queue {human} "
                          f"(> {r['max_human']}) — resolve or raise quota")
    if not lines[1:]:
        lines.append("  (no projects)")
    return "\n".join(lines), alerts


def render_overview(conn, projects: list[dict]) -> str:
    table, alerts = build_overview(conn, projects)
    if alerts:
        table += "\n\nALERTS:\n" + "\n".join(f"  ! {a}" for a in alerts)
    else:
        table += "\n\nALERTS: (none)"
    return table
