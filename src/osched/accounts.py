"""Directory account lifecycle (business.md B3).

Register-first directories need an account before submission. This module
closes the loop on the registration-role mailbox:

  - account ledger: per (project, directory) credentials, status lifecycle
    (pending-activation -> active -> expired/banned); stored ONLY in the
    gitignored data_root sqlite, masked in every output
  - verification-mail fetch (IMAP, stdlib imaplib): scan the mailbox for
    recent directory mails, extract activation links and codes, match them
    to ledger rows by sender domain, flip pending-activation -> active

Fail-open: missing IMAP credentials or an unreachable mailbox never raises
— returns a reason, logs to run_log, ready for the next cron pass.

中文：目录账号生命周期（B3）。台账只存 gitignored 数据库；IMAP 收验证信
提取激活链接/验证码并按发件域匹配台账；凭据缺失/邮箱不可达 fail-open。
"""
from __future__ import annotations

import email
import imaplib
import json
import re
from datetime import date, datetime, timedelta
from email.header import decode_header

from .config import ConfigError, require_env_key

ACT_LINK_RE = re.compile(
    r'href=["\']?(https?://[^"\'>\s]+(?:activat|verify|confirm|validat|'
    r'register|token=|code=)[^"\'>\s]*)', re.I)
# links that are opt-out mechanics, not account activation
NOISE_LINK_RE = re.compile(
    r'(unsubscri|optout|opt-out|opt_out|removeme|email_optout)', re.I)
# real codes: digits, or hex-ish tokens right after a code keyword
CODE_RE = re.compile(
    r'(?:code|验证码|confirmation)[:\s#"\']{0,3}([0-9]{4,8}|[0-9a-f]{8,16})\b')


def _mask(secret: str | None) -> str:
    if not secret:
        return ""
    return secret[:2] + "…" + secret[-2:] if len(secret) > 6 else "…"


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def add_account(conn, project_id: int, dir_domain: str, email_addr: str,
                username: str = "", password: str = "",
                status: str = "pending-activation", note: str = "") -> int:
    conn.execute(
        """INSERT INTO account(project_id, dir_domain, email, username,
             password, status, note)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(project_id, dir_domain, email) DO UPDATE SET
             username=excluded.username, password=excluded.password,
             status=excluded.status, note=excluded.note""",
        (project_id, dir_domain, email_addr, username, password, status, note),
    )
    row = conn.execute(
        "SELECT id FROM account WHERE project_id=? AND dir_domain=? AND "
        "email=?", (project_id, dir_domain, email_addr),
    ).fetchone()
    conn.commit()
    return row["id"]


def list_accounts(conn, project_id: int | None = None) -> list[dict]:
    sql = ("SELECT a.*, pr.domain AS project FROM account a "
           "JOIN project pr ON pr.id=a.project_id")
    args: list = []
    if project_id:
        sql += " WHERE a.project_id=?"
        args.append(project_id)
    sql += " ORDER BY pr.domain, a.dir_domain"
    out = []
    for r in conn.execute(sql, args).fetchall():
        out.append({
            "project": r["project"], "dir_domain": r["dir_domain"],
            "email": r["email"], "username": r["username"] or "",
            "password": _mask(r["password"]), "status": r["status"],
            "registered_at": r["registered_at"] or "",
            "last_used_at": r["last_used_at"] or "",
        })
    return out


# --------------------------------------------------------------------------
# IMAP verification-mail fetch
# --------------------------------------------------------------------------

def _imap_settings(cfg: dict) -> dict:
    env_file = cfg.get("report", {}).get("smtp_env_file",
                                         "config/secrets/smtp.env")
    return {
        "env_file": env_file,
        "host": cfg.get("imap", {}).get("host") or
                require_env_key(env_file, "IMAP_HOST"),
        "port": int(cfg.get("imap", {}).get("port")
                    or require_env_key(env_file, "IMAP_PORT") or 993),
        "user": require_env_key(env_file, "SMTP_USER"),
        # same app-password authorizes IMAP on Google accounts
        "pass": require_env_key(env_file, "SMTP_APP_PASSWORD"),
    }


def _decode_subject(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    parts = decode_header(raw)
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", "replace") \
            if isinstance(text, bytes) else text
    return out


def _sender_domain(msg) -> str:
    from_ = msg.get("From", "")
    m = re.search(r"@([A-Za-z0-9.-]+)", from_)
    return (m.group(1).lower().rstrip(".") if m else "")


def _extract_links_and_codes(body: str) -> tuple[list[str], list[str]]:
    links = list(dict.fromkeys(
        m.rstrip(".,;)")
        for m in ACT_LINK_RE.findall(body)
        if not NOISE_LINK_RE.search(m)))
    codes = list(dict.fromkeys(m for m in CODE_RE.findall(body)))
    return links[:5], codes[:3]


def _activate_pending_account(conn, project_id: int, dir_domain: str) -> bool:
    cur = conn.execute(
        "SELECT id FROM account WHERE project_id=? AND dir_domain=? AND "
        "status='pending-activation'", (project_id, dir_domain),
    ).fetchone()
    if not cur:
        return False
    conn.execute(
        "UPDATE account SET status='active', registered_at=coalesce("
        "registered_at, datetime('now')) WHERE id=?", (cur["id"],))
    return True


def fetch_verification_mails(conn, cfg: dict, project: dict, *,
                             days: int = 3, limit: int = 50) -> dict:
    """Scan the registration mailbox; extract activation links/codes."""
    pid = project["_id"]
    outcome: dict = {"project": project["domain"], "scanned": 0,
                     "found": 0, "activated": 0, "items": []}
    try:
        s = _imap_settings(cfg)
    except ConfigError as e:
        outcome["reason"] = f"config: {e}"
        return outcome
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        imap = imaplib.IMAP4_SSL(s["host"], s["port"], timeout=30)
        imap.login(s["user"], s["pass"])
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", since)
        ids = (data[0] or b"").split()[-limit:]
        for mid in ids:
            typ, parts = imap.fetch(mid, "(RFC822)")
            if typ != "OK" or not parts or not parts[0]:
                continue
            msg = email.message_from_bytes(parts[0][1])
            from_raw = str(msg.get("From", ""))
            if s["user"].lower() in from_raw.lower():
                continue  # our own outbound mail (e.g. the daily report)
            body = _mail_body(msg)
            links, codes = _extract_links_and_codes(body)
            sender_dom = _sender_domain(msg)
            outcome["scanned"] += 1
            if links or codes:
                outcome["found"] += 1
                activated = False
                if links and sender_dom:
                    activated = _activate_pending_account(conn, pid,
                                                          sender_dom)
                    if activated:
                        outcome["activated"] += 1
                outcome["items"].append({
                    "from": sender_dom,
                    "subject": _decode_subject(msg.get("Subject"))[:60],
                    "links": links, "codes": codes, "activated": activated,
                })
        imap.logout()
        if outcome["activated"]:
            conn.commit()
    except (imaplib.IMAP4.error, OSError) as e:
        outcome["reason"] = f"imap: {str(e)[:150]}"
        return outcome
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('mail-verify', ?, ?, ?)",
        (project["domain"],
         "ok" if "reason" not in outcome else "skipped/failed",
         json.dumps({k: v for k, v in outcome.items() if k != "items"},
                    ensure_ascii=False)),
    )
    conn.commit()
    return outcome


def _mail_body(msg) -> str:
    """Best-effort text extraction: text parts, else HTML-stripped."""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    chunks.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"))
                except (AttributeError, LookupError):
                    pass
    else:
        try:
            chunks.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace"))
        except (AttributeError, LookupError):
            pass
    text = "\n".join(chunks)
    # strip html tags so href= regex still works (it reads the raw attrs)
    return text
