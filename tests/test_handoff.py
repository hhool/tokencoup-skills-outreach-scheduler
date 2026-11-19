"""Captcha handoff contract: status file + .go marker (B3).

Run: python3 -m pytest tests/test_handoff.py -q
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from osched import handoff             # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    return {"_data_root_resolved": str(tmp_path)}


def test_request_status_and_clear(cfg):
    rec = handoff.request_handoff(
        cfg, "example.com", url="https://example.com/join",
        note="captcha=turnstile")
    assert rec["status"] == "waiting" and "turnstile" in rec["note"]
    assert handoff.status(cfg, "example.com")["status"] == "waiting"
    handoff.clear(cfg, "example.com")
    assert handoff.status(cfg, "example.com") is None


def test_wait_for_go_resumes_on_marker(cfg):
    handoff.request_handoff(cfg, "example.com")
    go = handoff.go_path(cfg, "example.com")
    # create the marker during the wait
    import threading

    def touch():
        import time
        time.sleep(0.3)
        go.touch()

    threading.Thread(target=touch, daemon=True).start()
    assert handoff.wait_for_go(cfg, "example.com", timeout=5, poll=0.1)
    assert handoff.status(cfg, "example.com")["status"] == "resumed"


def test_wait_times_out_fail_open(cfg):
    handoff.request_handoff(cfg, "example.com")
    assert not handoff.wait_for_go(cfg, "example.com", timeout=0.3, poll=0.1)
    assert handoff.status(cfg, "example.com")["status"] == "timed-out"


def test_stale_go_marker_is_cleared_on_new_request(cfg):
    handoff.request_handoff(cfg, "example.com")   # creates the handoff dir
    handoff.go_path(cfg, "example.com").touch()
    handoff.request_handoff(cfg, "example.com")   # new request clears stale go
    assert not handoff.go_path(cfg, "example.com").exists()


def test_domain_sanitized_in_filenames(cfg):
    p = handoff.status_path(cfg, "a.example.com/x")
    assert "/" not in p.name and p.name.endswith(".json")


def test_record_post_solve_writes_evidence(cfg):
    class FakePage:
        def screenshot(self, path):
            import pathlib as _p
            _p.Path(path).write_bytes(b"png")
    rec = handoff.record_post_solve(cfg, "example.com", "https://example.com/join",
                                    form=True, page=FakePage())
    assert rec["form_found"] is True and rec["url"].endswith("/join")
    assert handoff.evidence_path(cfg, "example.com").read_bytes() == b"png"
    import json as _json
    saved = _json.loads((handoff.handoff_dir(cfg) /
                         "example.com.post-solve.json").read_text())
    assert saved["form_found"] is True


def test_record_post_solve_without_page_is_safe(cfg):
    rec = handoff.record_post_solve(cfg, "x.example", "https://x.example/", form=False)
    assert rec["form_found"] is False and "screenshot" not in rec
