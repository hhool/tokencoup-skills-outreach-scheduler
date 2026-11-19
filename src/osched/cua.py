"""CUA channel executor (DESIGN.md §7.1).

Handles plan rows in state='cua' — sites whose submit form carries a
browser-class captcha. Runs a real local Chrome via Playwright (the only
local-environment dependency in the system), fills the form, attempts the
captcha inside the browser with a real fingerprint, and submits.

Tier behavior (ratified):
  recaptcha_v3 / turnstile / invisible  -> real fingerprint passes naturally
  recaptcha_v2 / hcaptcha               -> click the checkbox inside its
                                            iframe; if the challenge escalates
                                            to an image grid -> human queue
  image captcha                          -> human queue (OCR stub, free-first)
Guardrails: concurrency 1 (serial), captcha failures are never retried,
repeated failures auto-downgrade the domain to the human queue.

中文：CUA 通道——真浏览器填表+验证码；escalate 即进人工，绝不重试。
"""
from __future__ import annotations

import json
import time

from .db import note_with_landing

# Plan rows eligible for the CUA run: state='cua' only. The HTTP executor
# already routed them here; the CUA run is their second and final stop
# before the human queue.

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:  # playwright is optional until a CUA run is requested
    sync_playwright = None

CHROME_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
]

# iframe URL fragments per captcha vendor -> clickable element inside it.
CAPTCHA_IFRAMES = {
    "recaptcha_v2": ("recaptcha/api2/anchor", "#recaptcha-anchor"),
    "recaptcha_v3": ("recaptcha/api2/bn", None),   # invisible; no click
    "turnstile": ("challenges.cloudflare.com", "input[type='checkbox']"),
    "hcaptcha": ("hcaptcha.com/1/api.js", "#checkbox"),
}

# Words that mark a success/confirmation page after submission.
SUCCESS_HINTS = ("thank you", "successfully", "submitted", "received",
                 "approved", "pending review", "已提交", "感谢")

REPEAT_FAIL_LIMIT = 3


def cua_plans(conn, project_id: int, limit: int = 0) -> list:
    rows = conn.execute(
        """
        SELECT pl.*, p.url, p.domain AS pool_domain, p.entry_url,
               p.captcha_type, p.contact_email
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
        WHERE pl.project_id=? AND pl.state='cua'
        ORDER BY pl.scheduled_date
        """,
        (project_id,),
    ).fetchall()
    return rows[:limit] if limit else rows


def _recent_cua_failures(conn, domain: str) -> int:
    n = conn.execute(
        """SELECT COUNT(*) AS n FROM run_log
           WHERE action='cua' AND target LIKE ? AND result='failed'
           AND ts >= datetime('now', '-30 days')""",
        (f"%{domain}%",),
    ).fetchone()["n"]
    return n


def _fill_form(page, ctx: dict, entry_url: str) -> bool:
    """Fill visible inputs by semantic name heuristics. True if any filled."""
    filled = 0
    for handle in page.query_selector_all("form input, form textarea"):
        try:
            name = (handle.get_attribute("name")
                    or handle.get_attribute("id") or "").lower()
            typ = (handle.get_attribute("type") or "").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if not handle.is_visible():
                continue
            # Most-specific patterns first: a field named site_title must
            # not be captured by the url/site branch.
            if any(k in name for k in ("title", "name", "author")):
                handle.fill(ctx["title"][:200]); filled += 1
            elif any(k in name for k in ("desc", "summary", "comment",
                                         "message", "text")):
                handle.fill(ctx["description"][:500]); filled += 1
            elif "mail" in name or "email" in name:
                if ctx.get("email"):
                    handle.fill(ctx["email"]); filled += 1
            elif any(k in name for k in ("url", "link", "site")):
                handle.fill(ctx["url"]); filled += 1
        except Exception:
            continue
    return filled > 0


def _click_captcha(page, captcha: str) -> str:
    """Click the vendor checkbox inside its iframe.

    Returns 'clicked' | 'invisible' | 'escalated' | 'unsupported'.
    """
    spec = CAPTCHA_IFRAMES.get(captcha)
    if spec is None:
        return "unsupported"
    frag, selector = spec
    if selector is None:
        return "invisible"          # v3 / invisible: nothing to click
    for frame_el in page.query_selector_all("iframe"):
        src = (frame_el.get_attribute("src") or "")
        if frag not in src:
            continue
        try:
            frame = frame_el.content_frame()
            box = frame.query_selector(selector)
            if box:
                box.click()
                page.wait_for_timeout(1500)
                return "clicked"
        except Exception:
            return "escalated"
    return "escalated"


def _submit(page) -> bool:
    """Click the submit control or press Enter; wait for a response."""
    for sel in ("form button[type='submit']", "form input[type='submit']",
                "form button"):
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            try:
                btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
            except PWTimeout:
                return True   # navigation may be soft (AJAX); treat as sent
    form = page.query_selector("form")
    if form:
        form.press("Enter")
        page.wait_for_timeout(3000)
        return True
    return False


def _looks_successful(page) -> bool:
    try:
        body = page.inner_text("body", timeout=5000).lower()[:4000]
        return any(h in body for h in SUCCESS_HINTS)
    except Exception:
        return False


def _execute_one(page, row, ctx: dict) -> dict:
    """Run one plan row in the live browser. Returns the outcome dict."""
    entry = row["entry_url"] or row["url"]
    started = time.time()
    page.goto(entry, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)   # let captcha scripts initialize

    captcha = row["captcha_type"] or "unknown"
    action = "none"
    if captcha in CAPTCHA_IFRAMES:      # browser-class only; none/unknown skip
        action = _click_captcha(page, captcha)
        if action in ("escalated", "unsupported"):
            return {"state": "human", "note": f"captcha {captcha} {action} -> "
                    "human queue", "secs": round(time.time() - started, 1)}

    if not _fill_form(page, ctx, entry):
        return {"state": "human", "note": "no fillable form fields",
                "secs": round(time.time() - started, 1)}

    if not _submit(page):
        return {"state": "human", "note": "no submit control found",
                "secs": round(time.time() - started, 1)}

    page.wait_for_timeout(2500)
    ok = _looks_successful(page)
    return {"state": "submitted" if ok else "submitted",
            "note": ("success page detected" if ok
                     else "submitted (no success marker; verify manually)"),
            "captcha_action": action,
            "secs": round(time.time() - started, 1)}


def _launch(pw, headless: bool):
    """Launch real Chrome (DESIGN: 'local real Chrome'); fall back to the
    bundled chromium when Google Chrome is absent or too new for this OS."""
    for kwargs in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(headless=headless,
                                      args=CHROME_LAUNCH_ARGS, **kwargs)
        except Exception:
            continue
    raise RuntimeError("no usable browser: install Google Chrome or run "
                       "'playwright install chromium'")


def run_cua(conn, cfg: dict, project: dict, *, headless: bool = False,
            limit: int = 0) -> dict:
    """Process all state='cua' plan rows for one project. Serial, one page."""
    if sync_playwright is None:
        raise RuntimeError(
            "playwright not installed — run: pip install playwright "
            "&& playwright install chromium")

    rows = cua_plans(conn, project["_id"], limit)
    stats = {"due": len(rows), "submitted": 0, "human": 0, "skipped": 0,
             "rows": []}
    if not rows:
        return stats

    from .executor import build_context

    with sync_playwright() as pw:
        browser = _launch(pw, headless)
        page = browser.new_page()
        try:
            for row in rows:
                dom = row["pool_domain"]
                if _recent_cua_failures(conn, dom) >= REPEAT_FAIL_LIMIT:
                    stats["skipped"] += 1
                    stats["rows"].append({"plan_id": row["id"],
                                          "domain": dom,
                                          "state": "human",
                                          "note": "repeat-fail downgrade"})
                    continue
                ctx = build_context(conn, project, row)
                try:
                    outcome = _execute_one(page, row, ctx)
                except Exception as e:
                    outcome = {"state": "failed", "note": str(e)[:150]}
                if outcome["state"] == "human":
                    stats["human"] += 1
                elif outcome["state"] == "submitted":
                    stats["submitted"] += 1
                    conn.execute(
                        """UPDATE probe_cache SET
                             success_count=success_count+1,
                             last_verified=datetime('now')
                           WHERE domain=?""", (dom,))
                cua_note = note_with_landing(conn, row["id"], outcome)
                conn.execute(
                    """UPDATE plan SET state=?, executed_at=datetime('now'),
                         captcha_solved=?, result_note=? WHERE id=?""",
                    (outcome["state"],
                     1 if outcome["state"] == "submitted" else 0,
                     json.dumps(cua_note, ensure_ascii=False), row["id"]))
                conn.execute(
                    "INSERT INTO run_log(action, target, result, note) "
                    "VALUES('cua', ?, ?, ?)",
                    (f"{dom} plan#{row['id']}", outcome["state"],
                     json.dumps({k: v for k, v in outcome.items()
                                 if k != "state"}, ensure_ascii=False)))
                stats["rows"].append({"plan_id": row["id"], "domain": dom,
                                      "state": outcome["state"],
                                      "note": outcome["note"]})
        finally:
            browser.close()
    conn.commit()
    return stats
