"""Execution gates: form heuristic + which plans may auto-execute (§7).

Run: python3 -m pytest tests/test_execution_gates.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import executor, probe                    # noqa: E402
from osched import portal                             # noqa: E402
from osched.db import connect                         # noqa: E402


# --------------------------------------------------------------------------
# probe._looks_like_submit_form — a submission form must accept a URL
# --------------------------------------------------------------------------

def test_url_field_is_a_submission_form():
    html = """<form action="/add" method="post">
      <input name="site_url"><input name="title"><input name="description">
    </form>"""
    ok, fields = probe._looks_like_submit_form(html)
    assert ok and "site_url" in fields


def test_signup_form_with_password_is_not_a_submission_form():
    html = """<form action="/new-blog">
      <input name="site_name"><input name="password">
      <input name="recaptcha_response">
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_login_action_is_not_a_submission_form():
    html = """<form action="/wp-login.php">
      <input name="url"><input name="email">
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_search_and_newsletter_boxes_are_not_submission_forms():
    html = """<form action="/search"><input name="q"></form>
              <form action="/subscribe"><input name="email"></form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_title_only_form_is_not_a_submission_form():
    """`site_name` + `title` matched the old heuristic; it is a signup wizard."""
    html = """<form><input name="site_name"><input name="title"></form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


# --------------------------------------------------------------------------
# run_execution gates
# --------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path)
    c.executescript(portal.PORTAL_SCHEMA)
    portal._migrate_portal(c)
    c.execute("INSERT INTO project(domain, tags_json) VALUES('t.example','[]')")
    pid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    c.commit()
    return c, pid


def _plan(conn, pid, *, submit_type, channel, email=""):
    conn.execute(
        """INSERT INTO pool(domain, url, source_kind, source_file, source_row,
             submit_type, status, contact_email)
           VALUES(?, ?, 'test', 'test', 0, ?, 'active', ?)""",
        (f"d{conn.total_changes}.example", "https://d.example/",
         submit_type, email))
    pool_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """INSERT INTO match(project_id, pool_id, score, state)
           VALUES(?, ?, 0.9, 'confirmed')""", (pid, pool_id))
    match_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """INSERT INTO plan(match_id, project_id, channel, state,
             scheduled_date, template_id)
           VALUES(?,?,?,'queued', date('now'), 'dir-submit')""",
        (match_id, pid, channel))
    conn.commit()


def _project(**over):
    p = {"_id": 1, "domain": "t.example", "channels": ["form", "email"],
         "pages": [], "templates": []}
    p.update(over)
    return p


def test_unknown_submit_type_never_auto_executes(conn):
    c, pid = conn
    _plan(c, pid, submit_type="unknown", channel="form")
    stats = executor.run_execution(c, {}, _project())
    assert stats["human"] == 1
    note = c.execute("SELECT result_note FROM plan").fetchone()[0]
    assert "needs how" in note


def test_email_is_manual_without_explicit_opt_in(conn):
    c, pid = conn
    _plan(c, pid, submit_type="pitch", channel="email",
          email="editor@d.example")
    stats = executor.run_execution(c, {}, _project())
    assert stats["human"] == 1 and stats["sent"] == 0
    assert "R-520" in c.execute("SELECT result_note FROM plan").fetchone()[0]


def test_email_opt_in_reaches_the_smtp_layer(conn, monkeypatch):
    """With the opt-in the row is no longer short-circuited by the gate."""
    c, pid = conn
    _plan(c, pid, submit_type="pitch", channel="email",
          email="editor@d.example")
    seen = {}

    def fake_email(*a, **kw):
        seen["called"] = True
        return {"state": "human", "note": "no SMTP host configured"}

    monkeypatch.setattr(executor, "exec_email", fake_email)
    executor.run_execution(c, {}, _project(delivery={"email_auto": True}))
    assert seen.get("called") is True


# --------------------------------------------------------------------------
# hidden state fields (ASP.NET viewstate, CSRF tokens) must be echoed back
# --------------------------------------------------------------------------

def test_state_fields_capture_viewstate_and_csrf():
    html = """<form method="post" action="/add-site">
      <input type="hidden" name="__VIEWSTATE" value="abc123">
      <input type="hidden" name="__VIEWSTATEGENERATOR" value="XY">
      <input type="hidden" name="csrf_token" value="t0k">
      <input name="siteURLInputN2">
    </form>"""
    st = probe.state_fields(html)
    assert st["__VIEWSTATE"] == "abc123"
    assert st["csrf_token"] == "t0k"
    assert "siteURLInputN2" not in st


def test_post_form_echoes_state_values(monkeypatch):
    """Posting empty viewstate is an instant ASP.NET rejection."""
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data.decode()
        return FakeResp()

    monkeypatch.setattr(executor.urllib.request, "urlopen", fake_urlopen)
    status, _ = executor._post_form(
        "https://dir.example/add",
        ["__VIEWSTATE", "siteURLInputN2"], {"url": "https://t.example/"}, 5.0,
        {"__VIEWSTATE": "abc123"})
    assert status == 200
    assert "__VIEWSTATE=abc123" in captured["data"]
    assert "siteURLInputN2=https%3A%2F%2Ft.example%2F" in captured["data"]
