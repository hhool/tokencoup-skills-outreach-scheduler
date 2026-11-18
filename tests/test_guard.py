"""Guards against free-trial abuse (src/osched/guard.py).

Run: python3 -m pytest tests/test_guard.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import portal                      # noqa: E402
from osched import scheduler                   # noqa: E402
from osched.db import connect                  # noqa: E402
from osched.guard import (allowance, canonical_email, credit_cap,  # noqa: E402
                          live_count, normalize_host, registrable_domain,
                          signup_blocked, trial_verdict)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path)
    c.executescript(portal.PORTAL_SCHEMA)
    portal._migrate_portal(c)
    return c


def _client(conn, email, ip="203.0.113.1"):
    conn.execute(
        "INSERT INTO client(email, password_hash, verified, signup_ip) "
        "VALUES(?, 'x', 1, ?)", (email, ip))
    conn.commit()
    return conn.execute("SELECT * FROM client WHERE email=?",
                        (email,)).fetchone()


def _request(conn, client, domain, state="draft"):
    conn.execute(
        """INSERT INTO project_request(client_id, domain, verify_token,
             verify_state) VALUES(?,?,?,'draft')""",
        (client["id"], domain, "osched-" + domain))
    rid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if state != "draft":
        conn.execute("UPDATE project_request SET verify_state=? WHERE id=?",
                     (state, rid))
    conn.commit()
    return conn.execute("SELECT * FROM project_request WHERE id=?",
                        (rid,)).fetchone()


# -- normalisation ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("User.Name+trial@googlemail.com", "username@gmail.com"),
    ("u.s.e.r@GMAIL.COM", "user@gmail.com"),
    ("a+1@Example.COM ", "a@example.com"),
    ("plain@example.com", "plain@example.com"),
    ("not-an-email", "not-an-email"),
])
def test_canonical_email(raw, expected):
    assert canonical_email(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://www.Foo.COM:443/path?q=1", "foo.com"),
    ("shop.foo.co.uk", "foo.co.uk"),
    ("a.b.foo.com.", "foo.com"),
    ("WWW.FOO.COM", "foo.com"),
    ("foo.com", "foo.com"),
    ("sub.domain.example.io", "example.io"),
    ("exämple.de", "xn--exmple-cua.de"),
])
def test_normalize_and_registrable(raw, expected):
    assert registrable_domain(raw) == expected


def test_ip_literal_is_not_collapsed():
    assert registrable_domain("192.0.2.10") == "192.0.2.10"


# -- trial decision --------------------------------------------------------
def _grant(conn, client, domain, ip):
    from osched.guard import record_trial
    record_trial(conn, client_id=client["id"], email=client["email"],
                 domain=domain, ip=ip)


def test_fresh_client_gets_trial(conn):
    c = _client(conn, "fresh@example.com")
    ok, reason = trial_verdict(conn, c["id"], c["email"], "shop.example.com",
                               "203.0.113.7")
    assert (ok, reason) == (True, "granted")


def test_alias_cannot_farm_trials(conn):
    c1 = _client(conn, "user@gmail.com")
    _grant(conn, c1, "one.com", "203.0.113.10")
    c2 = _client(conn, "u.ser+trial@gmail.com", ip="198.51.100.4")
    ok, reason = trial_verdict(conn, c2["id"], c2["email"], "two.com",
                               "198.51.100.4")
    assert ok is False and reason == "email_reused"


def test_subdomain_cannot_remint_trial(conn):
    c1 = _client(conn, "owner@example.com")
    _grant(conn, c1, "example.com", "203.0.113.11")
    c2 = _client(conn, "owner2@example.org", ip="198.51.100.5")
    ok, reason = trial_verdict(conn, c2["id"], c2["email"],
                               "shop.example.com", "198.51.100.5")
    assert ok is False and reason == "site_reused"


def test_same_network_cannot_remint_trial(conn):
    c1 = _client(conn, "a@example.com", ip="203.0.113.20")
    _grant(conn, c1, "site-one.com", "203.0.113.20")
    c2 = _client(conn, "b@example.net", ip="203.0.113.20")
    ok, reason = trial_verdict(conn, c2["id"], c2["email"], "site-two.com",
                               "203.0.113.20")
    assert ok is False and reason == "ip_reused"


def test_same_account_cannot_get_second_trial(conn):
    c = _client(conn, "solo@example.com")
    _grant(conn, c, "one.com", "203.0.113.30")
    ok, reason = trial_verdict(conn, c["id"], c["email"], "two.com",
                               "203.0.113.31")
    assert ok is False and reason == "client_used"


def test_activate_withholds_credits_and_records_reason(conn):
    c1 = _client(conn, "first@example.com")
    req1 = _request(conn, c1, "brand.com")
    res1 = portal.activate_request(conn, req1, ip="203.0.113.40")
    assert res1["trial"] is True and res1["credits"] == 3

    c2 = _client(conn, "second@example.com", ip="198.51.100.9")
    req2 = _request(conn, c2, "brand.com")          # same site, new account
    res2 = portal.activate_request(conn, req2, ip="198.51.100.9")
    assert res2["trial"] is False and res2["credits"] == 0
    assert res2["reason"] in ("site_reused", "site_claimed")

    row = conn.execute("SELECT quota_total, quota_type, trial_note "
                       "FROM project_request WHERE id=?",
                       (req2["id"],)).fetchone()
    assert row["quota_total"] == 0
    assert row["quota_type"] == "paid"
    assert row["trial_note"] in ("site_reused", "site_claimed")


def test_signup_rate_limit(conn):
    for i in range(5):
        _client(conn, f"burst{i}@example.com", ip="198.51.100.77")
    assert signup_blocked(conn, "198.51.100.77") != ""
    assert signup_blocked(conn, "198.51.100.78") == ""


# -- credit enforcement ----------------------------------------------------
def _project(conn, domain):
    """Reuse the project row activate_request may already have created."""
    row = conn.execute("SELECT * FROM project WHERE domain=?",
                       (domain,)).fetchone()
    if row:
        return row
    conn.execute("INSERT INTO project(domain, tags_json) VALUES(?, '[]')",
                 (domain,))
    conn.commit()
    return conn.execute("SELECT * FROM project WHERE domain=?",
                        (domain,)).fetchone()


def _pool_row(conn, domain):
    conn.execute(
        """INSERT INTO pool(url, domain, source_kind, source_file, source_row,
             submit_type) VALUES(?,?, 'test', 'tests/test_guard.py', 0, 'auto')""",
        (f"https://{domain}/", domain))
    conn.commit()
    return conn.execute("SELECT id FROM pool WHERE domain=?",
                        (domain,)).fetchone()["id"]


def _confirmed_matches(conn, project, n):
    for i in range(n):
        pid = _pool_row(conn, f"dir{i}-{project['domain']}.test")
        conn.execute("INSERT INTO match(project_id, pool_id, score, state) "
                     "VALUES(?,?,?, 'confirmed')",
                     (project["id"], pid, 90 - i))
    conn.commit()


def test_uncapped_project_schedules_freely(conn):
    pr = _project(conn, "internal.test")
    _confirmed_matches(conn, pr, 5)
    out = scheduler.generate_plan(conn, {"_id": pr["id"],
                                         "domain": "internal.test",
                                         "daily_quota": 10}, days=7)
    assert out["scheduled"] == 5 and credit_cap(conn, pr["id"]) is None


def test_capped_project_schedules_only_remaining_credits(conn):
    c = _client(conn, "capped@example.com")
    req = _request(conn, c, "capped.test")
    portal.activate_request(conn, req, ip="203.0.113.55")
    pr = _project(conn, "capped.test")
    conn.execute("UPDATE project_request SET project_id=? WHERE id=?",
                 (pr["id"], req["id"]))
    conn.commit()
    _confirmed_matches(conn, pr, 6)

    out = scheduler.generate_plan(conn, {"_id": pr["id"],
                                         "domain": "capped.test",
                                         "daily_quota": 10}, days=7)
    assert out["scheduled"] == 3          # trial is 3 links, not 6

    # a second run must not add more while those 3 are still in flight
    out2 = scheduler.generate_plan(conn, {"_id": pr["id"],
                                          "domain": "capped.test",
                                          "daily_quota": 10}, days=7)
    assert out2["scheduled"] == 0 and out2.get("blocked") == "quota_exhausted"


def test_execution_stops_at_cap(conn):
    c = _client(conn, "exec@example.com")
    req = _request(conn, c, "exec.test")
    portal.activate_request(conn, req, ip="203.0.113.56")
    pr = _project(conn, "exec.test")
    conn.execute("UPDATE project_request SET project_id=? WHERE id=?",
                 (pr["id"], req["id"]))
    conn.commit()
    _confirmed_matches(conn, pr, 8)
    scheduler.generate_plan(conn, {"_id": pr["id"], "domain": "exec.test",
                                   "daily_quota": 10}, days=1)
    due = scheduler.due_today(conn, pr["id"], today="2999-01-01")
    assert len(due) == 3                  # hard stop, queued rows wait

    # after one link is delivered, allowance shrinks accordingly
    conn.execute("UPDATE plan SET state='live' WHERE id=?",
                 (due[0]["id"],))
    conn.commit()
    assert live_count(conn, pr["id"]) == 1
    assert allowance(conn, pr["id"]) == 0  # 2 queued + 1 live = 3


def test_internal_projects_are_uncapped(conn):
    c = _client(conn, "ops@example.com")
    req = _request(conn, c, "own-site.test")
    pr = _project(conn, "own-site.test")
    conn.execute("UPDATE project_request SET project_id=?, verify_state='active',"
                 " quota_type='internal' WHERE id=?", (pr["id"], req["id"]))
    conn.commit()
    _confirmed_matches(conn, pr, 4)
    out = scheduler.generate_plan(conn, {"_id": pr["id"],
                                         "domain": "own-site.test",
                                         "daily_quota": 10}, days=7)
    assert credit_cap(conn, pr["id"]) is None
    assert out["scheduled"] == 4


def test_quota_used_synced_on_live(conn):
    from osched.guard import sync_quota_used
    c = _client(conn, "sync@example.com")
    req = _request(conn, c, "sync.test")
    portal.activate_request(conn, req, ip="203.0.113.57")
    pr = _project(conn, "sync.test")
    conn.execute("UPDATE project_request SET project_id=? WHERE id=?",
                 (pr["id"], req["id"]))
    _confirmed_matches(conn, pr, 2)
    scheduler.generate_plan(conn, {"_id": pr["id"], "domain": "sync.test",
                                   "daily_quota": 5}, days=1)
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM plan WHERE project_id=?", (pr["id"],))]
    conn.execute("UPDATE plan SET state='live' WHERE id=?", (ids[0],))
    conn.commit()
    sync_quota_used(conn, pr["id"])
    used = conn.execute("SELECT quota_used FROM project_request WHERE id=?",
                        (req["id"],)).fetchone()["quota_used"]
    assert used == 1
