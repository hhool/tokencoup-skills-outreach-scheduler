"""Evidence layer: link_check time series + public link receipts (B1).

Run: python3 -m pytest tests/test_evidence.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import evidence, probe, verify          # noqa: E402
from osched.db import connect                       # noqa: E402

LISTING_HTML = """
<html><body><ul>
<li><a href="https://dir.example/out?to=https%3A%2F%2Fclient.example"
   rel="nofollow sponsored">Client Site</a></li>
<li><a href="https://other.example/page">Other</a></li>
</ul></body></html>
"""


@pytest.fixture()
def conn(tmp_path):
    return connect(tmp_path)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_SECRET", raising=False)
    monkeypatch.delenv("PORTAL_BASE_URL", raising=False)
    return {"_data_root_resolved": str(tmp_path / "cfgroot")}


def _seed_plan(conn):
    cur = conn.execute(
        """INSERT INTO pool(url, domain, source_kind, source_file, source_row)
           VALUES('https://dir.example', 'dir.example', 'test', 't', 1)""")
    pool_id = cur.lastrowid
    cur = conn.execute("INSERT INTO project(domain) VALUES('client.example')")
    project_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO match(project_id, pool_id, state) VALUES(?,?, 'confirmed')",
        (project_id, pool_id))
    match_id = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO plan(project_id, match_id, scheduled_date, channel,
                            state, executed_at, result_note)
           VALUES(?,?,?,?, 'submitted', datetime('now','-4 days'), ?)""",
        (project_id, match_id, "2026-11-15", "form",
         json.dumps({"landing": "https://client.example"})))
    conn.commit()
    return cur.lastrowid


def _due_row(conn):
    rows = verify.due_verify(conn)
    assert rows, "seeded plan should be due for verification"
    return rows[0]


# --------------------------------------------------------------------------
# verify.check_one writes the time series
# --------------------------------------------------------------------------

def test_live_check_writes_event_with_anchor(conn, monkeypatch):
    plan_id = _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", LISTING_HTML))
    row = _due_row(conn)
    outcome = verify.check_one(conn, row)
    assert outcome["result"] == "live"

    lv = conn.execute("SELECT * FROM link_verify WHERE plan_id=?",
                      (plan_id,)).fetchone()
    assert lv["result"] == "live"
    assert lv["rel"] == "nofollow sponsored"
    assert lv["anchor_text"] == "Client Site"
    assert len(lv["page_sha256"]) == 64

    events = conn.execute(
        "SELECT * FROM link_check WHERE plan_id=? ORDER BY id",
        (plan_id,)).fetchall()
    assert len(events) == 1
    assert events[0]["result"] == "live"
    assert events[0]["rel"] == "nofollow sponsored"


def test_second_check_appends_and_updates_latest(conn, monkeypatch):
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn))
    # plan is now live; age the check past the recheck cadence (14d)
    conn.execute("UPDATE link_check SET checked_at=datetime('now','-15 days')")
    conn.commit()
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", "<html></html>"))
    outcome = verify.check_one(conn, _due_row(conn))
    assert outcome["result"] == "missing"
    # the plan stays live (credit stands) but the series shows the drop
    st = conn.execute("SELECT state FROM plan").fetchone()["state"]
    assert st == "live"

    events = conn.execute(
        "SELECT result FROM link_check ORDER BY id").fetchall()
    assert [e["result"] for e in events] == ["live", "missing"]
    lv = conn.execute("SELECT result FROM link_verify").fetchone()
    assert lv["result"] == "missing"          # latest verdict overwritten


def test_recheck_miss_does_not_inherit_pre_live_missings(conn, monkeypatch,
                                                         tmp_path):
    """missing x2 before confirmation, then live, then one recheck miss ->
    still not exhausted (counter restarts at the first live verdict)."""
    _seed_plan(conn)
    # two pre-live missings in run_log
    conn.execute(
        """INSERT INTO run_log(action, target, result)
           VALUES('verify', '%plan-target%', 'missing')""" .replace(
             "%plan-target%", f"dir.example plan#{_plan_id(conn)}"))
    conn.execute(
        """INSERT INTO run_log(action, target, result)
           VALUES('verify', ?, 'missing')""",
        (f"dir.example plan#{_plan_id(conn)}",))
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn))
    conn.execute("UPDATE link_check SET checked_at=datetime('now','-15 days')")
    conn.commit()
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", "<html></html>"))
    verify.check_one(conn, _due_row(conn))
    st = conn.execute("SELECT state FROM plan").fetchone()["state"]
    assert st == "live"          # one recheck miss must not exhaust


def _plan_id(conn) -> int:
    return conn.execute("SELECT id FROM plan").fetchone()["id"]


def test_unreachable_check_still_lands_in_series(conn, monkeypatch):
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (0, "", ""))
    outcome = verify.check_one(conn, _due_row(conn))
    assert outcome["result"] == "unreachable"
    n = conn.execute("SELECT COUNT(*) AS n FROM link_check").fetchone()["n"]
    assert n == 1


# --------------------------------------------------------------------------
# anchor extraction
# --------------------------------------------------------------------------

def test_find_anchor_without_rel():
    a = verify._find_anchor(
        '<a href="https://client.example/x">Site</a>', "https://client.example")
    assert a["rel"] == "" and a["text"] == "Site"


def test_find_anchor_no_match():
    assert verify._find_anchor('<a href="https://x.example">x</a>',
                               "https://client.example") == {}


# --------------------------------------------------------------------------
# receipts
# --------------------------------------------------------------------------

def test_token_is_stable_and_tamper_proof(cfg):
    t1 = evidence.token_for(cfg, 7)
    assert t1 == evidence.token_for(cfg, 7)
    assert t1.startswith("7.")
    assert evidence.parse_token(cfg, t1) == 7
    bad = "7." + "0" * 24
    assert evidence.parse_token(cfg, bad) is None
    assert evidence.parse_token(cfg, "abc") is None


def test_url_for_uses_base_when_present(cfg, monkeypatch):
    monkeypatch.setenv("PORTAL_BASE_URL", "https://linkattest.com")
    url = evidence.url_for(cfg, 3)
    assert url.startswith("https://linkattest.com/evidence/3.")
    monkeypatch.delenv("PORTAL_BASE_URL")
    assert evidence.url_for(cfg, 3).startswith("/evidence/3.")


def test_receipt_and_page_render(conn, cfg, monkeypatch):
    plan_id = _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda target, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn))

    r = evidence.receipt(conn, cfg, plan_id)
    assert r["placement"] == "dir.example"
    assert r["landing"] == "https://client.example"
    assert r["first_live"]
    assert r["checks"] == 1
    assert r["history"][0]["rel"] == "nofollow sponsored"

    body = evidence.render_page(conn, cfg, plan_id)
    assert body and "VERIFIED LIVE" in body
    assert "dir.example" in body and "https://client.example" in body
    assert "nofollow sponsored" in body
    assert r["page_sha256"][:16] in body


def test_receipt_none_for_unknown_plan(conn, cfg):
    assert evidence.receipt(conn, cfg, 999) is None
    assert evidence.render_page(conn, cfg, 999) is None


# --------------------------------------------------------------------------
# alerts: transition-based, dedupe by edge, recipient = ops mailbox
# --------------------------------------------------------------------------

def _age_checks(conn, days=15):
    conn.execute("UPDATE link_check SET checked_at=datetime('now', ?)",
                 (f"-{days} days",))
    conn.commit()


def _alerts(conn):
    return conn.execute(
        "SELECT target, result, note FROM run_log WHERE action='alert' "
        "ORDER BY id").fetchall()


def test_drop_alert_fires_on_live_to_missing_edge(conn, cfg, monkeypatch):
    cfg["report"] = {"email": "ops@example.com"}
    sent = []
    monkeypatch.setattr("osched.report.send_mail",
                        lambda c, to, subj, body, **kw: sent.append(subj))
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn), cfg=cfg)
    assert not sent                                   # first live: no alert

    _age_checks(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", "<html></html>"))
    verify.check_one(conn, _due_row(conn), cfg=cfg)
    assert len(sent) == 1 and "link-drop" in sent[0]
    a = _alerts(conn)[0]
    assert a["result"] == "sent" and "link-drop" in a["note"]


def test_consecutive_missing_is_quiet_until_exhaustion(conn, cfg,
                                                       monkeypatch):
    cfg["report"] = {"email": "ops@example.com"}
    monkeypatch.setattr("osched.report.send_mail",
                        lambda c, to, subj, body, **kw: None)
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn), cfg=cfg)
    _age_checks(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", "<html></html>"))
    verify.check_one(conn, _due_row(conn), cfg=cfg)   # edge: drop alert
    for _ in range(2):                                # 2 more missings
        _age_checks(conn)
        verify.check_one(conn, _due_row(conn), cfg=cfg)
    kinds = [json.loads(a["note"])["kind"] for a in _alerts(conn)]
    assert kinds == ["link-drop", "live-check-exhausted"]
    st = conn.execute("SELECT state FROM plan").fetchone()["state"]
    assert st == "human"


def test_rel_change_alert_on_flip(conn, cfg, monkeypatch):
    cfg["report"] = {"email": "ops@example.com"}
    monkeypatch.setattr("osched.report.send_mail",
                        lambda c, to, subj, body, **kw: None)
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn), cfg=cfg)
    _age_checks(conn)
    flipped = LISTING_HTML.replace('rel="nofollow sponsored"',
                                   'rel="nofollow"')
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", flipped))
    verify.check_one(conn, _due_row(conn), cfg=cfg)
    kinds = [json.loads(a["note"])["kind"] for a in _alerts(conn)]
    assert kinds == ["rel-change"]


def test_no_cfg_means_no_alert_rows(conn, cfg, monkeypatch):
    _seed_plan(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", LISTING_HTML))
    verify.check_one(conn, _due_row(conn))            # cfg=None
    _age_checks(conn)
    monkeypatch.setattr(probe, "_fetch",
                        lambda t, timeout: (200, "", "<html></html>"))
    verify.check_one(conn, _due_row(conn))
    assert len(_alerts(conn)) == 0
