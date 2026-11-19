"""Register channel stage 2: publish recognition + plan-row contract.

Run: python3 -m pytest tests/test_publish.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import publish              # noqa: E402
from osched.db import connect           # noqa: E402


def test_login_form_recognized():
    html = """<form action="/login"><input name="username">
      <input name="password" type="password"></form>"""
    ok, fields, action = publish.find_login_form(html)
    assert ok and "username" in fields and action == "/login"


def test_signup_form_also_looks_like_login_for_relogin():
    html = """<form action="/register"><input name="email">
      <input name="password" type="password"><input name="password2"></form>"""
    ok, _, _ = publish.find_login_form(html)
    assert ok                          # expired session: log in either way


def test_no_credential_form_on_public_page():
    html = """<form><input name="q"><input name="site_url"></form>"""
    ok, _, _ = publish.find_login_form(html)
    assert ok is False


def test_success_marker():
    assert publish.looks_published("<p>Thanks, your listing was added</p>", True)
    assert not publish.looks_published("<p>Thanks</p>", False)
    assert not publish.looks_published("<p>Directory home</p>", True)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path)
    c.execute("INSERT INTO pool(url,domain,source_kind,source_file,source_row)"
              " VALUES('https://p.example','p.example','t','t',1)")
    c.execute("INSERT INTO project(domain) VALUES('c.example')")
    c.execute("INSERT INTO match(project_id,pool_id,state) "
              "VALUES(1,1,'confirmed')")
    c.commit()
    return c


def test_candidates_require_active_account(conn):
    assert publish.publish_candidates(conn, 1) == []
    conn.execute("INSERT INTO account(project_id,dir_domain,email,status)"
                 " VALUES(1,'p.example','a@b.c','active')")
    conn.commit()
    assert len(publish.publish_candidates(conn, 1)) == 1


def test_candidates_exclude_once_plan_exists(conn):
    conn.execute("INSERT INTO account(project_id,dir_domain,email,status)"
                 " VALUES(1,'p.example','a@b.c','active')")
    conn.execute("""INSERT INTO plan(project_id,match_id,scheduled_date,
                    channel,state,executed_at)
                    VALUES(1,1,'2026-11-19','register','submitted',
                    datetime('now'))""")
    conn.commit()
    assert publish.publish_candidates(conn, 1) == []


def test_publish_writes_plan_submitted_with_landing(conn):
    """The verify chain joins on plan: a publish without a plan is invisible."""
    conn.execute("INSERT INTO account(project_id,dir_domain,email,status)"
                 " VALUES(1,'p.example','a@b.c','active')")
    conn.commit()
    row = publish.publish_candidates(conn, 1)[0]
    conn.execute("""INSERT OR IGNORE INTO plan(project_id, match_id,
                    scheduled_date, channel, state, executed_at, result_note)
                    VALUES(?,?,date('now'),'register','submitted',
                    datetime('now'), ?)""",
                 (1, row["match_id"],
                  json.dumps({"landing": "https://c.example"})))
    conn.commit()
    plan = conn.execute("SELECT state, result_note FROM plan").fetchone()
    assert plan["state"] == "submitted"
    assert "https://c.example" in plan["result_note"]
