"""Daily email report (DESIGN.md §8).

Five sections:
  1. Today's plan — item counts, by project, by channel
  2. Yesterday's completions / failures detail
  3. Pending-confirmation queue — counts + top-10 preview
  4. Pool changes — new entries, dead links, probe/classify/dr activity
  5. Human-action items — cua/human plan rows awaiting resolution

Empty recipient = skip sending, run_log only (ratified default). Send time
follows scheduler config; the CLI is idempotent and safe to run from cron.

中文：五段式日报；收件人为空=不发信只记 run_log；cron 安全可重跑。
"""
from __future__ import annotations

import json
import os
import pathlib
import smtplib
import socket
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from .config import ConfigError, require_env_key

STATE_LABEL = {
    "queued": "queued", "submitted": "submitted", "sent": "sent",
    "cua": "cua-pending", "human": "human-queue", "failed": "failed",
}


def _day(day_str: str | None) -> str:
    return day_str or date.today().isoformat()


def _prev(day_str: str) -> str:
    d = datetime.strptime(day_str, "%Y-%m-%d").date() - timedelta(days=1)
    return d.isoformat()


# --------------------------------------------------------------------------
# Section builders — each returns (title, lines)
# --------------------------------------------------------------------------

def _sec_today_plan(conn, day: str) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT pr.domain, pl.channel, pl.state, COUNT(*) AS n
        FROM plan pl JOIN project pr ON pr.id = pl.project_id
        WHERE pl.scheduled_date = ?
        GROUP BY pr.domain, pl.channel, pl.state ORDER BY pr.domain
        """,
        (day,),
    ).fetchall()
    lines = []
    total = 0
    for r in rows:
        total += r["n"]
        lines.append(f"  {r['domain']:<28} {r['channel']:<6} "
                     f"{STATE_LABEL.get(r['state'], r['state']):<12} x{r['n']}")
    if not lines:
        lines = ["  (no plans scheduled for today)"]
    return f"1. Today's plan ({total} items)", lines


def _sec_yesterday(conn, day: str) -> tuple[str, list[str]]:
    prev = _prev(day)
    rows = conn.execute(
        """
        SELECT pr.domain, pl.channel, pl.state, pl.result_note,
               p.domain AS pool_domain
        FROM plan pl
        JOIN project pr ON pr.id = pl.project_id
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.executed_at >= ? AND pl.executed_at < date(?, '+1 day')
          AND pl.state != 'queued'
        ORDER BY pl.state, pr.domain
        """,
        (prev, prev),
    ).fetchall()
    lines, note_cache = [], {}
    for r in rows:
        try:
            note = json.loads(r["result_note"] or "{}")
        except json.JSONDecodeError:
            note = {}
        key = (r["pool_domain"], r["result_note"] or "")
        note_cache[key] = note_cache.get(key, 0) + 1
    ok = sum(1 for r in rows if r["state"] in ("submitted", "sent"))
    live = conn.execute(
        "SELECT COUNT(*) AS n FROM plan WHERE state='live'"
    ).fetchone()["n"]
    bad = len(rows) - ok
    lines.append(f"  completed: {ok}, failed/pending: {bad}, "
                 f"live links total: {live}")
    for (dom, note), n in sorted(note_cache.items()):
        short = note if len(note) <= 60 else note[:57] + "..."
        lines.append(f"  [{n}x] {dom} — {short}")
    if not rows:
        lines = ["  (nothing executed yesterday)"]
    return f"2. Yesterday's completions ({prev})", lines


def _sec_pending_queue(conn) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT pr.domain, m.score, p.domain AS pool_domain, p.captcha_type
        FROM match m JOIN project pr ON pr.id = m.project_id
        JOIN pool p ON p.id = m.pool_id
        WHERE m.state = 'pending'
        ORDER BY m.score DESC LIMIT 10
        """
    ).fetchall()
    counts = conn.execute(
        """SELECT pr.domain, COUNT(*) AS n FROM match m
           JOIN project pr ON pr.id = m.project_id
           WHERE m.state='pending' GROUP BY pr.domain ORDER BY n DESC"""
    ).fetchall()
    lines = [f"  {r['domain']}: {r['n']} pending" for r in counts] or \
            ["  (no pending matches)"]
    lines.append("  top-10 preview:")
    for r in rows:
        lines.append(f"    score={r['score']:<7} captcha={r['captcha_type']:<12}"
                     f" {r['pool_domain']}")
    return "3. Pending-confirmation queue", lines


def _sec_pool_changes(conn, day: str) -> tuple[str, list[str]]:
    prev = _prev(day)
    new = conn.execute(
        "SELECT COUNT(*) AS n FROM pool WHERE first_seen >= ?", (prev,)
    ).fetchone()["n"]
    dead = conn.execute(
        "SELECT COUNT(*) AS n FROM pool WHERE status='dead'"
    ).fetchone()["n"]
    blocked = conn.execute(
        "SELECT COUNT(*) AS n FROM pool WHERE status='bot-blocked'"
    ).fetchone()["n"]
    cache_new = conn.execute(
        "SELECT COUNT(*) AS n FROM probe_cache WHERE last_probed >= ?", (prev,)
    ).fetchone()["n"]
    acts = conn.execute(
        """SELECT action, COUNT(*) AS n FROM run_log
           WHERE ts >= ? AND action IN ('probe','classify','dr','import')
           GROUP BY action""",
        (prev,),
    ).fetchall()
    act_str = ", ".join(f"{a['action']}={a['n']}" for a in acts) or "none"
    return "4. Pool changes", [
        f"  new entries since {prev}: {new}",
        f"  dead: {dead}, bot-blocked (kept): {blocked}",
        f"  probe_cache refreshed: {cache_new}",
        f"  pipeline activity: {act_str}",
    ]


def _sec_human_items(conn) -> tuple[str, list[str]]:
    rows = conn.execute(
        """
        SELECT pr.domain, pl.channel, pl.state, p.domain AS pool_domain,
               pl.result_note
        FROM plan pl
        JOIN project pr ON pr.id = pl.project_id
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.state IN ('human', 'cua', 'failed')
        ORDER BY pr.domain, pl.state
        """
    ).fetchall()
    pending_matches = conn.execute(
        "SELECT COUNT(*) AS n FROM match WHERE state='pending'"
    ).fetchone()["n"]
    lines = []
    for r in rows:
        try:
            note = json.loads(r["result_note"] or "{}").get("note", "")
        except json.JSONDecodeError:
            note = ""
        lines.append(f"  [{r['state']:<6}] {r['domain']:<28} "
                     f"{r['channel']:<6} {r['pool_domain']} {note}")
    if pending_matches:
        lines.append(f"  decisions needed: {pending_matches} pending match(es)")
    if not rows and not pending_matches:
        lines = ["  (nothing waiting on a human)"]
    return "5. Human-action items", lines


# --------------------------------------------------------------------------
# Render + send
# --------------------------------------------------------------------------

def build_report(conn, day: str | None = None) -> str:
    """Assemble the five-section plain-text report for one date."""
    day = _day(day)
    sections = [
        _sec_today_plan(conn, day),
        _sec_yesterday(conn, day),
        _sec_pending_queue(conn),
        _sec_pool_changes(conn, day),
        _sec_human_items(conn),
    ]
    head = (f"outreach-scheduler daily report — {day}\n"
            + "=" * 60 + "\n")
    body = "\n".join(
        title + "\n" + "\n".join(lines) for title, lines in sections
    )
    return head + body + "\n"


def _smtp_settings(cfg: dict) -> dict:
    rep = cfg.get("report", {})
    env_file = rep.get("smtp_env_file", "config/secrets/smtp.env")
    port = int(rep.get("smtp_port") or
               require_env_key(env_file, "SMTP_PORT") or 587)
    return {
        "env_file": env_file,
        "host": rep.get("smtp_host") or require_env_key(env_file, "SMTP_HOST"),
        "port": port,
        "ssl": port == 465,   # implicit-TLS port; 587 = STARTTLS
        "user": require_env_key(env_file, "SMTP_USER"),
        # Gmail app-password convention (SMTP_APP_PASSWORD); SMTP_PASS alias.
        "pass": _require_any(env_file, "SMTP_APP_PASSWORD", "SMTP_PASS"),
    }


def _env_has(env_file: str, key: str) -> bool:
    try:
        for line in open(env_file, encoding="utf-8"):
            if line.strip().startswith(key + "=") and \
                    line.strip().split("=", 1)[1].strip():
                return True
    except OSError:
        pass
    return False


def _require_any(env_file: str, *keys: str) -> str:
    """First env key present with a non-empty value; clear error otherwise."""
    for key in keys:
        if _env_has(env_file, key) or os.environ.get(key, "").strip():
            return require_env_key(env_file, key)
    raise ConfigError(
        f"Missing credential {' or '.join(keys)} (env or {env_file})")


def send_mail(cfg: dict, to_addr: str, subject: str, body: str,
              from_label: str = "outreach-scheduler") -> None:
    """Generic text mail over the report SMTP settings; raises on failure.

    Used by the daily report, the client portal verification mail, and
    any future B2/B3 mail paths — one credential source, one port rule.
    """
    s = _smtp_settings(cfg)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((from_label, s["user"]))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=s["host"])
    with smtplib.SMTP_SSL(s["host"], s["port"], timeout=30) if s["ssl"] \
            else smtplib.SMTP(s["host"], s["port"], timeout=30) as smtp:
        if not s["ssl"]:
            smtp.starttls()
        smtp.login(s["user"], s["pass"])
        smtp.sendmail(s["user"], [to_addr], msg.as_string())


def send_report(cfg: dict, text: str, day: str) -> dict:
    """Email the report. Recipient empty = skip (ratified: run_log only)."""
    recipient = (cfg.get("report", {}).get("email") or "").strip()
    if not recipient:
        return {"sent": False, "reason": "no recipient configured"}
    send_mail(cfg, recipient, f"[outreach-scheduler] daily report {day}",
              text)
    return {"sent": True, "recipient": recipient}


def run_report(conn, cfg: dict, cfg_dir: str, *, day: str | None = None,
               dry_run: bool = False) -> dict:
    """Build (+ optionally send) the daily report; always run_log."""
    day = _day(day)
    text = build_report(conn, day)
    outcome = {"built": True, "chars": len(text)}
    if not dry_run:
        try:
            # env-file paths in YAML are relative to the repo root, which is
            # the parent of the config dir recorded by load_config.
            saved = cfg.get("report", {}).get("smtp_env_file")
            if saved and not saved.startswith("/"):
                repo_root = pathlib.Path(cfg.get("_cfg_dir", ".")).parent
                cfg.setdefault("report", {})["smtp_env_file"] = \
                    str(repo_root / saved)
            outcome.update(send_report(cfg, text, day))
        except ConfigError as e:
            outcome.update({"sent": False, "reason": f"config: {e}"})
        except (smtplib.SMTPException, OSError, socket.timeout) as e:
            outcome.update({"sent": False, "reason": f"smtp: {str(e)[:150]}"})
        conn.execute(
            "INSERT INTO run_log(action, target, result, note) "
            "VALUES('report', ?, ?, ?)",
            (day, "sent" if outcome.get("sent") else "skipped/failed",
             json.dumps({k: v for k, v in outcome.items() if k != "built"},
                        ensure_ascii=False)),
        )
        conn.commit()
    outcome["text"] = text
    return outcome
