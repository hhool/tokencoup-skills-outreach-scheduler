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


def test_contact_form_with_url_field_is_not_a_submission_form():
    """pcastuces /soumettre.htm: Nom+E-mail+URL+commentaire posting to
    erreur404.asp is a contact handler, not a listing endpoint."""
    html = """<form action="https://www.pcastuces.com/erreur404.asp">
      <input name="Nom"><input name="E-mail"><input name="URL">
      <textarea name="commentaire"></textarea>
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_hidden_contact_action_is_not_a_submission_form():
    """smoothdirectory: website_url+email to admin-post.php with hidden
    action=cdt_contact is a subscribe widget, not a listing submit."""
    html = """<form action="/wp-admin/admin-post.php">
      <input type="hidden" name="action" value="cdt_contact">
      <input name="website_url"><input name="email">
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_real_listing_form_with_contact_words_in_path_still_works():
    """Guard must not reject a genuine add-site form whose action contains a
    benign word — only explicit contact/subscribe handlers are excluded."""
    html = """<form action="/site/submit-listing">
      <input name="site_url"><input name="title"><input name="email">
    </form>"""
    ok, fields = probe._looks_like_submit_form(html)
    assert ok and "site_url" in fields


def test_site_search_form_with_hidden_siteurl_is_not_a_submission_form():
    """jayisgames.com/game-submit: Google CSE widget carries hidden `siteurl`
    and `ref` next to a visible `q` box — it searches, it does not publish."""
    html = """<form id="cse-search-box" action="https://jayisgames.com/search/">
      <input type="hidden" value="partner-pub" name="cx"/>
      <input type="text" size="24" name="q"/>
      <input type="submit" value="Search" name="sa"/>
      <input type="hidden" name="siteurl" value="jayisgames.com/games/"/>
      <input type="hidden" name="ref" value="jayisgames.com/"/>
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_hidden_only_destination_field_is_not_a_submission_form():
    """A destination we cannot type into is plumbing, not a submit field."""
    html = """<form action="/newsletter">
      <input type="hidden" name="website"><input name="email">
    </form>"""
    ok, _ = probe._looks_like_submit_form(html)
    assert ok is False


def test_classic_directory_form_still_accepted():
    """cam111.com/url-submit: name+siteurl+email+class with hidden action=add."""
    html = """<form action="/url-submit/" method="post" name="siteadd"
      onSubmit="return Validator.Validate(this,3)">
      <input name="name" type="text" dataType="Require">
      <input name="siteurl" type="text" value="http://">
      <input name="email" type="text" dataType="Email">
      <input name="class" type="text">
      <input type="hidden" name="action" value="add"/>
      <input type="submit" value="Submit" name="dosubmit">
    </form>"""
    ok, fields = probe._looks_like_submit_form(html)
    assert ok and "siteurl" in fields


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
    status, _, _ = executor._post_form(
        "https://dir.example/add",
        ["__VIEWSTATE", "siteURLInputN2"], {"url": "https://t.example/"}, 5.0,
        {"__VIEWSTATE": "abc123"})
    assert status == 200
    assert "__VIEWSTATE=abc123" in captured["data"]
    assert "siteURLInputN2=https%3A%2F%2Ft.example%2F" in captured["data"]


# --------------------------------------------------------------------------
# POST verdict: 2xx alone is not success — copy recognition + form re-render
# --------------------------------------------------------------------------

def test_copy_verdict_success_and_already():
    assert probe.copy_verdict(
        "<p>Thank you for your submission!</p>")["success"]
    assert probe.copy_verdict(
        "<p>Your link has been added and is pending review.</p>")["success"]
    v = probe.copy_verdict("<p>This URL already exists in our directory.</p>")
    assert v["already"] and not v["success"]


def test_copy_verdict_ignores_script_error_strings():
    """JS error strings in page chrome must not read as failure copy."""
    html = ("<script>if (x) { throw new Error('invalid'); }</script>"
            "<p>Thanks, your submission was received.</p>")
    v = probe.copy_verdict(html)
    assert v["success"] and not v["failure"]


def _form_row():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(entry_url TEXT, url TEXT, captcha_type TEXT)")
    conn.execute("INSERT INTO t VALUES('https://dir.example/add',"
                 "'https://dir.example','none')")
    return conn.execute("SELECT * FROM t").fetchone()


def _run_exec_form(monkeypatch, body: str, fields=("site_url", "title")):
    monkeypatch.setattr(executor, "_parse_live_form",
                        lambda entry, timeout:
                        ("https://dir.example/add", list(fields), "none", {}))
    monkeypatch.setattr(executor, "_post_form",
                        lambda *a, **kw: (200, body, ""))
    return executor.exec_form(
        None, {}, {"domain": "c.example"}, _form_row(),
        {"url": "https://c.example/", "title": "C", "description": "d",
         "email": ""}, dry_run=False)


def test_2xx_with_success_copy_is_submitted(monkeypatch):
    out = _run_exec_form(
        monkeypatch, "<p>Thank you for your submission. We will review it.</p>")
    assert out["state"] == "submitted"
    assert "success" in out["note"] or "Thank" in out["note"]


def test_2xx_with_failure_copy_is_human(monkeypatch):
    out = _run_exec_form(
        monkeypatch, "<p>Error: please enter a valid URL.</p>")
    assert out["state"] == "human"
    assert "failure copy" in out["note"]


def test_2xx_form_rerendered_is_human(monkeypatch):
    """Silent rejection: 200 with the same submission form redrawn."""
    out = _run_exec_form(monkeypatch, """
        <form action="/add" method="post">
          <input name="site_url"><input name="title">
        </form>""")
    assert out["state"] == "human"
    assert "re-rendered" in out["note"]


def test_2xx_no_signals_stays_submitted(monkeypatch):
    """Redirected to a landing page with no copy and no form: trust 2xx."""
    out = _run_exec_form(monkeypatch, "<html><body><h1>Directory</h1></body></html>")
    assert out["state"] == "submitted"


def test_already_listed_is_submitted(monkeypatch):
    out = _run_exec_form(
        monkeypatch, "<p>That URL already exists in our directory.</p>")
    assert out["state"] == "submitted" and "already" in out["note"]


# --------------------------------------------------------------------------
# cua_primary: project-level opt-in routes form plans to the browser channel
# --------------------------------------------------------------------------

def test_cua_primary_routes_no_field_pages_to_cua(monkeypatch):
    monkeypatch.setattr(executor, "_parse_live_form",
                        lambda entry, timeout: ("https://dir.example", [],
                                                "none", {}))
    out = executor.exec_form(
        None, {}, {"domain": "c.example", "delivery": {"cua_primary": True}},
        _form_row(), {"url": "https://c.example/"}, dry_run=False)
    assert out["state"] == "cua"
    assert "JS-rendered" in out["note"]


def test_cua_primary_routes_submittable_form_to_cua(monkeypatch):
    monkeypatch.setattr(executor, "_parse_live_form",
                        lambda entry, timeout:
                        ("https://dir.example/add",
                         ["site_url", "title"], "none", {}))
    posted = []
    monkeypatch.setattr(executor, "_post_form",
                        lambda *a, **kw: posted.append(a) or (200, "", ""))
    out = executor.exec_form(
        None, {}, {"domain": "c.example", "delivery": {"cua_primary": True}},
        _form_row(), {"url": "https://c.example/"}, dry_run=False)
    assert out["state"] == "cua"
    assert not posted                    # static POST never happened


def test_without_cua_primary_static_post_still_runs(monkeypatch):
    monkeypatch.setattr(executor, "_parse_live_form",
                        lambda entry, timeout:
                        ("https://dir.example/add",
                         ["site_url", "title"], "none", {}))
    posted = []
    monkeypatch.setattr(executor, "_post_form",
                        lambda *a, **kw: posted.append(a) or (200, "", ""))
    out = executor.exec_form(
        None, {}, {"domain": "c.example"},
        _form_row(), {"url": "https://c.example/"}, dry_run=False)
    assert out["state"] == "submitted" and len(posted) == 1


def test_cua_primary_respects_unknown_captcha(monkeypatch):
    monkeypatch.setattr(executor, "_parse_live_form",
                        lambda entry, timeout:
                        ("https://dir.example/add", ["site_url"],
                         "some_weird_captcha", {}))
    out = executor.exec_form(
        None, {}, {"domain": "c.example", "delivery": {"cua_primary": True}},
        _form_row(), {"url": "https://c.example/"}, dry_run=False)
    assert out["state"] == "human"       # captcha gate stays above cua_primary


# --------------------------------------------------------------------------
# mail quality gate: never send an unpersonalised pitch to a non-prospect
# --------------------------------------------------------------------------

def test_mail_gate_rejects_news_daily_placeholder():
    row = {"submit_type": "pitch"}
    reason = executor.mail_quality_gate(
        row, {"category": "general", "sender_name": "***REMOVED-PRIVATE-DOMAIN***"},
        "Hi,\nWe run {{site_name}}")
    assert "not a pitch" in reason or "placeholder" in reason


def test_mail_gate_allows_personalised_prospect():
    row = {"submit_type": "pitch"}
    reason = executor.mail_quality_gate(
        row, {"category": "browser-games", "sender_name": "Alex Chen"},
        "Hi,\nWe run AZ Games")
    assert reason == ""


def test_mail_gate_rejects_unrendered_template():
    row = {"submit_type": "pitch"}
    reason = executor.mail_quality_gate(
        row, {"category": "browser-games", "sender_name": "Alex Chen"},
        "Hello {{name}}")
    assert "unrendered" in reason


def test_mail_gate_rejects_domain_as_sender():
    row = {"submit_type": "pitch"}
    reason = executor.mail_quality_gate(
        row, {"category": "browser-games", "sender_name": "***REMOVED-PRIVATE-DOMAIN***"},
        "Hi")
    assert "domain, not a person" in reason


def test_turnstile_detected_from_widget_field_name():
    """betalist.com/submissions/new ships `cf_turnstile_widget` as a form
    field — invisible in markup but it IS a captcha gate."""
    html = """<form action="/submissions">
      <input name="submission[startup_attributes][website_url]">
      <input name="email"><input type="hidden" name="cf_turnstile_widget">
    </form>"""
    assert probe._detect_captcha(html) == "turnstile"
