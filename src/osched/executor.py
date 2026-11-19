"""Execution layer (DESIGN.md §7 + §7.1).

Consumes due plan rows (scheduler.due_today) and executes by channel:

- wave1 form channel: HTTP POST to the discovered submit endpoint. The form
  is re-parsed from the live page at execution time (pre-submit freshness
  check per §4c) — the executor never trusts a stale field mapping.
  Captcha routing: `none` submits directly; browser-class captchas go to the
  CUA channel (state='cua'); unknown goes to the human queue.
- wave2 email channel: SMTP with the per-project sender identity (project
  YAML `sender` block) falling back to the shared ops mailbox from
  config/secrets/smtp.env. Missing recipient email → human queue.

Failure handling (§7): retries <= 3 for 5xx/network only; 4xx is never
retried; exhausted retries and bot-blocked responses go to the human queue
and appear in the daily report.

中文：执行层——到期计划按通道执行；表单先取实时字段再 POST；验证码按
分级路由（none 直投 / 浏览器类进 CUA / unknown 人工）；仅 5xx 重试 ≤3。
"""
from __future__ import annotations

import json
import re
import smtplib
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from . import probe as probe_mod
from .config import ConfigError, require_env_key
from .matching import landing_url
from .report import _require_any
from .scheduler import due_today

# Captcha tiers (§7.1): what the HTTP executor may do itself.
CUA_CAPTCHAS = {"recaptcha_v2", "recaptcha_v3", "turnstile", "hcaptcha", "image"}
RETRYABLE_HTTP = set(range(500, 600))
MAX_RETRIES = 3
_POST_BODY_CAP = 262144        # read at most 256KB of a POST response

# Wave-3 rows never auto-execute: submit_type='unknown' means nobody has
# worked out *how* to submit there yet, and a blind POST to a random page is
# worse than no attempt. They go to the human queue until a how is recorded.
UNTYPED_SUBMIT = {"", "unknown"}

# R-520: outreach mail is human-sent. A project must opt in explicitly with
# `delivery: {email_auto: true}` before the executor touches SMTP.
EMAIL_AUTO_DEFAULT = False


def _email_auto(project: dict) -> bool:
    return bool((project.get("delivery") or {}).get("email_auto",
                                                    EMAIL_AUTO_DEFAULT))


def _cua_primary(project: dict, cfg: dict) -> bool:
    """Project-level opt-in: submit through the real browser by default.

    Evidence from the first real window (2026-11-19): both submittable
    static targets silently rejected the POST (one re-rendered form, one
    JS-populated hidden field + AJAX submit). Static form POST only works
    on server-rendered forms; a project that declares cua_primary sends
    form plans to the CUA channel instead.
    """
    d = (project.get("delivery") or {})
    if "cua_primary" in d:
        return bool(d["cua_primary"])
    return bool((cfg.get("delivery") or {}).get("cua_primary", False))

USER_AGENT = probe_mod.USER_AGENT

# Built-in generic templates (fallback when data_root/templates/<id>.txt is
# absent). First line of an email template = subject.
GENERIC_TEMPLATES: dict[str, str] = {
    "dir-submit": (
        "url={{url}}\ntitle={{title}}\ndescription={{description}}\n"
        "email={{email}}\nname={{name}}\n"
    ),
    "outreach-mail": (
        "Submission suggestion for your directory\n"
        "\n"
        "Hi,\n"
        "\n"
        "We run {{site_name}} — {{site_summary}}\n"
        "We think it fits your listing under {{category}}:\n"
        "{{landing}}\n"
        "\n"
        "If it looks like a fit, adding it would help your visitors find a\n"
        "relevant resource. Either way, thanks for maintaining the site.\n"
        "\n"
        "Regards,\n{{sender_name}}\n{{site_name}}\n{{landing}}\n"
    ),
}


class ExecutorError(Exception):
    pass


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

def load_template(cfg: dict, project: dict, template_id: str) -> str:
    """Project-local override, then data_root global, then built-in generic."""
    root = cfg.get("_data_root_resolved")
    candidates = []
    if root:
        candidates.append(f"{root}/projects/templates/{template_id}.txt")
        candidates.append(f"{root}/templates/{template_id}.txt")
    for path in candidates:
        try:
            text = open(path, encoding="utf-8").read()
            if text.strip():
                return text
        except OSError:
            continue
    return GENERIC_TEMPLATES.get(template_id, "")


def render(template: str, ctx: dict) -> str:
    """Replace {{key}} placeholders; unknown keys render as empty."""
    def sub(m: re.Match) -> str:
        return str(ctx.get(m.group(1), ""))
    return re.sub(r"\{\{(\w+)\}\}", sub, template)


def build_context(conn, project: dict, row) -> dict:
    """Submission context from project assets + pool row (client-intake §5a).

    site_summary falls back to the site-summary asset (§5: no human input ->
    site-summary), else a neutral one-liner built from the domain.
    """
    assets = conn.execute(
        "SELECT kind, content FROM project_asset WHERE project_id=?",
        (project["_id"],),
    ).fetchall()
    amap = {a["kind"]: a["content"] for a in assets}
    landing = landing_url(project, None)
    summary = amap.get("site-summary") or (
        f"a curated resource at {project['domain']}")
    return {
        "url": landing,
        "landing": landing,
        "title": amap.get("site-title") or project["domain"],
        "site_name": amap.get("site-title") or project["domain"],
        "site_summary": summary,
        "description": summary,
        "category": amap.get("category") or "general",
        "email": amap.get("contact-email") or "",
        "name": amap.get("sender-name") or project["domain"],
        "sender_name": amap.get("sender-name") or project["domain"],
        "target_domain": row["pool_domain"],
    }


# --------------------------------------------------------------------------
# wave1: HTTP form submit
# --------------------------------------------------------------------------

def _post_form(entry_url: str, fields: list[str], ctx: dict,
               timeout: float, prefill: dict[str, str] | None = None
               ) -> tuple[int, str, str]:
    """POST urlencoded values for the discovered field names.

    Only fields we can fill are sent; unknown honeypot-style names are left
    empty. Hidden state fields (viewstate, CSRF, nonce) are posted back
    exactly as the page shipped them. Redirect chains are followed, so the
    returned body is the landing page (where success/error copy lives).
    Returns (http_status, response_body, err_note); status 0 = network error.
    """
    prefill = prefill or {}
    values: list[tuple[str, str]] = []
    for name in fields:
        low = name.lower()
        if name in prefill:                 # hidden state: echo verbatim
            values.append((name, prefill[name]))
        elif any(k in low for k in ("url", "link", "site")) and "siti" not in low:
            values.append((name, ctx["url"]))
        elif any(k in low for k in ("title", "name", "author")):
            values.append((name, ctx["title"]))
        elif any(k in low for k in ("desc", "summary", "text", "comment", "message")):
            values.append((name, ctx["description"][:500]))
        elif "mail" in low or "email" in low:
            values.append((name, ctx["email"] or "ops@" + ctx["url"].split("//")[-1].split("/")[0]))
        else:
            values.append((name, ""))  # unknown/hidden field: leave empty
    data = urllib.parse.urlencode(values).encode()
    req = urllib.request.Request(
        entry_url, data=data, method="POST",
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": entry_url},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                body = resp.read(_POST_BODY_CAP).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — body is best-effort evidence
                body = ""
            return resp.status, body, ""
    except urllib.error.HTTPError as e:
        return e.code, "", ""
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
        return 0, "", str(e)[:120]


def _verdict_2xx(status: int, body: str, post_url: str) -> dict:
    """Interpret a 2xx POST response. HTTP 200 alone proves nothing: form
    plugins re-render the same page with an inline error at 200."""
    sig = probe_mod.copy_verdict(body)
    if sig["already"]:
        return {"state": "submitted", "http": status,
                "note": f"already listed: {sig['already']}"}
    if sig["success"]:
        return {"state": "submitted", "http": status,
                "note": f"form accepted ({sig['success']})"}
    if sig["failure"]:
        return {"state": "human", "http": status,
                "note": f"2xx but failure copy: {sig['failure']} -> human queue"}
    # No copy signals at all: a re-rendered submission form is the classic
    # silent rejection (validation failed, same page redrawn).
    ok, fields, _ = probe_mod.find_submit_form(body)
    if ok and fields:
        return {"state": "human", "http": status,
                "note": "2xx but submit form re-rendered (likely validation "
                        "failure) -> human queue"}
    return {"state": "submitted", "http": status,
            "note": "form accepted (2xx, no copy signals)"}


def _parse_live_form(entry_url: str, timeout: float):
    """Fetch the entry page and parse the first submit-like form.

    Pre-submit freshness check (§4c): returns (post_url, fields, captcha,
    prefill). Falls back to the cached entry_url when redirects move the form.
    """
    status, final_url, html = probe_mod._fetch(entry_url, timeout)
    if status in (403, 412, 429):
        return None, [], "bot-blocked", {}
    if status != 200 or not html:
        return None, [], "unreachable", {}
    ok, fields, action = probe_mod.find_submit_form(html)
    if not ok:
        return final_url, [], "none", {}
    # The form's own action is the real endpoint; posting to the page URL
    # silently misses handlers mounted elsewhere (or rewrites them as GET).
    post_url = (probe_mod._absolutize(action, final_url)
                if action else final_url) or final_url
    return (post_url, fields, probe_mod._detect_captcha(html),
            probe_mod.state_fields(html))


def exec_form(conn, cfg: dict, project: dict, row, ctx: dict,
              dry_run: bool) -> dict:
    """Execute one wave1 form plan row. Returns the outcome dict."""
    pcfg = cfg.get("probe", {})
    timeout = float(pcfg.get("timeout_seconds", 12.0))
    entry = row["entry_url"] or row["url"]

    live_entry, fields, captcha, prefill = _parse_live_form(entry, timeout)
    cua_primary = _cua_primary(project, cfg)
    if captcha == "bot-blocked":
        return {"state": "human", "note": "bot-blocked at pre-submit check"}
    if captcha == "unreachable":
        return {"state": "failed", "note": "entry page unreachable",
                "retries": MAX_RETRIES}

    # No form on the live page = nothing for any channel to submit. Checked
    # before captcha routing: a stale cached captcha must not send a browser
    # to a page that has no submission form at all.
    if not fields:
        if cua_primary:
            # JS-rendered forms are invisible to the static parser but real
            # in a browser — exactly the failure class seen in window 1.
            return {"state": "cua",
                    "note": "cua_primary: no static form fields "
                            "(likely JS-rendered) -> browser"}
        return {"state": "human",
                "note": "no usable form fields at execution time"}

    # Captcha tier routing (§7.1) — use the live reading, cache as backstop.
    cap = captcha if captcha != "none" else (row["captcha_type"] or "none")
    if cap in CUA_CAPTCHAS:
        return {"state": "cua", "note": f"captcha={cap} -> CUA channel"}
    if cap not in ("none",):
        return {"state": "human", "note": f"captcha={cap} unknown -> human queue"}

    if dry_run:
        state_n = sum(1 for f in fields if f in prefill)
        if cua_primary:
            note = f"would submit {len(fields)} fields via CUA browser at {live_entry}"
        else:
            note = f"would POST {len(fields)} fields to {live_entry}"
        if state_n:
            note += f" ({state_n} state field(s) echoed back)"
        return {"state": "dry-run", "note": note, "fields": fields}

    if cua_primary:
        return {"state": "cua",
                "note": "cua_primary -> browser submission (static POST "
                        "silently rejected in window 1)"}

    status, body, err = _post_form(live_entry, fields, ctx, timeout, prefill)
    if status in (200, 201, 202, 301, 302, 303, 307, 308):
        return _verdict_2xx(status, body, live_entry)
    if status in RETRYABLE_HTTP:
        return {"state": "failed", "http": status,
                "note": f"server error {status}", "retries": MAX_RETRIES}
    if status in (403, 412, 429):
        return {"state": "human", "http": status,
                "note": "bot-blocked on POST -> human queue"}
    return {"state": "failed", "http": status,
            "note": err or f"http {status} (4xx, not retried)"}


# --------------------------------------------------------------------------
# wave2: SMTP email
# --------------------------------------------------------------------------

def _sender_identity(cfg: dict, project: dict) -> dict:
    """Per-project sender block, else the shared ops mailbox (§7)."""
    ident = dict(project.get("sender") or {})
    smtp_cfg = cfg.get("smtp", {})
    if not ident.get("host"):
        ident["host"] = smtp_cfg.get("host", "")
        ident["port"] = smtp_cfg.get("port", 587)
    if not ident.get("env_file"):
        ident["env_file"] = smtp_cfg.get(
            "env_file", "config/secrets/smtp.env")
    return ident


# Outreach quality gate (2026-11-19 review): the first two wave2 drafts were
# sent to a news daily and a study-abroad agency — neither is a directory,
# and the template still had `category=general` + a domain as sender name.
# Sending those burns the sending domain's reputation. A plan must clear
# all three checks before SMTP is touched.
PITCH_TARGET_TYPES = {"pitch", "email", "outreach"}
PLACEHOLDER_CATEGORIES = {"", "general", "other", "unknown", "misc"}


def mail_quality_gate(row, ctx: dict, body: str) -> str:
    """Return a rejection reason; '' means safe to send."""
    keys = row.keys() if hasattr(row, "keys") else []
    st = ((row["submit_type"] if "submit_type" in keys else "") or "").strip().lower()
    if st not in PITCH_TARGET_TYPES:
        return f"target submit_type={st or 'empty'} is not a pitch/outreach prospect"
    cat = (ctx.get("category") or "").strip().lower()
    if cat in PLACEHOLDER_CATEGORIES:
        return f"category={cat or 'empty'} is a placeholder — not personalised"
    sender = (ctx.get("sender_name") or "").strip()
    if "." in sender and "@" not in sender:
        return "sender_name is a domain, not a person — would look automated"
    if "{{" in body:
        return "unrendered template placeholders remain"
    return ""


def exec_email(conn, cfg: dict, project: dict, row, ctx: dict,
               dry_run: bool) -> dict:
    """Execute one wave2 email plan row. Returns the outcome dict."""
    recipient = (row["contact_email"] or "").strip()
    if not recipient:
        return {"state": "human", "note": "no contact email on pool row"}

    ident = _sender_identity(cfg, project)
    if not ident.get("host"):
        return {"state": "human", "note": "no SMTP host configured"}

    body = render(load_template(cfg, project, row["template_id"] or
                                "outreach-mail"), ctx)
    if not body.strip():
        return {"state": "human", "note": "empty template"}
    subject, _, content = body.partition("\n")
    sender_user = require_env_key(ident.get("env_file"), "SMTP_USER")
    # Gmail app-password convention, same env file as the daily report.
    sender_pass = _require_any(ident.get("env_file"),
                               "SMTP_APP_PASSWORD", "SMTP_PASS")
    from_addr = ident.get("from") or sender_user

    gate = mail_quality_gate(row, ctx, body)
    if gate:
        return {"state": "human",
                "note": f"mail quality gate: {gate} -> human (no SMTP touched)"}

    if dry_run:
        return {"state": "dry-run",
                "note": f"would email {recipient} via {ident['host']}",
                "subject": subject.strip()}

    msg = MIMEText(content.strip(), "plain", "utf-8")
    msg["Subject"] = Header(subject.strip() or "Listing suggestion", "utf-8")
    msg["From"] = formataddr((str(Header(ctx["sender_name"], "utf-8")),
                              from_addr))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=ctx["url"].split("//")[-1]
                                   .split("/")[0])

    try:
        port = int(ident.get("port", 587))
        if port == 465:
            smtp = smtplib.SMTP_SSL(ident["host"], port, timeout=30)
        else:
            smtp = smtplib.SMTP(ident["host"], port, timeout=30)
        with smtp:
            if port != 465:
                smtp.starttls()
            smtp.login(sender_user, sender_pass)
            smtp.sendmail(from_addr, [recipient], msg.as_string())
        return {"state": "sent", "note": f"sent to {recipient}"}
    except (smtplib.SMTPException, OSError, socket.timeout) as e:
        return {"state": "failed", "note": f"smtp: {str(e)[:150]}",
                "retries": MAX_RETRIES}


# --------------------------------------------------------------------------
# Plan row driver
# --------------------------------------------------------------------------

def _finish(conn, plan_id: int, outcome: dict, *, dry_run: bool) -> None:
    if dry_run:
        return  # never mutate state on a dry run
    # Keep "state" in the dict: the caller logs it right after this call.
    state = outcome["state"]
    note = {k: v for k, v in outcome.items() if k != "state"}
    conn.execute(
        """
        UPDATE plan SET state=?, executed_at=datetime('now'),
            captcha_solved=CASE WHEN ?='submitted' THEN 0 ELSE captcha_solved END,
            result_note=?
        WHERE id=?
        """,
        (state, state, json.dumps(note, ensure_ascii=False), plan_id),
    )


def run_execution(conn, cfg: dict, project: dict, *, run_date: str | None = None,
                  dry_run: bool = False, limit: int = 0) -> dict:
    """Execute all due plan rows for one project (§7)."""
    rows = due_today(conn, project["_id"], run_date)
    if limit:
        rows = rows[:limit]

    stats = {"due": len(rows), "submitted": 0, "sent": 0, "cua": 0,
             "human": 0, "failed": 0, "dry_run": 0, "skipped_channel": 0}
    details: list[dict] = []
    channels = set(project.get("channels", ["form", "email"]))

    for row in rows:
        channel = row["channel"]
        if channel not in channels:
            stats["skipped_channel"] += 1
            continue
        ctx = build_context(conn, project, row)
        try:
            if channel == "form" and (row["submit_type"] or "") in UNTYPED_SUBMIT:
                outcome = {"state": "human",
                           "note": "submit_type unknown — needs how (wave3)"}
            elif channel == "email" and not _email_auto(project):
                outcome = {"state": "human",
                           "note": "email channel is manual (R-520) — "
                                   "queued for human send"}
            elif channel == "form":
                outcome = exec_form(conn, cfg, project, row, ctx, dry_run)
            elif channel == "email":
                outcome = exec_email(conn, cfg, project, row, ctx, dry_run)
            else:
                outcome = {"state": "human",
                           "note": f"unknown channel '{channel}'"}
        except ConfigError as e:
            outcome = {"state": "human", "note": f"config: {e}"}
        except Exception as e:  # executor must never crash the run
            outcome = {"state": "failed", "note": f"error: {str(e)[:150]}"}

        if dry_run:
            outcome["state"] = "dry-run"
            stats["dry_run"] += 1
        else:
            stats[outcome["state"]] = stats.get(outcome["state"], 0) + 1
        _finish(conn, row["id"], outcome, dry_run=dry_run)
        conn.execute(
            "INSERT INTO run_log(action, target, result, note) "
            "VALUES('execute', ?, ?, ?)",
            (f"{row['channel']}:{row['pool_domain']}",
             outcome["state"],
             json.dumps({k: v for k, v in outcome.items() if k != "state"},
                        ensure_ascii=False)),
        )
        details.append({"plan_id": row["id"], "channel": channel,
                        "domain": row["pool_domain"], "state": outcome["state"],
                        "note": outcome.get("note", "")})
    if not dry_run:
        conn.commit()
    stats["rows"] = details
    return stats
