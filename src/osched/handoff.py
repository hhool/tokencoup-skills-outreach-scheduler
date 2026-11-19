"""Human-in-the-loop captcha handoff for the register channel.

Evidence (2026-11-19 pilot): every confirmed big-platform signup target is
gated by turnstile/recaptcha or anti-bot. Automation can navigate and fill,
but must not pretend to solve a challenge. This module implements the
handoff contract:

  1. CUA opens the signup page in a *visible* browser and detects a captcha.
  2. It writes a status file under data_root/captcha_handoff/ and waits.
  3. The operator solves the captcha in that same browser window, then
     creates the sibling `.go` marker file (touch is enough).
  4. The automation resumes on the same page, finds the signup form,
     fills and submits as usual.

The marker is a file, not stdin: it works when the CLI runs in a background
shell / automation harness where interactive stdin is unavailable.

中文：验证码人机交接。可见浏览器停在验证码页，人工解决后 touch 一个 `.go`
文件作为信号，自动化在同页继续填报提交。用文件而非 stdin，便于后台运行。
"""
from __future__ import annotations

import json
import pathlib
import time

from . import config


def handoff_dir(cfg: dict) -> pathlib.Path:
    return config.data_root(cfg) / "captcha_handoff"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in name)


def status_path(cfg: dict, domain: str) -> pathlib.Path:
    return handoff_dir(cfg) / (_safe(domain) + ".json")


def go_path(cfg: dict, domain: str) -> pathlib.Path:
    return handoff_dir(cfg) / (_safe(domain) + ".go")


def request_handoff(cfg: dict, domain: str, *, url: str = "",
                    note: str = "") -> dict:
    """Publish a waiting status; clear any stale go marker first."""
    d = handoff_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    go = go_path(cfg, domain)
    if go.exists():
        go.unlink()
    rec = {"domain": domain, "url": url, "note": note,
           "status": "waiting", "requested_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    status_path(cfg, domain).write_text(json.dumps(rec, ensure_ascii=False))
    return rec


def status(cfg: dict, domain: str) -> dict | None:
    p = status_path(cfg, domain)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def wait_for_go(cfg: dict, domain: str, *, timeout: float = 300,
                poll: float = 2.0) -> bool:
    """Block until the operator creates the `.go` marker. Fail-open on timeout."""
    go = go_path(cfg, domain)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if go.exists():
            rec = status(cfg, domain) or {}
            rec.update(status="resumed",
                       resumed_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            status_path(cfg, domain).write_text(
                json.dumps(rec, ensure_ascii=False))
            return True
        time.sleep(poll)
    rec = status(cfg, domain) or {}
    rec.update(status="timed-out")
    status_path(cfg, domain).write_text(json.dumps(rec, ensure_ascii=False))
    return False


def evidence_path(cfg: dict, domain: str) -> pathlib.Path:
    return handoff_dir(cfg) / (_safe(domain) + ".post-solve.png")


def record_post_solve(cfg: dict, domain: str, url: str, *, form: bool,
                      page=None) -> dict:
    """Snapshot the state the operator left after solving the challenge.

    The browser may have navigated on solve; a screenshot plus the landing URL
    and a form flag make the handoff auditable — if the flow ends in `human`
    again we can see whether the challenge was actually cleared, instead of
    guessing. Nothing here affects the signup decision.
    """
    d = handoff_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"domain": domain, "url": url, "form_found": bool(form),
           "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if page is not None:
        try:
            page.screenshot(path=str(evidence_path(cfg, domain)))
            rec["screenshot"] = evidence_path(cfg, domain).name
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass
    (d / (_safe(domain) + ".post-solve.json")).write_text(
        json.dumps(rec, ensure_ascii=False))
    return rec


def clear(cfg: dict, domain: str) -> None:
    for p in (status_path(cfg, domain), go_path(cfg, domain)):
        if p.exists():
            p.unlink()
