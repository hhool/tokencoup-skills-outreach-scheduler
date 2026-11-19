"""Register channel stage 1: signup form recognition + candidate selection.

Run: python3 -m pytest tests/test_signup.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import signup                 # noqa: E402
from osched.db import connect             # noqa: E402


def test_signup_form_recognized():
    html = """<form action="/register" method="post">
      <input name="username"><input name="email">
      <input name="password" type="password">
      <input name="password2" type="password">
    </form>"""
    ok, fields, action = signup.find_signup_form(html)
    assert ok and "username" in fields and action == "/register"


def test_login_form_rejected():
    html = """<form action="/login" method="post">
      <input name="username"><input name="password" type="password">
    </form>"""
    ok, _, _ = signup.find_signup_form(html)
    assert ok is False          # 2 fields only + login action


def test_login_action_with_extra_fields_rejected():
    html = """<form action="/sign-in" method="post">
      <input name="user_login"><input name="pwd"><input name="remember">
    </form>"""
    ok, _, _ = signup.find_signup_form(html)
    assert ok is False


def test_password_only_form_rejected():
    html = """<form action="/reset">
      <input name="password" type="password">
      <input name="password2" type="password">
    </form>"""
    ok, _, _ = signup.find_signup_form(html)
    assert ok is False          # no identity field


def test_captcha_hints():
    assert signup.signup_captcha_hint("<div class=g-recaptcha>") == "recaptcha"
    assert signup.signup_captcha_hint("cf-turnstile") == "turnstile"
    assert signup.signup_captcha_hint("<p>hello</p>") == "none"


def test_cloudflare_managed_challenge_detected():
    # alternativeto serves "Just a moment..." with challenges.cloudflare.com
    # but no literal 'turnstile' string — must still pause the handoff.
    html = "<title>Just a moment...</title><script src='https://challenges.cloudflare.com/turnstile'></script>"
    assert signup.signup_captcha_hint(html) == "turnstile"
    assert signup.signup_captcha_hint("<html>cf_chl_opt={...}</html>") == "turnstile"
    assert signup.signup_captcha_hint("<title>Just a moment...</title>") == "none"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path)
    cur = c.execute("INSERT INTO pool(url,domain,source_kind,source_file,"
                    "source_row,submit_type) VALUES('https://p.example',"
                    "'p.example','t','t',1,'register')")
    c.execute("INSERT INTO project(domain) VALUES('c.example')")
    c.execute("INSERT INTO match(project_id,pool_id,state) "
              "VALUES(1,1,'confirmed')")
    c.commit()
    return c


def test_candidates_exclude_domains_with_accounts(conn):
    cands = signup.signup_candidates(conn, 1)
    assert [r["pool_domain"] for r in cands] == ["p.example"]
    conn.execute("INSERT INTO account(project_id,dir_domain,email,status) "
                 "VALUES(1,'p.example','a@b.c','active')")
    conn.commit()
    assert signup.signup_candidates(conn, 1) == []


def test_candidates_exclude_unconfirmed(conn):
    conn.execute("UPDATE match SET state='pending'")
    conn.commit()
    assert signup.signup_candidates(conn, 1) == []


def test_password_generation():
    p1, p2 = signup.gen_password(), signup.gen_password()
    assert p1 != p2 and len(p1) >= 16


def test_site_root_strips_deep_entry_path():
    assert (signup.site_root("https://alternativeto.net/manage/add-app")
            == "https://alternativeto.net")
    assert signup.site_root("sitelike.org/add-site") == "https://sitelike.org"


def test_signup_candidates_use_site_root_not_deep_entry():
    urls = signup.signup_candidate_urls(
        "https://alternativeto.net/manage/add-app", "https://alternativeto.net/")
    # static discovery returned only the root index -> fall through to the
    # entry + root-relative signup paths; never /manage/add-app/register
    assert urls[0] == "https://alternativeto.net/manage/add-app"
    assert "https://alternativeto.net/register" in urls
    assert not any("/manage/add-app/" in u for u in urls)


def test_discovered_signup_page_wins():
    urls = signup.signup_candidate_urls(
        "https://sitelike.org/add-site", "https://sitelike.org/signup")
    assert urls == ["https://sitelike.org/signup"]


def test_cap_after_handoff_clears_on_solve():
    # widget markup still present, but .go means the human solved it
    assert signup.cap_after_handoff(True, "cf-turnstile script") == "none"
    assert signup.cap_after_handoff(False, "cf-turnstile script") == "turnstile"
