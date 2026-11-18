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
import time
from datetime import date, datetime, timedelta
from email.header import decode_header

from .config import ConfigError, require_env_key

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # playwright is optional until an activation run
    sync_playwright = None

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


def _activate_pending_account(conn, project_id: int, dir_domain: str,
                              links: list[str]) -> bool:
    """Flip a pending account to active; backfill the activation link."""
    cur = conn.execute(
        "SELECT id, activation_url FROM account WHERE project_id=? AND "
        "dir_domain=? AND status='pending-activation'",
        (project_id, dir_domain),
    ).fetchone()
    if not cur:
        return False
    conn.execute(
        """UPDATE account SET status='active',
             registered_at=coalesce(registered_at, datetime('now')),
             activation_url=coalesce(activation_url, ?)
           WHERE id=?""",
        (links[0] if links else None, cur["id"]))
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
                                                          sender_dom, links)
                    if activated:
                        outcome["activated"] += 1
                    else:
                        # account exists (or not yet) — keep the freshest
                        # activation link on the ledger row for the CUA run
                        conn.execute(
                            """UPDATE account SET activation_url=?
                               WHERE project_id=? AND dir_domain=?
                                 AND activation_url IS NULL""",
                            (links[0], pid, sender_dom))
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


# --------------------------------------------------------------------------
# CUA activation: click the activation link in the real browser (B3 phase 2)
# --------------------------------------------------------------------------

# Words that mark a successful activation/confirmation page.
ACT_HINTS = ("activated", "confirmed", "verified", "welcome", "success",
             "thank", "account is ready", "email address has been",
             "激活成功", "验证成功", "已确认")


def pending_activations(conn, project_id: int, limit: int = 0) -> list:
    rows = conn.execute(
        """SELECT id, dir_domain, email, activation_url FROM account
           WHERE project_id=? AND status='pending-activation'
             AND activation_url IS NOT NULL
           ORDER BY registered_at DESC, id DESC""",
        (project_id,),
    ).fetchall()
    return rows[:limit] if limit else rows


def _activation_looks_successful(page) -> bool:
    try:
        body = page.inner_text("body", timeout=5000).lower()[:4000]
        return any(h in body for h in ACT_HINTS)
    except Exception:
        return False


def _activate_one(page, row) -> dict:
    started = time.time()
    page.goto(row["activation_url"], timeout=30000,
              wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    ok = _activation_looks_successful(page)
    return {"state": "active" if ok else "pending-activation",
            "note": ("activation page confirmed" if ok
                     else "no activation marker; keeping pending, "
                          "escalate to human if repeated"),
            "secs": round(time.time() - started, 1)}


def run_activation(conn, project: dict, *, headless: bool = False,
                   limit: int = 0) -> dict:
    """Visit pending accounts' activation links in real Chrome (serial)."""
    if sync_playwright is None:
        raise RuntimeError(
            "playwright not installed — run: pip install playwright")
    from .cua import _launch

    rows = pending_activations(conn, project["_id"], limit)
    stats = {"due": len(rows), "activated": 0, "pending": 0, "rows": []}
    if not rows:
        return stats
    with sync_playwright() as pw:
        browser = _launch(pw, headless)
        page = browser.new_page()
        try:
            for row in rows:
                try:
                    outcome = _activate_one(page, row)
                except Exception as e:  # noqa: BLE001 — one site never stops the run
                    outcome = {"state": "pending-activation",
                               "note": f"error: {str(e)[:120]}"}
                if outcome["state"] == "active":
                    stats["activated"] += 1
                    conn.execute(
                        "UPDATE account SET status='active', "
                        "registered_at=coalesce(registered_at, "
                        "datetime('now')) WHERE id=?", (row["id"],))
                else:
                    stats["pending"] += 1
                conn.execute(
                    "INSERT INTO run_log(action, target, result, note) "
                    "VALUES('account-activate', ?, ?, ?)",
                    (f"{row['dir_domain']} account#{row['id']}",
                     outcome["state"],
                     json.dumps({k: v for k, v in outcome.items()
                                 if k != "state"}, ensure_ascii=False)))
                stats["rows"].append({"account_id": row["id"],
                                      "domain": row["dir_domain"],
                                      "state": outcome["state"],
                                      "note": outcome["note"]})
        finally:
            browser.close()
    conn.commit()
    return stats
