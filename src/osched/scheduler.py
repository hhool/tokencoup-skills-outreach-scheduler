"""Plan scheduling (DESIGN.md §5/§7).

Turns confirmed matches into dated plans: fill future dates from the
confirmed pool respecting daily_quota and channel mix. The executor (Phase
4) consumes only rows with scheduled_date <= today and state='queued'.

Landing-URL rule (client-intake §5a + §5 freshness): newest page asset wins,
homepage fallback. The chosen landing URL is stored in the plan row so the
executor never re-decides.

中文：排期生成——确认项按每日配额填充未来日期；落地 URL 在排期时定死。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .guard import allowance
from .matching import landing_url

CHANNEL_BY_SUBMIT = {
    "auto": "form", "register": "form", "free": "form",
    "listing": "form", "posting": "form",
    "pitch": "email", "guest_post": "email", "apply": "email",
}


def generate_plan(conn, project: dict, *, days: int = 7,
                  start: date | None = None) -> dict:
    """Fill scheduled dates for confirmed, unscheduled matches.

    Quota: project daily_quota per day. Channel chosen from the pool row's
    submit_type (form-first per §7).
    """
    start = start or date.today() + timedelta(days=1)
    quota = int(project.get("daily_quota", 10))

    confirmed = conn.execute(
        """
        SELECT m.id, m.score, p.submit_type, p.captcha_type
        FROM match m JOIN pool p ON p.id = m.pool_id
        WHERE m.project_id=? AND m.state='confirmed'
          AND NOT EXISTS (SELECT 1 FROM plan pl WHERE pl.match_id=m.id)
        ORDER BY m.score DESC
        """,
        (project["_id"],),
    ).fetchall()

    # Credit ceiling (guard.credit_cap): a portal project may only schedule
    # as many plans as it still has unpaid-or-paid credits for. Without this
    # the "3 free links" trial would be an unlimited work order.
    room = allowance(conn, project["_id"])
    if room is not None:
        if room <= 0:
            conn.execute(
                "INSERT INTO run_log(action, target, result, note) "
                "VALUES('schedule', ?, 'quota', ?)",
                (project["domain"], json.dumps(
                    {"scheduled": 0, "reason": "no credits left"})))
            conn.commit()
            return {"scheduled": 0, "by_channel": {},
                    "unscheduled_confirmed": len(confirmed),
                    "window": f"{start.isoformat()}+{days}d",
                    "blocked": "quota_exhausted"}
        confirmed = confirmed[:room]

    slots: list[tuple[str, int]] = []   # (scheduled_date, remaining)
    counts: dict[str, int] = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        slots.append((d, quota))

    scheduled = 0
    for m in confirmed:
        channel = CHANNEL_BY_SUBMIT.get(m["submit_type"] or "", "form")
        landing = landing_url(project, None)
        for idx, (d, remaining) in enumerate(slots):
            if remaining > 0:
                conn.execute(
                    """
                    INSERT INTO plan(project_id, match_id, scheduled_date,
                        channel, template_id, state, result_note)
                    VALUES(?,?,?,?,?, 'queued', ?)
                    ON CONFLICT(match_id, scheduled_date) DO NOTHING
                    """,
                    (project["_id"], m["id"], d, channel,
                     "dir-submit" if channel == "form" else "outreach-mail",
                     json.dumps({"landing": landing, "captcha":
                                 m["captcha_type"]}, ensure_ascii=False)),
                )
                slots[idx] = (d, remaining - 1)
                scheduled += 1
                counts[channel] = counts.get(channel, 0) + 1
                break

    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('schedule', ?, 'ok', ?)",
        (project["domain"], json.dumps(
            {"scheduled": scheduled, "by_channel": counts,
             "start": start.isoformat(), "days": days})),
    )
    conn.commit()
    return {"scheduled": scheduled, "by_channel": counts,
            "unscheduled_confirmed": len(confirmed) - scheduled,
            "window": f"{start.isoformat()}+{days}d"}


def due_today(conn, project_id: int, today: str | None = None) -> list:
    """Plan rows the executor should run now (queued, scheduled <= today).

    Capped by remaining credits: a portal project never gets more work
    executed than it still has credits for, even if an operator queued
    extra plans by hand.
    """
    today = today or date.today().isoformat()
    rows = conn.execute(
        """
        SELECT pl.*, p.url, p.domain AS pool_domain, p.entry_url,
               p.captcha_type, p.contact_email, p.submit_type
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.project_id=? AND pl.state='queued'
              AND pl.scheduled_date <= ?
        ORDER BY pl.scheduled_date
        """,
        (project_id, today),
    ).fetchall()
    room = allowance(conn, project_id, include_reserved=True)
    if room is not None:
        return rows[:room]
    return rows
