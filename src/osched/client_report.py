"""Client report (business.md B2) — the commercialization deliverable.

A client-facing filtered view, one report per project, over a period
(weekly default 7 days, monthly 30):
  1. Period summary — submissions, success rate, live links (new + total)
  2. Live-link list — the evidence the client pays for (landing URLs
     verified live by verify.py)
  3. Activity detail — what was submitted where, per channel
  4. Awaiting client decision — human-queue items that need client input

Recipient: project `report.email`, falling back to the global
`report.email` (client-intake §5.4 summary role). Empty = skip send,
run_log only — same ratified default as the internal daily report.

中文：客户报告（B2 商业化交付物）；按项目+周期过滤；实链清单是客户付费
证据；收件人取项目 report_email，缺省回落全局 report.email；空=不发只记。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .report import _smtp_settings, send_report

CHANNEL_LABEL = {"form": "directory form", "email": "outreach email"}


def _period(days: int, ref: str | None) -> tuple[str, str]:
    """Return (start ISO date, end ISO date) — end is inclusive 'today'."""
    end = datetime.strptime(ref, "%Y-%m-%d").date() if ref else date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _safe_note(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------------
# Section builders — all filtered by project_id and [start, end]
# --------------------------------------------------------------------------

def _sec_summary(conn, pid: int, start: str, end: str) -> tuple[str, list[str]]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN pl.state IN ('submitted','sent','live') THEN 1
                   ELSE 0 END) AS submitted,
          SUM(CASE WHEN pl.state = 'live' THEN 1 ELSE 0 END) AS live_new,
          SUM(CASE WHEN pl.state = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM plan pl
        WHERE pl.project_id = ? AND pl.executed_at >= ?
          AND pl.executed_at < date(?, '+1 day') AND pl.state != 'queued'
        """,
        (pid, start, end),
    ).fetchone()
    submitted = row["submitted"] or 0
    live_new = row["live_new"] or 0
    failed = row["failed"] or 0
    live_total = conn.execute(
        "SELECT COUNT(*) AS n FROM plan WHERE project_id=? AND state='live'",
        (pid,),
    ).fetchone()["n"]
    rate = f"{submitted and live_total / max(submitted, 1) * 100:.0f}%"
    lines = [
        f"  submissions this period: {submitted} "
        f"(form/email outreach), {failed} failed",
        f"  live links: +{live_new} new this period, {live_total} total",
        f"  confirmation rate (live/submissions, cumulative): {rate}",
    ]
    return "1. Summary", lines


def _sec_live_links(conn, pid: int, cfg: dict | None = None) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT p.domain AS dir_domain, pl.result_note, pl.executed_at,
               pl.id AS plan_id
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.project_id = ? AND pl.state = 'live'
        ORDER BY pl.executed_at DESC
        """,
        (pid,),
    ).fetchall()
    lines = []
    for r in rows:
        landing = _safe_note(r["result_note"]).get("landing", "")
        where = f" on {r['dir_domain']}" if r["dir_domain"] else ""
        lines.append(f"  {landing}{where} (submitted {r['executed_at'][:10]})")
        if cfg:
            from .evidence import url_for
            lines.append(f"    receipt: {url_for(cfg, r['plan_id'])}")
    if not lines:
        lines = ["  (no live links verified yet — first ones typically ",
                 "   appear ~3 days after submission)"]
    return f"2. Live links ({len(rows)})", lines


def _sec_activity(conn, pid: int, start: str, end: str) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT pl.channel, pl.state, COUNT(*) AS n, p.domain AS dir_domain
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.project_id = ? AND pl.executed_at >= ?
          AND pl.executed_at < date(?, '+1 day') AND pl.state != 'queued'
        GROUP BY pl.channel, pl.state, p.domain ORDER BY pl.channel, n DESC
        """,
        (pid, start, end),
    ).fetchall()
    lines = []
    for r in rows:
        lines.append(f"  {CHANNEL_LABEL.get(r['channel'], r['channel']):<16} "
                     f"{r['state']:<10} x{r['n']:<4} {r['dir_domain']}")
    if not lines:
        lines = ["  (no submissions this period)"]
    return "3. Activity detail", lines


def _sec_decisions(conn, pid: int) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT pl.state, COUNT(*) AS n FROM plan pl
        WHERE pl.project_id = ? AND pl.state IN ('human', 'failed')
        GROUP BY pl.state
        """,
        (pid,),
    ).fetchall()
    lines = []
    for r in rows:
        lines.append(f"  {r['n']} item(s) in '{r['state']}' — we are "
                     "handling these; client action usually not needed")
    decisions = conn.execute(
        """SELECT COUNT(*) AS n FROM match m
           WHERE m.project_id = ? AND m.state = 'pending'""",
        (pid,),
    ).fetchone()["n"]
    if decisions:
        lines.append(f"  {decisions} candidate site(s) pending internal "
                     "review — will appear in future submissions")
    if not lines:
        lines = ["  (nothing awaiting decision)"]
    return "4. Awaiting decision", lines


# --------------------------------------------------------------------------
# Render + send
# --------------------------------------------------------------------------

def build_client_report(conn, project: dict, *, days: int = 7,
                        ref: str | None = None,
                        cfg: dict | None = None) -> str:
    start, end = _period(days, ref)
    pid = project["_id"]
    sections = [
        _sec_summary(conn, pid, start, end),
        _sec_live_links(conn, pid, cfg=cfg),
        _sec_activity(conn, pid, start, end),
        _sec_decisions(conn, pid),
    ]
    head = (f"{project['domain']} outreach report — {start} to {end}\n"
            + "=" * 60 + "\n")
    body = "\n".join(
        title + "\n" + "\n".join(lines) for title, lines in sections
    )
    return head + body + "\n"


def run_client_report(conn, cfg: dict, project: dict, *, days: int = 7,
                      ref: str | None = None, dry_run: bool = False) -> dict:
    """Build (+ optionally send) one client report; always run_log."""
    text = build_client_report(conn, project, days=days, ref=ref, cfg=cfg)
    outcome = {"built": True, "chars": len(text),
               "project": project["domain"], "days": days}
    recipient = ((project.get("report") or {}).get("email")
                 or cfg.get("report", {}).get("email") or "").strip()
    if not recipient:
        outcome.update({"sent": False, "reason": "no recipient configured"})
    if not dry_run and recipient:
        try:
            import pathlib
            saved = cfg.get("report", {}).get("smtp_env_file")
            if saved and not saved.startswith("/"):
                repo_root = pathlib.Path(cfg.get("_cfg_dir", ".")).parent
                cfg = {**cfg, "report": {
                    **cfg.get("report", {}),
                    "smtp_env_file": str(repo_root / saved)}}
            send_cfg = {**cfg, "report": {
                **cfg.get("report", {}), "email": recipient}}
            day = ref or date.today().isoformat()
            outcome.update(send_report(send_cfg, text, day))
        except Exception as e:  # noqa: BLE001 — report must never crash cron
            outcome.update({"sent": False, "reason": str(e)[:150]})
        conn.execute(
            "INSERT INTO run_log(action, target, result, note) "
            "VALUES('client-report', ?, ?, ?)",
            (f"{project['domain']}:{outcome.get('days', days)}d",
             "sent" if outcome.get("sent") else "skipped/failed",
             json.dumps({k: v for k, v in outcome.items()
                         if k not in ("built", "text")},
                        ensure_ascii=False)),
        )
        conn.commit()
    outcome["text"] = text
    return outcome
