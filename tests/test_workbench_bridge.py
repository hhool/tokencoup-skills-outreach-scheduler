"""Workbench ledger bridge + curated scoring.

Run: python3 -m pytest tests/test_workbench_bridge.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import matching, portal                  # noqa: E402
from osched.db import connect                        # noqa: E402
from osched.workbench import import_targets          # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path)
    c.executescript(portal.PORTAL_SCHEMA)
    portal._migrate_portal(c)
    return c


def _ledger(tmp_path, rows):
    p = tmp_path / "targets.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _t(**over):
    base = {"id": "t_1", "domain": "dir.example", "url": "https://dir.example/add",
            "submit_type": "listing", "wave": 1, "status": "working",
            "fit_score": 80, "how": "submit the url", "category": "directory"}
    base.update(over)
    return base


def test_import_inserts_and_is_idempotent(conn, tmp_path):
    path = _ledger(tmp_path, [_t(), _t(id="t_2", domain="two.example",
                                       url="https://two.example/submit")])
    first = import_targets(conn, path)
    assert (first["inserted"], first["upgraded"]) == (2, 0)
    second = import_targets(conn, path)
    assert second["inserted"] == 0          # re-import updates, never duplicates
    n = conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
    assert n == 2


def test_dropped_targets_are_never_imported(conn, tmp_path):
    path = _ledger(tmp_path, [_t(status="dropped")])
    stats = import_targets(conn, path)
    assert stats["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM pool").fetchone()[0] == 0


def test_ledger_drop_reaches_the_pool(conn, tmp_path):
    """A ledger drop must stop delivery: leaving the pool row `active` kept
    queueing submissions to a target the operator had already rejected."""
    conn.execute("""INSERT INTO pool(url, domain, source_kind, source_file,
        source_row, submit_type, wave, probe_state)
        VALUES('http://contact.example/','contact.example','wdc','f',0,
               'listing',1,'probed')""")
    conn.commit()
    path = _ledger(tmp_path, [_t(domain="contact.example",
                                 url="http://contact.example/",
                                 status="dropped", submit_type="unknown")])
    stats = import_targets(conn, path)
    assert stats["dropped"] == 1
    assert conn.execute("SELECT status FROM pool WHERE domain='contact.example'"
                        ).fetchone()["status"] == "dropped"


def test_ledger_drop_is_idempotent(conn, tmp_path):
    conn.execute("""INSERT INTO pool(url, domain, source_kind, source_file,
        source_row, submit_type, wave, probe_state)
        VALUES('http://gone.example/','gone.example','wdc','f',0,'listing',1,'probed')""")
    conn.commit()
    path = _ledger(tmp_path, [_t(domain="gone.example",
                                 url="http://gone.example/", status="dropped")])
    assert import_targets(conn, path)["dropped"] == 1
    assert import_targets(conn, path)["dropped"] == 0


def test_existing_classification_is_not_downgraded(conn, tmp_path):
    conn.execute("""INSERT INTO pool(url, domain, source_kind, source_file,
        source_row, submit_type, wave, probe_state)
        VALUES('http://auto.example/','auto.example','wdc','f',0,'auto',1,
               'probed')""")
    conn.commit()
    path = _ledger(tmp_path, [_t(domain="auto.example",
                                 url="https://auto.example/",
                                 submit_type="unknown", wave=3)])
    stats = import_targets(conn, path)
    assert stats["skipped_downgrade"] == 1
    row = conn.execute("SELECT submit_type, wave FROM pool "
                       "WHERE domain='auto.example'").fetchone()
    assert (row["submit_type"], row["wave"]) == ("auto", 1)


def test_bridge_fills_entry_url_but_keeps_a_specific_one(conn, tmp_path):
    conn.execute("""INSERT INTO pool(url, domain, source_kind, source_file,
        source_row, entry_url, submit_type)
        VALUES('http://keep.example/','keep.example','wdc','f',0,
               'https://keep.example/add-listing','listing')""")
    conn.commit()
    path = _ledger(tmp_path, [_t(domain="keep.example",
                                 url="https://keep.example/",
                                 submit_url="https://keep.example/other")])
    import_targets(conn, path)
    row = conn.execute("SELECT entry_url FROM pool WHERE domain='keep.example'"
                       ).fetchone()
    assert row["entry_url"] == "https://keep.example/add-listing"


def test_curated_rows_enter_the_queue_despite_min_score(conn, tmp_path):
    conn.execute("INSERT INTO project(domain, tags_json) VALUES('t.example','[]')")
    pid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    path = _ledger(tmp_path, [_t()])
    import_targets(conn, path)
    project = {"_id": pid, "domain": "t.example", "tags": ["gaming"],
               "exclusions": [], "scoring": {"min_score": 0.9,
                                             "weights": matching.DEFAULT_WEIGHTS}}
    stats = matching.run_match(conn, project)
    assert stats["pending"] == 1
    detail = json.loads(conn.execute("SELECT score_json FROM match").fetchone()[0])
    assert detail["curated"] is True
