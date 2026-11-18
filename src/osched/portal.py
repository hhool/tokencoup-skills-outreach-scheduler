"""Client portal (business docs client-portal.md §1–§2, skeleton phase).

Stdlib-only HTTP server, single process, shares the scheduler's sqlite
in read-only fashion for delivery-side tables. Portal-owned writes are
exactly: client identity, sessions, project requests (docs §4 write-path
discipline). OAuth buttons are stubs in this phase (Google first when
callback domain exists).

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

import hashlib
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
from urllib.parse import parse_qs, urlparse

from .db import connect as db_connect
from .report import send_mail

SESSION_DAYS = 14
VERIFY_HOURS = 24

PORTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS client (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,          -- scrypt$salt_hex$hash_hex
  verified      INTEGER NOT NULL DEFAULT 0,
  oauth_provider TEXT,                  -- google | facebook | NULL (stub phase)
  oauth_sub     TEXT,
  verify_token  TEXT,
  verify_expires TEXT,
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
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(client_id, domain)
);
"""

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
    return conn.execute(
        """SELECT 1 FROM project_request WHERE client_id=?
           AND quota_type='trial'
           AND verify_state IN ('verified','active') LIMIT 1""",
        (client_id,)).fetchone() is not None


def _domain_claimed(conn, domain: str, exclude_client: int) -> bool:
    return conn.execute(
        """SELECT 1 FROM project_request WHERE domain=?
           AND verify_state IN ('verified','active')
           AND client_id != ? LIMIT 1""",
        (domain, exclude_client)).fetchone() is not None


def activate_request(conn, req) -> dict:
    """Ownership verified -> link/create the scheduler project, grant trial."""
    trial = not _trial_used(conn, req["client_id"])
    credits = (3 if trial else 0)
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
           quota_type=?, quota_total=?, quota_used=0 WHERE id=?""",
        (project_id, "trial" if trial else "paid",
         credits if trial else 0, req["id"]))
    conn.commit()
    return {"project_id": project_id, "trial": trial, "credits": credits}


# --------------------------------------------------------------------------
# dashboard queries (read-only over scheduler tables)
# --------------------------------------------------------------------------

def _client_projects(conn, client_id: int):
    return conn.execute(
        """
        SELECT pr.id AS project_id, pr.domain, req.quota_type, req.quota_total,
          req.quota_used,
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


def _live_ledger(conn, client_id: int, limit: int = 20):
    return conn.execute(
        """
        SELECT pr.domain AS project, p.domain AS placement, pl.state,
               p.dr, lv.checked_at
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
            f"<td>{r['live30']}</td></tr>"
            for r in rows)
        quota_card = (
            "<div class='card'><h2>Quota consumption</h2>"
            "<table><tr><th>project</th><th>type</th><th>live / quota</th>"
            "<th>live (this month)</th><th>in flight</th><th>pending</th>"
            "<th>live (30d)</th></tr>"
            + quota_rows + "</table>"
            "<p class='muted'>Only confirmed live links consume credits; "
            "in-flight and never-live items are not billed.</p></div>")

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

        ledger = _live_ledger(conn, client["id"])
        if ledger:
            led_rows = "".join(
                f"<tr><td>{_esc(r['project'])}</td><td>{_esc(r['placement'])}</td>"
                f"<td>{'%.0f' % r['dr'] if r['dr'] is not None else '—'}</td>"
                f"<td>{_esc(r['checked_at'])}</td></tr>" for r in ledger)
        else:
            led_rows = ("<tr><td colspan='4' class='muted'>no live links "
                        "confirmed yet</td></tr>")
        effect_card = (
            "<div class='card'><h2>Live-link ledger</h2>"
            "<table><tr><th>project</th><th>placement domain</th><th>DR</th>"
            "<th>confirmed</th></tr>" + led_rows + "</table>"
            "<p class='muted'>Tier 1 evidence — each row is a consumed "
            "credit with a receipt. <a href='/reports'>Weekly report "
            "archive →</a></p></div>")

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

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie",
                             f"osched_session={cookie}; HttpOnly; SameSite=Lax; "
                             f"Path=/; Max-Age={SESSION_DAYS * 86400}")
        self.end_headers()

    def _html(self, body: str, title: str = "client portal",
              nav: str = "") -> None:
        data = PAGE.format(title=_esc(title), body=body, nav=nav).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()
                if v}

    def _base_url(self) -> str:
        return (os.environ.get("PORTAL_BASE_URL")
                or self.cfg.get("portal", {}).get("base_url")
                or "http://127.0.0.1:8090").rstrip("/")

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
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
        elif path == "/logout":
            tok = self._cookie("osched_session")
            if tok:
                self.conn.execute("DELETE FROM client_session WHERE token=?",
                                  (tok,))
                self.conn.commit()
            self._redirect("/", cookie="deleted")
        else:
            self._html("<h1>Not found</h1>", title="404")

    def _page_home(self, notice: str = "", email: str = ""):
        notice_html = f"<p class='ok'>{_esc(notice)}</p>" if notice else ""
        body = f"""
        {notice_html}
        <div class='card'><h2>Log in</h2>
        <form method='post' action='/login'>
          <p><input name='email' placeholder='email' value='{_esc(email)}' required></p>
          <p><input name='password' type='password' placeholder='password' required></p>
          <p><button type='submit'>Log in</button>
             <button type='button' disabled title='OAuth callback domain pending'>Sign in with Google</button>
             <button type='button' disabled title='developer app review pending'>Sign in with Facebook</button></p>
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
        if path == "/register":
            self._post_register()
        elif path == "/login":
            self._post_login()
        elif path == "/new":
            self._post_new()
        elif path == "/verify-domain":
            self._post_verify_domain()
        else:
            self._html("<h1>Not found</h1>", title="404")

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
        token = secrets.token_urlsafe(32)
        exp = (datetime.utcnow() + timedelta(hours=VERIFY_HOURS)).isoformat()
        self.conn.execute(
            """INSERT INTO client(email, password_hash, verified,
               verify_token, verify_expires)
               VALUES(?,?,0,?,?)""",
            (email, hash_password(password), token, exp))
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

    @staticmethod
    def _nav_links() -> str:
        return ("<a href='/dashboard' style='color:inherit'>dashboard</a> "
                "<a href='/reports' style='color:inherit'>reports</a> "
                "<a href='/new' style='color:inherit'>new project</a> "
                "<form method='post' action='/logout' style='display:inline'>"
                "<button style='padding:2px 8px'>Log out</button></form>")

    def _post_new(self):
        client = self._require_client()
        if not client:
            self._redirect("/")
            return
        f = self._form()
        domain = normalize_domain(f.get("domain"))
        dev = os.environ.get("PORTAL_DEV") == "1"
        err = validate_domain(domain, dev=dev)
        if err:
            self._page_new(notice=err)
            return
        if _domain_claimed(self.conn, domain, client["id"]):
            self._page_new(notice="This domain is already claimed by "
                                  "another account.")
            return
        dup = self.conn.execute(
            "SELECT 1 FROM project_request WHERE client_id=? AND domain=?",
            (client["id"], domain)).fetchone()
        if dup:
            self._page_new(notice="You already have a request for this "
                                  "domain.")
            return
        tags = [t.strip() for t in (f.get("tags") or "").split(",")
                if t.strip()]
        token = "osched-" + secrets.token_urlsafe(16)
        self.conn.execute(
            """INSERT INTO project_request(client_id, domain, site_title,
               site_summary, contact_email, tags_json, verify_method,
               verify_token, verify_state)
               VALUES(?,?,?,?,?,?, 'meta', ?, 'draft')""",
            (client["id"], domain, (f.get("site_title") or "").strip(),
             (f.get("site_summary") or "").strip(),
             (f.get("contact_email") or "").strip(),
             json.dumps(tags), token))
        self.conn.commit()
        self._page_new()

    def _post_verify_domain(self):
        client = self._require_client()
        if not client:
            self._redirect("/")
            return
        rid = (self._form().get("id") or "")
        req = self.conn.execute(
            """SELECT * FROM project_request WHERE id=? AND client_id=?
               AND verify_state='draft'""",
            (rid, client["id"])).fetchone()
        if not req:
            self._page_new(notice="No pending request found.")
            return
        if _domain_claimed(self.conn, req["domain"], client["id"]):
            self._page_new(notice="This domain was claimed by another "
                                  "account in the meantime.")
            return
        try:
            found = fetch_meta_token(_verify_url(req["domain"]))
        except Exception as e:  # noqa: BLE001
            self._page_new(notice=f"Could not fetch {_esc(req['domain'])}: "
                                  f"{_esc(str(e)[:120])}")
            return
        if found != req["verify_token"]:
            self._page_new(notice="Verification tag not found on your "
                                  "homepage — add it to <head> and retry.")
            return
        result = activate_request(self.conn, req)
        note = (f"{_esc(req['domain'])} verified — project active with "
                f"{result['credits']} trial live links."
                if result["trial"] else
                f"{_esc(req['domain'])} verified — project active "
                "(trial already used on this account).")
        self._page_new(notice=note)


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def serve(cfg: dict, data_root: pathlib.Path, host: str, port: int) -> int:
    # single-threaded server: portal traffic is tiny, and one connection
    # in one thread sidesteps sqlite cross-thread restrictions entirely
    conn = db_connect(data_root)
    conn.executescript(PORTAL_SCHEMA)
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
