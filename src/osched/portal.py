"""Client portal (business docs client-portal.md §1–§2).

Stdlib-only HTTP server, single process, shares the scheduler's sqlite
in read-only fashion for delivery-side tables. Portal-owned writes are
exactly: client identity, sessions, project requests (docs §4 write-path
discipline). OAuth (Google/Facebook) uses the authorization-code flow on
the backend with a one-time handoff code to the SPA — session tokens
never appear in URLs.

Routes:
  GET  /                login/register page (EN default)
  POST /register        email+password -> verification mail
  GET  /verify?token=   mark verified, auto-login -> /dashboard
  POST /login           session cookie
  GET  /dashboard       View A quota + View B effect summary (read-only)
  POST /logout

中文：客户端门户骨架；自有表仅身份/会话/项目请求，投递侧表只读。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import secrets
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .db import connect as db_connect
from . import evidence as evidence_mod
from .guard import (DENY_TEXT, TRIAL_CREDITS, canonical_email, client_ip,
                    draft_quota_message, record_trial, registrable_domain,
                    signup_blocked, trial_verdict)
from .report import privacy_url, send_mail

SESSION_DAYS = 14
VERIFY_HOURS = 24

# ratified pricing (docs/business.md §Pricing) — per live link, monthly packs
PACKAGE_TIERS = {
    "starter": 80,   # 1–9 links/month
    "growth": 65,    # 10–49 links/month
    "scale": 50,     # 50+ links/month
}

PORTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS client (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,          -- scrypt$salt_hex$hash_hex
  verified      INTEGER NOT NULL DEFAULT 0,
  role          TEXT NOT NULL DEFAULT 'client',
                -- client | admin (admin = whitelisted super user)
  oauth_provider TEXT,                  -- google | facebook | NULL (stub phase)
  oauth_sub     TEXT,
  verify_token  TEXT,
  verify_expires TEXT,
  signup_ip     TEXT,                   -- abuse ledger: registration source
  last_ip       TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS client_session (
  token      TEXT PRIMARY KEY,
  client_id  INTEGER NOT NULL REFERENCES client(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_request (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id     INTEGER NOT NULL REFERENCES client(id),
  domain        TEXT NOT NULL,
  site_title    TEXT,
  site_summary  TEXT,
  contact_email TEXT,
  tags_json     TEXT NOT NULL DEFAULT '[]',
  verify_method TEXT,                   -- dns_txt | meta
  verify_token  TEXT,
  verify_state  TEXT NOT NULL DEFAULT 'draft',
                -- draft | verified | active | rejected
  project_id    INTEGER,                -- linked after activation
  quota_type    TEXT NOT NULL DEFAULT 'trial',
  quota_total   INTEGER NOT NULL DEFAULT 3,
  quota_used    INTEGER NOT NULL DEFAULT 0,
  trial_until   TEXT,                   -- trial window expiry (activated +14d)
  trial_note    TEXT,                   -- granted | deny reason (guard.py)
  trial_checked_at TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(client_id, domain)
);

CREATE TABLE IF NOT EXISTS admin_whitelist (
  email    TEXT PRIMARY KEY,
  note     TEXT,
  added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_handoff (
  code          TEXT PRIMARY KEY,       -- one-time code handed to the SPA
  session_token TEXT NOT NULL REFERENCES client_session(token),
  expires_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_state (
  state      TEXT PRIMARY KEY,          -- CSRF state, server-side single use
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS package_request (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id      INTEGER NOT NULL REFERENCES client(id),
  tier           TEXT NOT NULL,          -- starter | growth | scale
  links_planned  INTEGER,
  price_per_link INTEGER NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending',
                 -- pending | done | cancelled
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS data_deletion (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  provider         TEXT NOT NULL,        -- facebook
  provider_user_id TEXT NOT NULL,
  client_id        INTEGER,              -- matched client, NULL if none
  confirmation_code TEXT NOT NULL,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Free-trial abuse ledger: one row per granted trial. The three signal
-- columns are why a second grant is refused (see guard.py).
CREATE TABLE IF NOT EXISTS trial_ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id   INTEGER NOT NULL,
  request_id  INTEGER,
  project_id  INTEGER,
  email_canon TEXT,                     -- alias-collapsed mailbox
  reg_domain  TEXT,                     -- eTLD+1 of the verified site
  ip          TEXT,                     -- client IP at activation
  granted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trial_ledger_email ON trial_ledger(email_canon);
CREATE INDEX IF NOT EXISTS idx_trial_ledger_site  ON trial_ledger(reg_domain);
CREATE INDEX IF NOT EXISTS idx_trial_ledger_ip    ON trial_ledger(ip);
"""

def _migrate_portal(conn: sqlite3.Connection) -> None:
    """Additive portal migrations (idempotent)."""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(client)").fetchall()}
    if "role" not in cols:
        conn.execute("ALTER TABLE client ADD COLUMN role TEXT "
                     "NOT NULL DEFAULT 'client'")
    if "signup_ip" not in cols:
        conn.execute("ALTER TABLE client ADD COLUMN signup_ip TEXT")
    if "last_ip" not in cols:
        conn.execute("ALTER TABLE client ADD COLUMN last_ip TEXT")
    req_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(project_request)").fetchall()}
    if "trial_until" not in req_cols:
        conn.execute("ALTER TABLE project_request ADD COLUMN "
                     "trial_until TEXT")
    if "trial_note" not in req_cols:
        # why the free credits were withheld ("granted" | deny reason)
        conn.execute("ALTER TABLE project_request ADD COLUMN "
                     "trial_note TEXT")
    if "trial_checked_at" not in req_cols:
        conn.execute("ALTER TABLE project_request ADD COLUMN "
                     "trial_checked_at TEXT")


def seed_admin_whitelist(conn: sqlite3.Connection, cfg: dict) -> None:
    """Whitelist sources: config portal.admin_emails + env override.

    Whitelisted emails are privileges, not registration gates: anyone
    can register, but only whitelist members carry role='admin'.
    """
    emails = set(cfg.get("portal", {}).get("admin_emails") or [])
    env_val = os.environ.get("PORTAL_ADMIN_EMAILS", "")
    emails |= {e.strip().lower() for e in env_val.split(",") if e.strip()}
    for email in emails:
        conn.execute("INSERT OR IGNORE INTO admin_whitelist(email, note) "
                     "VALUES(?, 'seeded')", (email,))
    conn.execute("""UPDATE client SET role='admin' WHERE email IN
                    (SELECT email FROM admin_whitelist)""")
    conn.execute("""UPDATE client SET role='client' WHERE role='admin'
                    AND email NOT IN (SELECT email FROM admin_whitelist)""")
    conn.commit()

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — client portal</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --text:#1a1a1a; --muted:#6b6b6b;
  --border:#dcdcd8; --accent:#2563eb; --code:#f1f1ee; }}
body {{ margin:0; background:var(--bg); color:var(--text); line-height:1.6;
  font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif; }}
header {{ background:var(--panel); border-bottom:1px solid var(--border);
  padding:12px 20px; display:flex; gap:16px; align-items:center; }}
header b {{ letter-spacing:.2px; }}
main {{ max-width:860px; margin:0 auto; padding:28px 18px 80px; }}
.card {{ background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:22px 26px; margin:14px 0; }}
textarea {{ width:100%; }}
input,button {{ font:inherit; padding:8px 12px; border-radius:6px;
  border:1px solid var(--border); }}
button {{ background:var(--accent); color:#fff; border:none; cursor:pointer; }}
button[disabled] {{ background:var(--code); color:var(--muted); }}
table {{ border-collapse:collapse; width:100%; font-size:.95em; }}
th,td {{ border:1px solid var(--border); padding:6px 10px; text-align:left; }}
th {{ background:var(--code); }}
.muted {{ color:var(--muted); font-size:.9em; }}
.ok {{ color:#15803d; }} .warn {{ color:#b45309; }}
</style></head><body>
<header><b>client portal</b><span class="muted">outreach-scheduler</span>
<span style="flex:1"></span>{nav}</header>
<main>{body}</main></body></html>"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


# --------------------------------------------------------------------------
# passwords / sessions / tokens
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, h_hex = stored.split("$")
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                           n=2 ** 14, r=8, p=1)
        return secrets.compare_digest(h.hex(), h_hex)
    except (ValueError, TypeError):
        return False


def _new_session(conn, client_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    conn.execute(
        "INSERT INTO client_session(token, client_id, expires_at) "
        "VALUES(?,?,?)", (token, client_id, exp))
    conn.commit()
    return token


def _client_from_session(conn, token: str | None):
    if not token:
        return None
    row = conn.execute(
        """SELECT c.* FROM client_session s JOIN client c ON c.id=s.client_id
           WHERE s.token=? AND s.expires_at > datetime('now')""",
        (token,)).fetchone()
    return row


# --------------------------------------------------------------------------
# OAuth (Google / Facebook) — authorization-code flow, stdlib only.
# The callback lives on the backend (PORTAL_BASE_URL); the SPA receives a
# one-time handoff code via URL fragment, exchanged through the JSON API,
# so the session token never appears in a URL.
# --------------------------------------------------------------------------

OAUTH_STATE_COOKIE = "osched_ostate"
OAUTH_STATE_MAX_AGE = 600          # 10 minutes
HANDOFF_TTL_MINUTES = 5

OAUTH_ENDPOINTS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
    },
    "facebook": {
        "authorize": "https://www.facebook.com/v19.0/dialog/oauth",
        "token": ("https://graph.facebook.com/v19.0/oauth/access_token"
                  "?client_id={cid}&redirect_uri={ruri}&code={code}"),
        "scope": "email,public_profile",
    },
}


def _oauth_cfg(cfg: dict, provider: str) -> tuple[str, str]:
    """(client_id, client_secret) from env or config portal.oauth.<p>."""
    key = {"google": "OAUTH_GOOGLE", "facebook": "OAUTH_FACEBOOK"}[provider]
    cid = os.environ.get(f"{key}_CLIENT_ID") or os.environ.get(
        f"{key}_APP_ID") or ""
    csec = os.environ.get(f"{key}_CLIENT_SECRET") or os.environ.get(
        f"{key}_APP_SECRET") or ""
    block = cfg.get("portal", {}).get("oauth", {}).get(provider) or {}
    cid = cid or block.get("client_id") or block.get("app_id") or ""
    csec = csec or block.get("client_secret") or block.get("app_secret") or ""
    return cid.strip(), csec.strip()


def _oauth_redirect_uri(cfg: dict, provider: str) -> str:
    base = (os.environ.get("PORTAL_BASE_URL")
            or cfg.get("portal", {}).get("base_url")
            or "http://127.0.0.1:8090").rstrip("/")
    return f"{base}/auth/{provider}/callback"


def _http_json(url: str, data: dict | None = None) -> dict:
    """POST form (if data) or GET url, parse JSON. Raises on HTTP errors."""
    if data is not None:
        req = urllib.request.Request(
            url, data=urlencode(data).encode(), method="POST")
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _oauth_exchange_code(cfg: dict, provider: str, code: str,
                         redirect_uri: str) -> dict:
    """Trade the auth code for provider identity {sub, email}."""
    cid, csec = _oauth_cfg(cfg, provider)
    ep = OAUTH_ENDPOINTS[provider]
    if provider == "google":
        tok = _http_json(ep["token"], {
            "code": code, "client_id": cid, "client_secret": csec,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        idt = (tok.get("id_token") or "").split(".")
        if len(idt) < 2:
            raise RuntimeError("token response missing id_token")
        pad = idt[1] + "=" * (-len(idt[1]) % 4)
        claims = json.loads(__import__("base64").urlsafe_b64decode(pad))
        return {"sub": claims.get("sub") or "", "email": claims.get("email")}
    # facebook
    tok_url = ep["token"].format(cid=cid, ruri=redirect_uri, code=code)
    tok = _http_json(tok_url)
    access = tok.get("access_token") or ""
    me = _http_json("https://graph.facebook.com/me"
                    "?fields=id,name,email&access_token=" + access)
    return {"sub": me.get("id") or "", "email": me.get("email")}


def _fb_parse_signed_request(signed: str, app_secret: str) -> dict:
    """Verify and decode a Facebook `signed_request` (HMAC-SHA256)."""
    sig_b64, payload_b64 = signed.split(".", 1)
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    expected = hmac.new(app_secret.encode(), payload_b64.encode(),
                        hashlib.sha256).digest()
    actual = base64.urlsafe_b64decode(pad(sig_b64))
    if not hmac.compare_digest(expected, actual):
        raise RuntimeError("signed_request signature mismatch")
    return json.loads(base64.urlsafe_b64decode(pad(payload_b64)))


def _oauth_upsert_client(conn, provider: str, sub: str, email: str,
                         ip: str = ""):
    """Find or create the client for an OAuth identity.

    OAuth emails count as verified (providers verify mailbox ownership).
    Existing clients keep their password; provider identity is linked.
    Whitelist membership still governs the admin role.
    """
    email = (email or "").strip().lower()
    if not email:
        raise RuntimeError(f"{provider} account has no verified email — "
                           "use e-mail registration instead.")
    row = conn.execute("SELECT * FROM client WHERE email=?",
                       (email,)).fetchone()
    if row:
        conn.execute("UPDATE client SET verified=1, oauth_provider=?, "
                     "oauth_sub=?, last_ip=COALESCE(?, last_ip) WHERE id=?",
                     (provider, sub, ip or None, row["id"]))
        conn.commit()
        return row["id"]
    conn.execute(
        """INSERT INTO client(email, password_hash, verified,
           oauth_provider, oauth_sub, role, signup_ip, last_ip)
           VALUES(?, ?, 1, ?, ?,
             CASE WHEN ? IN (SELECT email FROM admin_whitelist)
               THEN 'admin' ELSE 'client' END, ?, ?)""",
        (email, f"oauth!{provider}:{secrets.token_urlsafe(24)}",
         provider, sub, email, ip or None, ip or None))
    conn.commit()
    return conn.execute("SELECT id FROM client WHERE email=?",
                        (email,)).fetchone()["id"]


def _new_handoff(conn, session_token: str) -> str:
    code = secrets.token_urlsafe(24)
    exp = (datetime.utcnow()
           + timedelta(minutes=HANDOFF_TTL_MINUTES)).isoformat()
    conn.execute("DELETE FROM oauth_handoff WHERE expires_at < datetime('now')")
    conn.execute("INSERT INTO oauth_handoff(code, session_token, expires_at) "
                 "VALUES(?,?,?)", (code, session_token, exp))
    conn.commit()
    return code


def _frontend_url(cfg: dict) -> str:
    return (os.environ.get("PORTAL_FRONTEND_URL")
            or cfg.get("portal", {}).get("frontend_url") or "").rstrip("/")


# --------------------------------------------------------------------------
# self-service project creation (client-portal §1.5)
# --------------------------------------------------------------------------

DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
META_RE = re.compile(
    r"<meta[^>]+(?:name|property)\s*=\s*[\"']osched-verify[\"']"
    r"[^>]*>", re.I)
CONTENT_RE = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.I)


def normalize_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].strip(".")
    if d.startswith("www."):
        d = d[4:]
    return d


def validate_domain(domain: str, *, dev: bool) -> str | None:
    """Return an error message, or None when acceptable."""
    if not dev and domain == "localhost":
        return "localhost is not a verifiable promotion domain."
    if not DOMAIN_RE.match(domain) and not (dev and domain == "localhost"):
        return "Not a valid domain (e.g. example.com)."
    return None


def fetch_meta_token(url: str) -> str:
    """Fetch a page and return its osched-verify meta content ('' if none)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "osched-portal-verify/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read(512 * 1024).decode("utf-8", "replace")
    for m in META_RE.finditer(body):
        cm = CONTENT_RE.search(m.group(0))
        if cm:
            return cm.group(1)
    return ""


def _verify_url(domain: str) -> str:
    base = os.environ.get("PORTAL_VERIFY_FETCH_BASE")
    if base:  # dev/test override: fetch the stub server instead
        return base.rstrip("/") + "/"
    return f"https://{domain}/"


def _trial_used(conn, client_id: int) -> bool:
    """True once this account can no longer earn free credits.

    Two sources: the abuse ledger (authoritative) and legacy trial rows
    created before the ledger existed.
    """
    if conn.execute("SELECT 1 FROM trial_ledger WHERE client_id=? LIMIT 1",
                    (client_id,)).fetchone() is not None:
        return True
    return conn.execute(
        """SELECT 1 FROM project_request WHERE client_id=?
           AND quota_type='trial' AND quota_total>0
           AND verify_state IN ('verified','active') LIMIT 1""",
        (client_id,)).fetchone() is not None


def _domain_claimed(conn, domain: str, exclude_client: int) -> bool:
    """Exact-host OR same-site claim by another account.

    eTLD+1 comparison blocks the ``shop.a.com`` / ``a.com`` re-claim trick.
    """
    site = registrable_domain(domain)
    for row in conn.execute(
            """SELECT domain FROM project_request
               WHERE verify_state IN ('verified','active')
                 AND client_id != ?""", (exclude_client,)).fetchall():
        other = row["domain"]
        if other == domain or registrable_domain(other) == site:
            return True
    return False


def activate_request(conn, req, ip: str = "") -> dict:
    """Ownership verified -> link/create the scheduler project, grant trial.

    Credentials are guarded (``guard.trial_verdict``): the project always
    activates, but the 3 free links are withheld when the mailbox, the
    site, or the network already earned a trial. Withholding is recorded
    so an admin can override after review.
    """
    client = conn.execute("SELECT id, email FROM client WHERE id=?",
                          (req["client_id"],)).fetchone()
    email = client["email"] if client else ""
    granted, reason = trial_verdict(conn, req["client_id"], email,
                                    req["domain"], ip)
    credits = TRIAL_CREDITS if granted else 0
    pr = conn.execute("SELECT id FROM project WHERE domain=?",
                      (req["domain"],)).fetchone()
    if pr:
        project_id = pr["id"]
    else:
        conn.execute("INSERT INTO project(domain, tags_json) VALUES(?,?)",
                     (req["domain"], req["tags_json"] or "[]"))
        project_id = conn.execute(
            "SELECT id FROM project WHERE domain=?",
            (req["domain"],)).fetchone()["id"]
    for kind, content in (("site-title", req["site_title"]),
                          ("site-summary", req["site_summary"]),
                          ("contact-email", req["contact_email"])):
        if content:
            conn.execute(
                """INSERT INTO project_asset(project_id, kind, content, source)
                   VALUES(?,?,?,'portal')
                   ON CONFLICT(project_id, kind) DO UPDATE SET
                     content=excluded.content,
                     updated_at=datetime('now')""",
                (project_id, kind, content))
    conn.execute(
        """UPDATE project_request SET verify_state='active', project_id=?,
           quota_type=?, quota_total=?, quota_used=0,
           trial_note=?, trial_checked_at=datetime('now'),
           trial_until=(CASE WHEN ?='granted'
             THEN datetime('now','+14 days') ELSE trial_until END)
           WHERE id=?""",
        (project_id, "trial" if granted else "paid", credits, reason,
         reason, req["id"]))
    conn.commit()
    if granted:
        record_trial(conn, client_id=req["client_id"], request_id=req["id"],
                     project_id=project_id, email=email,
                     domain=req["domain"], ip=ip)
    return {"project_id": project_id, "trial": granted, "credits": credits,
            "reason": reason, "deny_text": DENY_TEXT.get(reason, "")}


# --------------------------------------------------------------------------
# dashboard queries (read-only over scheduler tables)
# --------------------------------------------------------------------------

def _client_projects(conn, client_id: int):
    return conn.execute(
        """
        SELECT pr.id AS project_id, pr.domain, req.quota_type, req.quota_total,
          req.quota_used, req.trial_until, req.trial_note,
          (SELECT COUNT(*) FROM match m WHERE m.project_id=pr.id
             AND m.state='pending') AS pending,
          (SELECT COUNT(*) FROM plan pl WHERE pl.project_id=pr.id
             AND pl.state IN ('submitted','sent')) AS inflight,
          (SELECT COUNT(*) FROM plan pl WHERE pl.project_id=pr.id
             AND pl.state='live') AS live,
          (SELECT COUNT(*) FROM link_verify lv JOIN plan pl2 ON pl2.id=lv.plan_id
             WHERE pl2.project_id=pr.id AND lv.result='live'
               AND lv.checked_at >= date('now','-30 days')) AS live30,
          (SELECT COUNT(*) FROM link_verify lv JOIN plan pl2 ON pl2.id=lv.plan_id
             WHERE pl2.project_id=pr.id AND lv.result='live'
               AND lv.checked_at >= date('now','start of month')) AS live_month
        FROM project_request req JOIN project pr ON pr.id=req.project_id
        WHERE req.client_id=? AND req.verify_state='active'
        ORDER BY pr.domain
        """, (client_id,)).fetchall()


def _pending_projects(conn, client_id: int):
    """Requests not yet delivering — drives the dashboard next-steps card."""
    return conn.execute(
        """SELECT id, domain, verify_state, verify_method, verify_token,
                  created_at
           FROM project_request WHERE client_id=?
             AND verify_state IN ('draft','verified','rejected')
           ORDER BY created_at DESC""", (client_id,)).fetchall()


def _live_ledger(conn, client_id: int, limit: int = 20, cfg: dict | None = None):
    rows = conn.execute(
        """
        SELECT pr.domain AS project, p.domain AS placement, pl.state,
               p.dr, lv.checked_at, pl.id AS plan_id
        FROM link_verify lv
        JOIN plan pl ON pl.id = lv.plan_id
        JOIN project pr ON pr.id = pl.project_id
        JOIN project_request req
          ON req.project_id = pr.id AND req.verify_state='active'
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE req.client_id=? AND lv.result='live'
        ORDER BY lv.checked_at DESC LIMIT ?
        """, (client_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["receipt"] = (evidence_mod.url_for(cfg, r["plan_id"])
                        if cfg else "")
        out.append(d)
    return out


def _capacity_rows(conn, client_id: int, day: str):
    """Delivery capacity gauge (View A layer 2): placements touched today."""
    return conn.execute(
        """
        SELECT p.domain AS placement, COUNT(*) AS used
        FROM plan pl
        JOIN project pr ON pr.id = pl.project_id
        JOIN project_request req
          ON req.project_id = pr.id AND req.verify_state='active'
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE req.client_id=? AND pl.executed_at >= ?
          AND pl.executed_at < date(?, '+1 day')
        GROUP BY p.domain ORDER BY used DESC LIMIT 8
        """, (client_id, day, day)).fetchall()


def _dr_buckets(conn, client_id: int):
    rows = conn.execute(
        """
        SELECT p.dr
        FROM link_verify lv
        JOIN plan pl ON pl.id = lv.plan_id
        JOIN project pr ON pr.id = pl.project_id
        JOIN project_request req
          ON req.project_id = pr.id AND req.verify_state='active'
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE req.client_id=? AND lv.result='live'
        """, (client_id,)).fetchall()
    buckets = {"<30": 0, "30-60": 0, "60+": 0, "unknown": 0}
    for r in rows:
        dr = r["dr"]
        if dr is None:
            buckets["unknown"] += 1
        elif dr < 30:
            buckets["<30"] += 1
        elif dr < 60:
            buckets["30-60"] += 1
        else:
            buckets["60+"] += 1
    return buckets


def _source_mix(conn, client_id: int):
    return conn.execute(
        """
        SELECT COALESCE(NULLIF(p.site_type, ''), 'unclassified') AS stype,
               COUNT(*) AS n
        FROM link_verify lv
        JOIN plan pl ON pl.id = lv.plan_id
        JOIN project pr ON pr.id = pl.project_id
        JOIN project_request req
          ON req.project_id = pr.id AND req.verify_state='active'
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE req.client_id=? AND lv.result='live'
        GROUP BY stype ORDER BY n DESC
        """, (client_id,)).fetchall()


def _funnel(conn, client_id: int, weeks: int = 6):
    """Weekly submitted vs live counts (View B tier 3 trend).

    Week axis is computed in python; sqlite only answers per-week counts.
    """
    starts = [(datetime.utcnow().date() - timedelta(days=7 * i)).isoformat()
              for i in range(weeks - 1, -1, -1)]
    out = []
    for s in starts:
        e = (datetime.fromisoformat(s) + timedelta(days=7)).date().isoformat()
        submitted = conn.execute(
            """
            SELECT COUNT(*) AS n FROM plan pl
            JOIN project pr ON pr.id = pl.project_id
            JOIN project_request req
              ON req.project_id = pr.id AND req.verify_state='active'
            WHERE req.client_id=? AND pl.executed_at >= ?
              AND pl.executed_at < ? AND pl.state IN
              ('submitted','sent','live')
            """, (client_id, s, e)).fetchone()["n"]
        live = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM link_verify lv
            JOIN plan pl ON pl.id = lv.plan_id
            JOIN project pr ON pr.id = pl.project_id
            JOIN project_request req
              ON req.project_id = pr.id AND req.verify_state='active'
            WHERE req.client_id=? AND lv.result='live'
              AND lv.checked_at >= ? AND lv.checked_at < ?
            """, (client_id, s, e)).fetchone()["n"]
        rate = f"{live / submitted * 100:.0f}%" if submitted else "—"
        out.append({"week": s, "submitted": submitted, "live": live,
                    "rate": rate})
    return out


def render_dashboard(conn, client) -> str:
    rows = _client_projects(conn, client["id"])
    if rows:
        quota_rows = "".join(
            f"<tr><td>{_esc(r['domain'])}</td>"
            f"<td>{_esc(r['quota_type'])}</td>"
            f"<td>{r['live']} / {r['quota_total']}</td>"
            f"<td>{r['live_month']}</td>"
            f"<td>{r['inflight']}</td><td>{r['pending']}</td>"
            f"<td>{r['live30']}</td>"
            f"<td class='muted'>{_esc(DENY_TEXT.get(r['trial_note'] or '', '')[:60])}</td>"
            f"</tr>"
            for r in rows)
        quota_card = (
            "<div class='card'><h2>Quota consumption</h2>"
            "<table><tr><th>project</th><th>type</th><th>live / quota</th>"
            "<th>live (this month)</th><th>in flight</th><th>pending</th>"
            "<th>live (30d)</th><th>trial</th></tr>"
            + quota_rows + "</table>"
            "<p class='muted'>Only confirmed live links consume credits; "
            "in-flight and never-live items are not billed. The trial "
            "column explains a withheld free allocation; paid credits have "
            "no per-account limit.</p></div>")

        cap = _capacity_rows(conn, client["id"],
                             datetime.utcnow().date().isoformat())
        if cap:
            cap_rows = "".join(
                f"<tr><td>{_esc(r['placement'])}</td>"
                f"<td>{r['used']} / 100</td></tr>"
                for r in cap)
        else:
            cap_rows = ("<tr><td colspan='2' class='muted'>nothing executed "
                        "today</td></tr>")
        capacity_card = (
            "<div class='card'><h2>Delivery capacity today</h2>"
            "<table><tr><th>placement domain</th><th>addresses used</th>"
            "</tr>" + cap_rows + "</table>"
            "<p class='muted'>Per-domain platform budget: 100 addresses / "
            "24h. Pacing is deliberate — submissions are staggered.</p>"
            "</div>")

        ledger = _live_ledger(conn, client["id"], cfg=self.cfg)
        if ledger:
            led_rows = "".join(
                f"<tr><td>{_esc(r['project'])}</td><td>{_esc(r['placement'])}</td>"
                f"<td>{'%.0f' % r['dr'] if r['dr'] is not None else '—'}</td>"
                f"<td>{_esc(r['checked_at'])}</td>"
                f"<td><a href='{_esc(r['receipt'])}'>receipt →</a></td></tr>"
                for r in ledger)
        else:
            led_rows = ("<tr><td colspan='5' class='muted'>no live links "
                        "confirmed yet</td></tr>")
        effect_card = (
            "<div class='card'><h2>Live-link ledger</h2>"
            "<table><tr><th>project</th><th>placement domain</th><th>DR</th>"
            "<th>confirmed</th><th>receipt</th></tr>" + led_rows + "</table>"
            "<p class='muted'>Tier 1 evidence — each row is a consumed "
            "credit with a public, verifiable receipt. <a href='/reports'>"
            "Weekly report archive →</a></p></div>")

        b = _dr_buckets(conn, client["id"])
        mix = _source_mix(conn, client["id"])
        mix_rows = "".join(f"<tr><td>{_esc(r['stype'])}</td><td>{r['n']}</td>"
                           "</tr>" for r in mix) or \
            "<tr><td class='muted'>no data yet</td></tr>"
        quality_card = (
            "<div class='card'><h2>Placement quality</h2>"
            f"<p>DR distribution — &lt;30: <b>{b['<30']}</b>, 30–60: "
            f"<b>{b['30-60']}</b>, 60+: <b>{b['60+']}</b>, "
            f"unranked: <b>{b['unknown']}</b></p>"
            "<table><tr><th>source type</th><th>live links</th></tr>"
            + mix_rows + "</table></div>")

        weeks = _funnel(conn, client["id"])
        f_rows = "".join(
            f"<tr><td>{w['week']}</td><td>{w['submitted']}</td>"
            f"<td>{w['live']}</td><td>{w['rate']}</td></tr>"
            for w in weeks)
        trend_card = (
            "<div class='card'><h2>Funnel trend (weekly)</h2>"
            "<table><tr><th>week starting</th><th>submitted</th>"
            "<th>confirmed live</th><th>live rate</th></tr>"
            + f_rows + "</table></div>")
        cards = (quota_card + capacity_card + effect_card + quality_card
                 + trend_card)
    else:
        cards = ("<div class='card'><h2>Quota consumption</h2>"
                 "<p class='muted'>No active project yet. "
                 "<a href='/new'>Create your promotion project</a> — "
                 "first verified project gets 3 trial live links "
                 "(client-portal §1.5).</p></div>"
                 "<div class='card'><h2>Promotion effect</h2>"
                 "<p class='muted'>Nothing to show yet.</p></div>")
    return (f"<h1>Welcome, {_esc(client['email'])}</h1>" + cards)


def render_reports(conn, client) -> str:
    """Report archive: B2 machinery rendered on demand (no mail)."""
    from .client_report import build_client_report
    rows = _client_projects(conn, client["id"])
    if not rows:
        return ("<h1>Report archive</h1><div class='card'>"
                "<p class='muted'>No active project yet.</p></div>")
    parts = ["<h1>Report archive (last 7 days)</h1>"]
    for r in rows:
        project = {"_id": r["project_id"], "domain": r["domain"]}
        text = build_client_report(conn, project, days=7)
        parts.append(f"<div class='card'><h2>{_esc(r['domain'])}</h2>"
                     f"<pre>{_esc(text)}</pre></div>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class PortalHandler(BaseHTTPRequestHandler):
    server_version = "osched-portal/0.1"

    # injected by serve(): sqlite conn + cfg
    conn: sqlite3.Connection = None
    cfg: dict = {}

    def log_message(self, fmt, *args):  # concise logs
        sys.stderr.write("portal: " + fmt % args + "\n")

    # -- helpers ----------------------------------------------------------

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _redirect(self, location: str, cookie: str | None = None,
                  extra_cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie",
                             f"osched_session={cookie}; HttpOnly; SameSite=Lax; "
                             f"Path=/; Max-Age={SESSION_DAYS * 86400}")
        if extra_cookie:
            self.send_header("Set-Cookie", extra_cookie)
        self.end_headers()

    def _html(self, body: str, title: str = "client portal",
              nav: str = "") -> None:
        data = PAGE.format(title=_esc(title), body=body, nav=nav).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _png(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()
                if v}

    def _base_url(self) -> str:
        return (os.environ.get("PORTAL_BASE_URL")
                or self.cfg.get("portal", {}).get("base_url")
                or "http://127.0.0.1:8090").rstrip("/")

    def _ip(self) -> str:
        """Client IP for trial/abuse ledgering (proxy-header aware)."""
        return client_ip(self)

    def _signup_block(self, email: str, ip: str) -> str:
        """Empty string = allowed. Whitelisted admins are never rate-limited."""
        wl = self.conn.execute(
            "SELECT 1 FROM admin_whitelist WHERE email=?", (email,)).fetchone()
        if wl:
            return ""
        return signup_blocked(self.conn, ip)

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._api_dispatch("GET", path)
            return
        if path == "/":
            self._page_home()
        elif path == "/verify":
            self._page_verify()
        elif path == "/dashboard":
            self._page_dashboard()
        elif path == "/reports":
            self._page_reports()
        elif path == "/new":
            self._page_new()
        elif path == "/admin":
            self._page_admin()
        elif path == "/auth/google":
            self._oauth_start("google")
        elif path == "/auth/google/callback":
            self._oauth_callback("google")
        elif path == "/auth/facebook":
            self._oauth_start("facebook")
        elif path == "/auth/facebook/callback":
            self._oauth_callback("facebook")
        elif path == "/logout":
            tok = self._cookie("osched_session")
            if tok:
                self.conn.execute("DELETE FROM client_session WHERE token=?",
                                  (tok,))
                self.conn.commit()
            self._redirect("/", cookie="deleted")
        elif path.startswith("/evidence/"):
            self._page_evidence(path[len("/evidence/"):])
        else:
            self._html("<h1>Not found</h1>", title="404")

    def _page_evidence(self, token: str):
        """Public link receipt — no session, unguessable signed URL."""
        shot_name = ""
        if token.endswith("/shot.png"):
            token, shot_name = token[:-len("/shot.png")], "shot.png"
        plan_id = evidence_mod.parse_token(self.cfg, token)
        if plan_id is None:
            self._html("<h1>Not found</h1>", title="404")
            return
        if shot_name:
            s = evidence_mod.shot(self.conn, plan_id)
            if s and s.get("png"):
                self._png(s["png"])
            else:
                self._html("<h1>Not found</h1>", title="404")
            return
        body = evidence_mod.render_page(self.conn, self.cfg, plan_id)
        if body is None:
            self._html("<h1>Not found</h1>", title="404")
            return
        self._html(body, title="link receipt")

    def _page_home(self, notice: str = "", email: str = ""):
        notice_html = f"<p class='ok'>{_esc(notice)}</p>" if notice else ""
        g, f = _oauth_cfg(self.cfg, "google")[0], \
            _oauth_cfg(self.cfg, "facebook")[0]
        gbtn = (f"<a href='/auth/google' style='padding:8px 12px;"
                f"border-radius:6px;background:var(--code);color:var(--text);"
                f"text-decoration:none'>Sign in with Google</a>")
        fbtn = (f"<a href='/auth/facebook' style='padding:8px 12px;"
                f"border-radius:6px;background:var(--code);color:var(--text);"
                f"text-decoration:none'>Sign in with Facebook</a>")
        if not g:
            gbtn = ("<button type='button' disabled title='not configured'>"
                    "Sign in with Google</button>")
        if not f:
            fbtn = ("<button type='button' disabled title='not configured'>"
                    "Sign in with Facebook</button>")
        body = f"""
        {notice_html}
        <div class='card'><h2>Log in</h2>
        <form method='post' action='/login'>
          <p><input name='email' placeholder='email' value='{_esc(email)}' required></p>
          <p><input name='password' type='password' placeholder='password' required></p>
          <p>{gbtn} {fbtn}</p>
          <p><button type='submit'>Log in</button></p>
        </form></div>
        <div class='card'><h2>Create an account</h2>
        <p class='muted'>Registration sends a verification mail; verifying
        grants trial credits when you create your first promotion project.</p>
        <form method='post' action='/register'>
          <p><input name='email' placeholder='email' required></p>
          <p><input name='password' type='password' placeholder='password (min 8 chars)' required minlength=8></p>
          <p><button type='submit'>Register</button></p>
        </form></div>"""
        self._html(body, title="log in")

    def _page_verify(self):
        qs = parse_qs(urlparse(self.path).query)
        token = (qs.get("token") or [""])[0]
        row = self.conn.execute(
            "SELECT * FROM client WHERE verify_token=?", (token,)).fetchone()
        if not row:
            self._html("<h1>Invalid verification link</h1>", title="verify")
            return
        exp = row["verify_expires"] or ""
        if exp and exp < datetime.utcnow().isoformat():
            self._html("<h1>Link expired — register again</h1>", title="verify")
            return
        self.conn.execute(
            """UPDATE client SET verified=1, verify_token=NULL,
               verify_expires=NULL WHERE id=?""", (row["id"],))
        self.conn.commit()
        tok = _new_session(self.conn, row["id"])
        self._redirect("/dashboard", cookie=tok)

    def _page_dashboard(self):
        client = _client_from_session(self.conn, self._cookie("osched_session"))
        if not client:
            self._redirect("/")
            return
        nav = self._nav_links()
        self._html(render_dashboard(self.conn, client),
                   title="dashboard", nav=nav)

    def _page_reports(self):
        client = _client_from_session(self.conn, self._cookie("osched_session"))
        if not client:
            self._redirect("/")
            return
        nav = self._nav_links()
        self._html(render_reports(self.conn, client),
                   title="reports", nav=nav)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._api_dispatch("POST", path)
            return
        if path == "/register":
            self._post_register()
        elif path == "/login":
            self._post_login()
        elif path == "/new":
            self._post_new()
        elif path == "/verify-domain":
            self._post_verify_domain()
        elif path == "/admin":
            self._post_admin()
        elif path == "/auth/facebook/data-deletion":
            self._fb_data_deletion()
        else:
            self._html("<h1>Not found</h1>", title="404")

    def _fb_data_deletion(self):
        """Facebook 'Data deletion request callback' (Basic settings).

        FB POSTs signed_request=<HMAC-signed payload with user_id>.
        We anonymize the matching client row, log the request, and reply
        with the JSON contract FB expects.
        """
        _, csec = _oauth_cfg(self.cfg, "facebook")
        if not csec:
            self._api_send({"error": "facebook app secret not configured"},
                           status=503)
            return
        signed = self._form().get("signed_request") or ""
        try:
            data = _fb_parse_signed_request(signed, csec)
        except Exception as e:  # noqa: BLE001 — reject unverifiable payloads
            self._api_send({"error": f"invalid signed_request: {e}"},
                           status=403)
            return
        uid = str(data.get("user_id") or "")
        if not uid:
            self._api_send({"error": "user_id missing"}, status=400)
            return
        code = secrets.token_urlsafe(16)
        conn = self.conn
        row = conn.execute(
            "SELECT id FROM client WHERE oauth_provider='facebook' "
            "AND oauth_sub=?", (uid,)).fetchone()
        client_id = row["id"] if row else None
        if client_id:
            junk = f"deleted-{code[:8]}@invalid.local"
            conn.execute(
                """UPDATE client SET email=?, password_hash=?, verified=0,
                   oauth_provider=NULL, oauth_sub=NULL, verify_token=NULL,
                   verify_expires=NULL WHERE id=?""",
                (junk, f"deleted:{secrets.token_urlsafe(24)}", client_id))
            conn.execute("DELETE FROM client_session WHERE client_id=?",
                         (client_id,))
        conn.execute(
            """INSERT INTO data_deletion(provider, provider_user_id,
               client_id, confirmation_code) VALUES('facebook',?,?,?)""",
            (uid, client_id, code))
        conn.commit()
        self._api_send({
            "url": privacy_url(self.cfg),
            "confirmation_code": code,
        })

    def _post_register(self):
        f = self._form()
        email = (f.get("email") or "").strip().lower()
        password = f.get("password") or ""
        if "@" not in email or len(password) < 8:
            self._page_home(notice="Invalid email or password too short "
                                   "(min 8 chars).", email=email)
            return
        exists = self.conn.execute(
            "SELECT 1 FROM client WHERE email=?", (email,)).fetchone()
        if exists:
            self._page_home(notice="This email is already registered — log in.",
                            email=email)
            return
        ip = self._ip()
        blocked = self._signup_block(email, ip)
        if blocked:
            self._page_home(notice=blocked, email=email)
            return
        token = secrets.token_urlsafe(32)
        exp = (datetime.utcnow() + timedelta(hours=VERIFY_HOURS)).isoformat()
        self.conn.execute(
            """INSERT INTO client(email, password_hash, verified,
               verify_token, verify_expires, role, signup_ip, last_ip)
               VALUES(?,?,0,?,?,
                 CASE WHEN ? IN (SELECT email FROM admin_whitelist)
                   THEN 'admin' ELSE 'client' END, ?, ?)""",
            (email, hash_password(password), token, exp, email, ip, ip))
        self.conn.commit()
        link = f"{self._base_url()}/verify?token={token}"
        dev = os.environ.get("PORTAL_DEV") == "1" or \
            self.cfg.get("portal", {}).get("dev")
        if dev:
            print(f"portal: DEV verification link for {email}: {link}",
                  flush=True)
            self._page_home(notice="DEV mode: verification link printed to "
                                   "server console.", email=email)
            return
        try:
            send_mail(self.cfg, email,
                      "Verify your client portal account",
                      f"Confirm your registration:\n\n{link}\n\n"
                      f"Link valid for {VERIFY_HOURS}h.\n")
            self._page_home(notice="Verification mail sent — check your "
                                   "inbox.", email=email)
        except Exception as e:  # noqa: BLE001 — surface, never 500 silently
            self._page_home(notice=f"Could not send verification mail: "
                                   f"{_esc(str(e)[:120])}", email=email)

    def _post_login(self):
        f = self._form()
        email = (f.get("email") or "").strip().lower()
        password = f.get("password") or ""
        row = self.conn.execute(
            "SELECT * FROM client WHERE email=?", (email,)).fetchone()
        if not row or not check_password(password, row["password_hash"]):
            self._page_home(notice="Wrong email or password.", email=email)
            return
        if not row["verified"]:
            self._page_home(notice="Account not verified — check your inbox "
                                   "for the verification mail.", email=email)
            return
        self.conn.execute("UPDATE client SET last_ip=? WHERE id=?",
                          (self._ip(), row["id"]))
        self.conn.commit()
        tok = _new_session(self.conn, row["id"])
        self._redirect("/dashboard", cookie=tok)

    # -- self-service project creation (§1.5) ------------------------------

    def _require_client(self):
        return _client_from_session(self.conn,
                                    self._cookie("osched_session"))

    def _page_new(self, notice: str = ""):
        client = self._require_client()
        if not client:
            self._redirect("/")
            return
        reqs = self.conn.execute(
            """SELECT * FROM project_request WHERE client_id=?
               ORDER BY created_at DESC""", (client["id"],)).fetchall()
        req_html = ""
        for r in reqs:
            if r["verify_state"] == "draft":
                snippet = (f"&lt;meta name='osched-verify' "
                           f"content='{_esc(r['verify_token'])}'&gt;")
                req_html += f"""
        <div class='card'><b>{_esc(r['domain'])}</b>
          <span class='muted'>— pending ownership verification</span>
          <p class='muted'>Add this tag inside the <code>&lt;head&gt;</code>
          of your site's homepage:</p>
          <p><code>{snippet}</code></p>
          <form method='post' action='/verify-domain'>
            <input type='hidden' name='id' value='{r['id']}'>
            <button type='submit'>Verify now</button>
          </form></div>"""
            else:
                req_html += (f"<div class='card'><b>{_esc(r['domain'])}</b> "
                             f"<span class='ok'>{_esc(r['verify_state'])}</span>"
                             f" <span class='muted'>quota "
                             f"{r['quota_used']}/{r['quota_total']} "
                             f"({_esc(r['quota_type'])})</span></div>")
        trial = "" if _trial_used(self.conn, client["id"]) else \
            "<p class='ok'>First verified project gets 3 trial live links.</p>"
        notice_html = f"<p class='warn'>{_esc(notice)}</p>" if notice else ""
        body = f"""
        <h1>New promotion project</h1>{notice_html}{trial}
        <div class='card'>
        <form method='post' action='/new'>
          <p><input name='domain' placeholder='your-domain.com' required></p>
          <p><input name='site_title' placeholder='site title' required></p>
          <p><textarea name='site_summary' rows='3' style='width:100%'
             placeholder='one-paragraph site summary'></textarea></p>
          <p><input name='contact_email' placeholder='contact email' required></p>
          <p><input name='tags' placeholder='tags, comma-separated (optional)'></p>
          <p><button type='submit'>Create project</button></p>
        </form></div>
        <h2>Your projects</h2>{req_html or
            "<p class='muted'>none yet</p>"}"""
        self._html(body, title="new project",
                   nav=self._nav_links())

    def _nav_links(self) -> str:
        c = self._require_client()
        admin = ("<a href='/admin' style='color:inherit'>admin</a> "
                 if c is not None and c["role"] == "admin" else "")
        return ("<a href='/dashboard' style='color:inherit'>dashboard</a> "
                "<a href='/reports' style='color:inherit'>reports</a> "
                "<a href='/new' style='color:inherit'>new project</a> "
                + admin +
                "<form method='post' action='/logout' style='display:inline'>"
                "<button style='padding:2px 8px'>Log out</button></form>")

    def _post_new(self):
        client = self._require_client()
        if not client:
            self._redirect("/")
            return
        ok, msg = create_request(self.conn, client, self._form())
        self._page_new(notice=None if ok else msg)

    def _post_verify_domain(self):
        client = self._require_client()
        if not client:
            self._redirect("/")
            return
        ok, msg = verify_and_activate(self.conn, client["id"],
                                      self._form().get("id") or "",
                                      ip=self._ip())
        self._page_new(notice=msg)

    def do_OPTIONS(self):  # CORS preflight for the Cloudflare Pages origin
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # -- JSON API (Cloudflare Pages frontend consumes these) ---------------

    def _api_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token")

    def _api_send(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Auth-Token")
        self.end_headers()
        self.wfile.write(data)

    def _api_auth(self):
        token = self.headers.get("X-Auth-Token") or \
            self._cookie("osched_session")
        return _client_from_session(self.conn, token)

    def _api_dispatch(self, method: str, path: str):
        try:
            if method == "GET":
                routes = {
                    "/api/dashboard": self._api_dashboard,
                    "/api/reports": self._api_reports,
                    "/api/requests": self._api_requests,
                    "/api/admin": self._api_admin,
                    "/api/oauth/status": self._api_oauth_status,
                }
            else:
                routes = {
                    "/api/register": self._api_register,
                    "/api/login": self._api_login,
                    "/api/logout": self._api_logout,
                    "/api/new": self._api_new,
                    "/api/verify-domain": self._api_verify_domain,
                    "/api/request-package": self._api_request_package,
                    "/api/admin": self._api_admin_post,
                    "/api/oauth/exchange": self._api_oauth_exchange,
                }
            fn = routes.get(path)
            if fn:
                fn()
            else:
                self._api_send({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001 — JSON error, never crash
            self._api_send({"error": str(e)[:200]}, 500)

    def _api_dashboard(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        projects = [dict(r) for r in _client_projects(self.conn, client["id"])]
        day = datetime.utcnow().date().isoformat()
        payload = {
            "email": client["email"], "role": client["role"],
            "projects": projects,
            "pending": [dict(r) for r in
                        _pending_projects(self.conn, client["id"])],
            "capacity": [dict(r) for r in
                         _capacity_rows(self.conn, client["id"], day)],
            "ledger": [dict(r) for r in
                       _live_ledger(self.conn, client["id"],
                                    cfg=self.cfg)],
            "dr_buckets": _dr_buckets(self.conn, client["id"]),
            "source_mix": [dict(r) for r in
                           _source_mix(self.conn, client["id"])],
            "funnel": _funnel(self.conn, client["id"]),
        }
        self._api_send(payload)

    def _api_reports(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        from .client_report import build_client_report
        out = []
        for r in _client_projects(self.conn, client["id"]):
            project = {"_id": r["project_id"], "domain": r["domain"]}
            out.append({"domain": r["domain"],
                        "text": build_client_report(self.conn, project,
                                                    days=7)})
        self._api_send({"reports": out})

    def _api_requests(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        rows = self.conn.execute(
            "SELECT * FROM project_request WHERE client_id=? "
            "ORDER BY created_at DESC", (client["id"],)).fetchall()
        self._api_send({"requests": [dict(r) for r in rows],
                        "trial_used": _trial_used(self.conn, client["id"])})

    def _api_new(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        ok, msg = create_request(self.conn, client, body)
        self._api_send({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_verify_domain(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        ok, msg = verify_and_activate(self.conn, client["id"],
                                      str(body.get("id") or ""),
                                      ip=self._ip())
        self._api_send({"ok": ok, "message": msg}, 200 if ok else 400)

    def _api_register(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if "@" not in email or len(password) < 8:
            self._api_send({"ok": False,
                            "error": "invalid email or short password"}, 400)
            return
        if self.conn.execute("SELECT 1 FROM client WHERE email=?",
                             (email,)).fetchone():
            self._api_send({"ok": False, "error": "already registered"}, 400)
            return
        ip = self._ip()
        blocked = self._signup_block(email, ip)
        if blocked:
            self._api_send({"ok": False, "error": blocked}, 429)
            return
        token = secrets.token_urlsafe(32)
        exp = (datetime.utcnow() + timedelta(hours=VERIFY_HOURS)).isoformat()
        self.conn.execute(
            """INSERT INTO client(email, password_hash, verified,
               verify_token, verify_expires, role, signup_ip, last_ip)
               VALUES(?,?,0,?,?,
                 CASE WHEN ? IN (SELECT email FROM admin_whitelist)
                   THEN 'admin' ELSE 'client' END, ?, ?)""",
            (email, hash_password(password), token, exp, email, ip, ip))
        self.conn.commit()
        link = f"{self._base_url()}/verify?token={token}"
        dev = os.environ.get("PORTAL_DEV") == "1" or \
            self.cfg.get("portal", {}).get("dev")
        if dev:
            print(f"portal: DEV verification link for {email}: {link}",
                  flush=True)
            self._api_send({"ok": True, "dev_link": link})
            return
        try:
            send_mail(self.cfg, email, "Verify your client portal account",
                      f"Confirm your registration:\n\n{link}\n\n"
                      f"Link valid for {VERIFY_HOURS}h.\n")
            self._api_send({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._api_send({"ok": False, "error": str(e)[:200]}, 500)

    def _api_login(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        row = self.conn.execute("SELECT * FROM client WHERE email=?",
                                (email,)).fetchone()
        if not row or not check_password(password, row["password_hash"]):
            self._api_send({"ok": False, "error": "wrong credentials"}, 401)
            return
        if not row["verified"]:
            self._api_send({"ok": False, "error": "not verified"}, 403)
            return
        self.conn.execute("UPDATE client SET last_ip=? WHERE id=?",
                          (self._ip(), row["id"]))
        self.conn.commit()
        token = _new_session(self.conn, row["id"])
        self._api_send({"ok": True, "token": token,
                        "role": row["role"], "email": email})

    def _api_request_package(self):
        client = self._api_auth()
        if not client:
            self._api_send({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        tier = (body.get("tier") or "").strip().lower()
        if tier not in PACKAGE_TIERS:
            self._api_send({"ok": False, "error": "unknown tier"}, 400)
            return
        try:
            links = max(0, int(body.get("links") or 0)) or None
        except (TypeError, ValueError):
            links = None
        self.conn.execute(
            """INSERT INTO package_request(client_id, tier, links_planned,
               price_per_link) VALUES(?,?,?,?)""",
            (client["id"], tier, links, PACKAGE_TIERS[tier]))
        self.conn.commit()
        self._api_send({"ok": True,
                        "message": f"{tier} package request sent — "
                        "we confirm details within 24h and set your paid "
                        "quota. Historical ledger stays as evidence."})

    def _api_logout(self):
        token = self.headers.get("X-Auth-Token") or \
            self._cookie("osched_session")
        if token:
            self.conn.execute("DELETE FROM client_session WHERE token=?",
                              (token,))
            self.conn.commit()
        self._api_send({"ok": True})

    def _api_oauth_status(self):
        g = bool(_oauth_cfg(self.cfg, "google")[0])
        f = bool(_oauth_cfg(self.cfg, "facebook")[0])
        self._api_send({"google": g, "facebook": f})

    def _api_oauth_exchange(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        code = str(body.get("code") or "")
        row = self.conn.execute(
            """SELECT h.*, s.client_id FROM oauth_handoff h
               JOIN client_session s ON s.token=h.session_token
               WHERE h.code=? AND h.expires_at > datetime('now')""",
            (code,)).fetchone()
        if not row:
            self._api_send({"ok": False,
                            "error": "invalid or expired sign-in code"}, 400)
            return
        self.conn.execute("DELETE FROM oauth_handoff WHERE code=?", (code,))
        self.conn.commit()
        client = self.conn.execute("SELECT * FROM client WHERE id=?",
                                   (row["client_id"],)).fetchone()
        self._api_send({"ok": True, "token": row["session_token"],
                        "role": client["role"], "email": client["email"]})

    def _api_admin(self):
        client = self._api_auth()
        if not client or client["role"] != "admin":
            self._api_send({"error": "forbidden"}, 403)
            return
        drafts = [dict(r) for r in self.conn.execute(
            """SELECT r.*, c.email FROM project_request r
               JOIN client c ON c.id=r.client_id
               WHERE r.verify_state IN ('draft','verified')
               ORDER BY r.created_at""")]
        actives = [dict(r) for r in self.conn.execute(
            """SELECT r.*, c.email FROM project_request r
               JOIN client c ON c.id=r.client_id
               WHERE r.verify_state='active' ORDER BY r.domain""")]
        clients = [dict(r) for r in self.conn.execute(
            "SELECT id,email,role,verified,created_at FROM client "
            "ORDER BY email")]
        wl = [r["email"] for r in self.conn.execute(
            "SELECT email FROM admin_whitelist ORDER BY email")]
        packages = [dict(r) for r in self.conn.execute(
            """SELECT p.*, c.email FROM package_request p
               JOIN client c ON c.id=p.client_id
               WHERE p.status='pending' ORDER BY p.created_at""")]
        self._api_send({"drafts": drafts, "actives": actives,
                        "clients": clients, "whitelist": wl,
                        "packages": packages})

    def _api_admin_post(self):
        client = self._api_auth()
        if not client or client["role"] != "admin":
            self._api_send({"error": "forbidden"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        body = json.loads(raw or "{}")
        ok, msg = admin_action(self.conn, client, body.get("action"),
                               id=body.get("id"),
                               quota_total=body.get("quota_total"),
                               email=body.get("email"))
        self._api_send({"ok": ok, "message": msg}, 200 if ok else 400)

    # -- OAuth (Google / Facebook) -----------------------------------------

    def _oauth_error(self, msg: str) -> None:
        self._html(f"<h1>Sign-in failed</h1><p class='warn'>{_esc(msg)}</p>"
                   f"<p><a href='/'>Back to login</a></p>", title="oauth")

    def _oauth_start(self, provider: str) -> None:
        cid, _ = _oauth_cfg(self.cfg, provider)
        if not cid:
            self._oauth_error(
                f"{provider} sign-in is not configured on this deployment.")
            return
        state = secrets.token_urlsafe(16)
        # state is validated server-side (single use, 10 min TTL) because
        # privacy browsers (WebKit ITP et al.) drop Set-Cookie on the first
        # hop of a cross-site redirect chain; the cookie stays as an extra
        # check where it works.
        self.conn.execute(
            "DELETE FROM oauth_state WHERE created_at <= "
            "datetime('now','-10 minutes')")
        self.conn.execute("INSERT INTO oauth_state(state) VALUES (?)",
                          (state,))
        self.conn.commit()
        cookie = (f"{OAUTH_STATE_COOKIE}={state}; HttpOnly; SameSite=Lax; "
                  f"Path=/; Max-Age={OAUTH_STATE_MAX_AGE}")
        ep = OAUTH_ENDPOINTS[provider]
        qs = urlencode({
            "client_id": cid,
            "redirect_uri": _oauth_redirect_uri(self.cfg, provider),
            "response_type": "code",
            "scope": ep["scope"],
            "state": state,
            **({"prompt": "select_account"} if provider == "google" else {}),
        })
        self._redirect(f"{ep['authorize']}?{qs}", extra_cookie=cookie)

    def _oauth_callback(self, provider: str) -> None:
        qs = parse_qs(urlparse(self.path).query)
        state = (qs.get("state") or [""])[0]
        cookie_state = self._cookie(OAUTH_STATE_COOKIE) or ""
        # consume the stored state regardless of outcome (single use);
        # valid when it matches the cookie OR the server-side store
        stored = False
        if state:
            stored = self.conn.execute(
                "SELECT 1 FROM oauth_state WHERE state=? AND created_at > "
                "datetime('now','-10 minutes')", (state,)).fetchone() \
                is not None
            self.conn.execute("DELETE FROM oauth_state WHERE state=?",
                              (state,))
            self.conn.commit()
        expire = (f"{OAUTH_STATE_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age=0")
        if not state or (state != cookie_state and not stored):
            self._redirect("/", extra_cookie=expire)
            return
        code = (qs.get("code") or [""])[0]
        if not code:
            err = (qs.get("error_description") or qs.get("error")
                   or ["provider denied the request"])[0]
            self._oauth_error(str(err)[:200])
            return
        try:
            ident = _oauth_exchange_code(
                self.cfg, provider, code,
                _oauth_redirect_uri(self.cfg, provider))
            client_id = _oauth_upsert_client(
                self.conn, provider, ident["sub"], ident["email"],
                ip=self._ip())
        except Exception as e:  # noqa: BLE001 — render as user-facing page
            self._oauth_error(str(e)[:200])
            return
        tok = _new_session(self.conn, client_id)
        frontend = _frontend_url(self.cfg)
        if frontend:
            handoff = _new_handoff(self.conn, tok)
            self._redirect(f"{frontend}/#/oauth?c={handoff}",
                           extra_cookie=expire)
            return
        # legacy HTML portal on the backend domain — cookie works directly
        self._redirect("/dashboard", cookie=tok, extra_cookie=expire)


    # -- super admin (whitelist privileges) --------------------------------

    def _page_admin(self, notice: str = ""):
        client = self._require_client()
        if not client or client["role"] != "admin":
            self._html("<h1>Forbidden</h1>"
                       "<p class='muted'>Whitelisted admins only.</p>",
                       title="403")
            return
        notice_html = f"<p class='warn'>{_esc(notice)}</p>" if notice else ""
        drafts = self.conn.execute(
            """SELECT r.*, c.email FROM project_request r
               JOIN client c ON c.id=r.client_id
               WHERE r.verify_state IN ('draft','verified')
               ORDER BY r.created_at""").fetchall()
        draft_rows = ""
        for r in drafts:
            draft_rows += (f"<tr><td>{_esc(r['email'])}</td>"
                           f"<td>{_esc(r['domain'])}</td>"
                           f"<td>{_esc(r['verify_state'])}</td>"
                           f"<td><form method='post' action='/admin'>"
                           f"<input type='hidden' name='action' value='approve'>"
                           f"<input type='hidden' name='id' value='{r['id']}'>"
                           f"<button style='padding:2px 8px'>Approve</button>"
                           f"</form></td></tr>")
        if not draft_rows:
            draft_rows = ("<tr><td colspan='4' class='muted'>no pending "
                          "requests</td></tr>")
        actives = self.conn.execute(
            """SELECT r.*, c.email FROM project_request r
               JOIN client c ON c.id=r.client_id
               WHERE r.verify_state='active' ORDER BY r.domain""").fetchall()
        active_rows = ""
        for r in actives:
            active_rows += (
                f"<tr><td>{_esc(r['domain'])}</td><td>{_esc(r['email'])}</td>"
                f"<td>{_esc(r['quota_type'])}</td>"
                f"<td>{r['quota_used']} / {r['quota_total']}</td>"
                f"<td class='muted'>{_esc(r['trial_note'] or '-')}</td>"
                f"<td><form method='post' action='/admin' "
                f"style='display:flex;gap:4px'>"
                f"<input type='hidden' name='action' value='quota'>"
                f"<input type='hidden' name='id' value='{r['id']}'>"
                f"<input name='quota_total' value='{r['quota_total']}' "
                f"style='width:60px'>"
                f"<button style='padding:2px 8px'>Set</button></form>"
                f"</td></tr>")
        if not active_rows:
            active_rows = ("<tr><td colspan='6' class='muted'>no active "
                           "projects</td></tr>")
        clients = self.conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM project_request r
                WHERE r.client_id=c.id AND r.verify_state='active') AS npr
               FROM client c ORDER BY c.email""").fetchall()
        wl = {r["email"] for r in self.conn.execute(
            "SELECT email FROM admin_whitelist")}
        client_rows = ""
        for c in clients:
            in_wl = c["email"] in wl
            client_rows += (
                f"<tr><td>{_esc(c['email'])}</td><td>{_esc(c['role'])}</td>"
                f"<td>{c['npr']}</td>"
                f"<td class='muted'>{_esc(c['signup_ip'] or '-')}</td><td>"
                f"<form method='post' action='/admin'>"
                f"<input type='hidden' name='action' value='whitelist'>"
                f"<input type='hidden' name='email' value='{_esc(c['email'])}'>"
                f"<button style='padding:2px 8px'>"
                f"{'Remove' if in_wl else 'Whitelist'}</button>"
                f"</form></td></tr>")
        wl_rows = "".join(f"<code>{_esc(r['email'])}</code> "
                          for r in self.conn.execute(
                              "SELECT email FROM admin_whitelist ORDER BY email"))
        body = f"""
        <h1>Super admin</h1>{notice_html}
        <div class='card'><h2>Whitelist</h2>
        <p>{wl_rows or '<span class=muted>empty</span>'}</p>
        <p class='muted'>Whitelisted emails get role=admin at registration
        or next login. Sources: config portal.admin_emails + env, plus
        manual entries below. Anyone can still register; the whitelist is
        a privilege gate, not a registration gate.</p>
        <form method='post' action='/admin' style='display:flex;gap:6px'>
          <input type='hidden' name='action' value='whitelist'>
          <input name='email' placeholder='email to whitelist'>
          <button style='padding:4px 10px'>Add</button>
        </form></div>
        <div class='card'><h2>Pending project requests</h2>
        <table><tr><th>client</th><th>domain</th><th>state</th><th></th></tr>
        {draft_rows}</table>
        <p class='muted'>Approve = activate without meta verification
        (manual override for trusted cases). Delivery gates unchanged:
        match/confirm stay scheduler-owned.</p></div>
        <div class='card'><h2>Active projects &amp; quotas</h2>
        <table><tr><th>project</th><th>client</th><th>type</th>
        <th>used / total</th><th>trial</th><th>set total</th></tr>
        {active_rows}</table>
        <p class='muted'>trial column = why the free credits were granted or
        withheld (guard.py signals: mailbox, site, network). Withheld
        projects start at 0 and are invisible to the scheduler until you
        set a total below.</p></div>
        <div class='card'><h2>Clients</h2>
        <table><tr><th>email</th><th>role</th><th>projects</th>
        <th>signup ip</th><th></th></tr>
        {client_rows}</table></div>"""
        self._html(body, title="admin", nav=self._nav_links())

    def _post_admin(self):
        client = self._require_client()
        if not client or client["role"] != "admin":
            self._page_admin(notice="Forbidden.")
            return
        f = self._form()
        ok, notice = admin_action(self.conn, client, f.get("action"),
                                  id=f.get("id"),
                                  quota_total=f.get("quota_total"),
                                  email=f.get("email"))
        self._page_admin(notice=notice)



def create_request(conn, client, form: dict) -> tuple[bool, str]:
    """Self-service project creation core (shared by HTML and JSON)."""
    domain = normalize_domain(form.get("domain"))
    dev = os.environ.get("PORTAL_DEV") == "1"
    err = validate_domain(domain, dev=dev)
    if err:
        return False, err
    if _domain_claimed(conn, domain, client["id"]):
        return False, "This domain is already claimed by another account."
    blocked = draft_quota_message(conn, client["id"])
    if blocked:
        return False, blocked
    dup = conn.execute(
        "SELECT 1 FROM project_request WHERE client_id=? AND domain=?",
        (client["id"], domain)).fetchone()
    if dup:
        return False, "You already have a request for this domain."
    tags = [t.strip() for t in (form.get("tags") or "").split(",")
            if t.strip()]
    token = "osched-" + secrets.token_urlsafe(16)
    conn.execute(
        """INSERT INTO project_request(client_id, domain, site_title,
           site_summary, contact_email, tags_json, verify_method,
           verify_token, verify_state)
           VALUES(?,?,?,?,?,?, 'meta', ?, 'draft')""",
        (client["id"], domain, (form.get("site_title") or "").strip(),
         (form.get("site_summary") or "").strip(),
         (form.get("contact_email") or "").strip(),
         json.dumps(tags), token))
    conn.commit()
    return True, domain


def verify_and_activate(conn, client_id: int, rid: str,
                        ip: str = "") -> tuple[bool, str]:
    """Ownership check + activation core (shared by HTML and JSON)."""
    req = conn.execute(
        """SELECT * FROM project_request WHERE id=? AND client_id=?
           AND verify_state='draft'""",
        (rid, client_id)).fetchone()
    if not req:
        return False, "No pending request found."
    if _domain_claimed(conn, req["domain"], client_id):
        return False, "This domain was claimed by another account."
    try:
        found = fetch_meta_token(_verify_url(req["domain"]))
    except Exception as e:  # noqa: BLE001
        return False, f"Could not fetch {req['domain']}: {str(e)[:120]}"
    if found != req["verify_token"]:
        return False, ("Verification tag not found on your homepage — "
                       "add it to <head> and retry.")
    result = activate_request(conn, req, ip=ip)
    if result["trial"]:
        return True, (f"{req['domain']} verified — project active with "
                      f"{result['credits']} trial live links.")
    return True, (f"{req['domain']} verified — project active. Free trial "
                  f"not granted: {result['deny_text']}")


def admin_action(conn, client, action: str, **kw) -> tuple[bool, str]:
    """Shared admin action core for HTML and JSON paths.

    Returns (ok, notice_text) — plain text, escape at render time.
    """
    action = (action or "").strip()
    if action == "approve":
        req = conn.execute(
            "SELECT * FROM project_request WHERE id=? AND "
            "verify_state IN ('draft','verified')",
            (kw.get("id") or "",)).fetchone()
        if not req:
            return False, "Request not found."
        # Manual approval still runs the email/site guards, but skips the
        # network rule (the IP here is the admin's, not the client's).
        result = activate_request(conn, req, ip="")
        if result["trial"]:
            return True, (f"{req['domain']} approved "
                          f"({result['credits']} trial credits).")
        return True, (f"{req['domain']} approved without credits — "
                      f"{result['deny_text']} Set a quota below to grant.")
    if action == "quota":
        try:
            total = max(0, int(kw.get("quota_total") or 0))
        except (TypeError, ValueError):
            total = 0
        conn.execute(
            "UPDATE project_request SET quota_total=? WHERE id=? AND "
            "verify_state='active'", (total, kw.get("id") or ""))
        conn.commit()
        return True, "Quota updated."
    if action in ("package_done", "package_cancel"):
        st = "done" if action == "package_done" else "cancelled"
        conn.execute("UPDATE package_request SET status=? WHERE id=?",
                     (st, kw.get("id") or ""))
        conn.commit()
        return True, f"Package request {st}."
    if action == "whitelist":
        email = (kw.get("email") or "").strip().lower()
        if not email:
            return False, "Empty email."
        wl_now = {r["email"] for r in conn.execute(
            "SELECT email FROM admin_whitelist")}
        if email in wl_now:
            if len(wl_now) == 1:
                return False, ("Cannot remove the last whitelisted admin "
                               "- add another one first.")
            conn.execute("DELETE FROM admin_whitelist WHERE email=?", (email,))
            conn.execute("UPDATE client SET role='client' WHERE email=?",
                         (email,))
            conn.commit()
            return True, f"{email} removed from whitelist."
        conn.execute(
            "INSERT OR IGNORE INTO admin_whitelist(email, note) "
            "VALUES(?, 'added by admin')", (email,))
        conn.execute("UPDATE client SET role='admin' WHERE email=?", (email,))
        conn.commit()
        return True, f"{email} whitelisted (admin)."
    return False, "Unknown action."


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def serve(cfg: dict, data_root: pathlib.Path, host: str, port: int) -> int:
    # single-threaded server: portal traffic is tiny, and one connection
    # in one thread sidesteps sqlite cross-thread restrictions entirely
    conn = db_connect(data_root)
    conn.executescript(PORTAL_SCHEMA)
    _migrate_portal(conn)
    seed_admin_whitelist(conn, cfg)
    conn.commit()
    PortalHandler.conn = conn
    PortalHandler.cfg = cfg
    srv = HTTPServer((host, port), PortalHandler)
    print(f"portal: serving on http://{host}:{port} "
          f"(data_root={data_root})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return 0
