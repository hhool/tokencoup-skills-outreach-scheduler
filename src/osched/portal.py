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
import secrets
import sqlite3
import sys
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
               AND lv.checked_at >= date('now','-30 days')) AS live30
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


def render_dashboard(conn, client) -> str:
    rows = _client_projects(conn, client["id"])
    if rows:
        quota_rows = "".join(
            f"<tr><td>{_esc(r['domain'])}</td>"
            f"<td>{_esc(r['quota_type'])}</td>"
            f"<td>{r['live']} / {r['quota_total']}</td>"
            f"<td>{r['inflight']}</td><td>{r['pending']}</td>"
            f"<td>{r['live30']}</td></tr>"
            for r in rows)
        quota_card = (
            "<div class='card'><h2>Quota consumption</h2>"
            "<table><tr><th>project</th><th>type</th><th>live / quota</th>"
            "<th>in flight</th><th>pending</th><th>live (30d)</th></tr>"
            + quota_rows + "</table>"
            "<p class='muted'>Only confirmed live links consume credits; "
            "in-flight and never-live items are not billed.</p></div>")
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
            "<div class='card'><h2>Promotion effect</h2>"
            "<table><tr><th>project</th><th>placement domain</th><th>DR</th>"
            "<th>confirmed</th></tr>" + led_rows + "</table>"
            "<p class='muted'>Tier 1 evidence (live-link ledger). DR band and "
            "funnel trend land with the next portal phase.</p></div>")
    else:
        quota_card = ("<div class='card'><h2>Quota consumption</h2>"
                      "<p class='muted'>No active project yet. Self-service "
                      "project creation with trial credits ships next "
                      "(client-portal §1.5).</p></div>")
        effect_card = ("<div class='card'><h2>Promotion effect</h2>"
                       "<p class='muted'>Nothing to show yet.</p></div>")
    return (f"<h1>Welcome, {_esc(client['email'])}</h1>"
            + quota_card + effect_card)


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
        nav = ("<a href='/dashboard' style='color:inherit'>dashboard</a> "
              "<form method='post' action='/logout' style='display:inline'>"
              "<button style='padding:2px 8px'>Log out</button></form>")
        self._html(render_dashboard(self.conn, client),
                   title="dashboard", nav=nav)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/register":
            self._post_register()
        elif path == "/login":
            self._post_login()
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
