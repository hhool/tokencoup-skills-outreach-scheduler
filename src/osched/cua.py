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
import re
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
# Include French/German/Spanish — several directory targets (pcastuces, etc.)
# confirm in their own language; English-only hints produced false `missing`.
SUCCESS_HINTS = ("thank you", "successfully", "submitted", "received",
                 "approved", "pending review", "已提交", "感谢",
                 # French: use specific success phrases. Bare "merci" is an
                 # error on some sites ("Merci de préciser votre requête").
                 "merci, votre", "votre site a été", "a été ajouté",
                 "a bien été transmis", "a bien été enregistré",
                 "enregistré avec succès", "danke", "gracias",
                 "confirmación")

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


# Semantic field keys used both to score a form and to fill it.
_FIELD_KEYS = ("title", "name", "author", "nom", "desc", "summary",
               "comment", "message", "text", "commentaire", "mail",
               "email", "e-mail", "url", "link", "site", "web")


def _pick_form(page):
    """Choose the form with the most semantic fields — not the site search.

    Many directory pages put a site-search form (single `q` input) before the
    actual submission form. Filling all forms and clicking the first submit
    button posted to search and produced a false `submitted` verdict.
    """
    best, best_score = None, 0
    for form in page.query_selector_all("form"):
        score = 0
        for h in form.query_selector_all("input, textarea"):
            typ = (h.get_attribute("type") or "").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            name = ((h.get_attribute("name") or h.get_attribute("id") or "")
                    .lower())
            if name and any(k in name for k in _FIELD_KEYS):
                score += 1
        if score > best_score:
            best, best_score = form, score
    return best


def _fill_form(page, ctx: dict, entry_url: str, form=None) -> bool:
    """Fill visible inputs by semantic name heuristics. True if any filled.

    When ``form`` is given, only that form's fields are touched — critical on
    pages that also carry a search form.
    """
    filled = 0
    scope = form.query_selector_all("input, textarea") if form else \
        page.query_selector_all("form input, form textarea")
    for handle in scope:
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
            if name in ARITH_FIELDS:
                answer = _arith_answer(_label_text(handle))
                if answer is not None:
                    handle.fill(str(answer)); filled += 1
            elif name in NAME_FIELDS:
                who = (ctx.get("sender_name") or "").strip()
                if not who or ("." in who and "@" not in who):
                    who = ctx.get("site_name") or ctx["title"]
                handle.fill(who[:200]); filled += 1
            elif any(k in name for k in ("title", "name", "author", "nom")):
                # "Nom" (French) and "name" both carry the person's name.
                # A domain used as sender_name looks automated — use the
                # human name when configured, else a neutral site-derived
                # label (never the bare domain).
                who = (ctx.get("sender_name") or "").strip()
                if not who or ("." in who and "@" not in who):
                    who = ctx.get("site_name") or ctx["title"]
                handle.fill(who[:200]); filled += 1
            elif any(k in name for k in ("desc", "summary", "comment",
                                         "message", "text", "commentaire")):
                handle.fill(ctx["description"][:500]); filled += 1
            elif "mail" in name or "email" in name or "e-mail" in name:
                if ctx.get("email"):
                    handle.fill(ctx["email"]); filled += 1
            elif (name in ("class", "category", "cat", "section", "topic",
                           "niche", "industry") or "categ" in name
                  or name.startswith("cat_")):
                # Classic PHP directories ask a free-text `class`/`category`
                # and JS-validate it as required (cam111: "Bad Category!").
                if ctx.get("category"):
                    handle.fill(str(ctx["category"])[:80]); filled += 1
            elif any(k in name for k in ("url", "link", "site", "web")):
                handle.fill(ctx["url"]); filled += 1
        except Exception:
            continue
    return filled > 0


# Options that claim nothing: a form asking "funding?" / "budget?" is answered
# honestly with the free/none option rather than an invented figure. Bare "no"
# is deliberately absent — a yes/no group must not be answered by guesswork.
HONEST_OPTION_RE = re.compile(
    r"^\s*(none|none of these|none of the above|not applicable|n/a|na|"
    r"\$?0|free|free tier|other|zero cost|no budget)\s*$", re.I)


def _fill_radios(page, form=None) -> tuple[int, list[str]]:
    """Tick the honest option in each radio group; report groups left blank.

    launchingnext asks funding + marketing budget as required radios. Leaving
    them blank re-rendered the form with "Please check..." at HTTP 200 — a
    rejection, not a submission. Only none/$0/free/other options are chosen.
    """
    groups: dict[str, list] = {}
    scope = (form.query_selector_all("input") if form
             else page.query_selector_all("form input"))
    for handle in scope:
        try:
            if (handle.get_attribute("type") or "").lower() != "radio":
                continue
            name = handle.get_attribute("name") or ""
            if not name:
                continue
            groups.setdefault(name, []).append(handle)
        except Exception:
            continue

    picked, left = 0, []
    for name, handles in groups.items():
        chosen = None
        for handle in handles:
            try:
                value = (handle.get_attribute("value") or "").strip()
            except Exception:
                continue
            if HONEST_OPTION_RE.match(value):
                chosen = handle
                break
        if chosen is None:
            left.append(name)
            continue
        try:
            chosen.check()
            picked += 1
        except Exception:
            left.append(name)
    return picked, left


ARITH_RE = re.compile(r"(\d+)\s*([+\-x×*])\s*(\d+)")
NAME_FIELDS = {"user", "yourname", "your_name", "fullname", "full_name",
               "contact_name", "submitter", "submitted_by"}
# Old-school directories gate spam with a plain arithmetic question ("Quick
# check: what is 2+3?"). It is a form field, not a vendor captcha: answer it
# from the label instead of burning a human handoff.
ARITH_FIELDS = {"math", "sum", "answer", "captcha_answer", "verify", "check",
                "human_check", "antispam", "spam_check"}


def _arith_answer(text: str) -> int | None:
    """Solve a bare `a op b` quoted in a field label; None if absent."""
    m = ARITH_RE.search(text or "")
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    return a * b


def _label_text(handle) -> str:
    """Text of the input's <label> (empty when the stub/page cannot say)."""
    try:
        return handle.evaluate(
            "el => (el.labels && el.labels.length) ? el.labels[0].innerText : ''"
        ) or ""
    except Exception:
        return ""


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


def _submit(page, form=None) -> bool:
    """Click a submit control (preferably inside ``form``); wait for a reply.

    Scoping to the chosen form avoids clicking a site-search button that
    precedes the real submission form in the DOM.
    """
    selectors = ("button[type='submit']", "input[type='submit']", "button")
    if form is not None:
        for sel in selectors:
            btn = form.query_selector(sel)
            if btn and btn.is_visible():
                try:
                    btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    return True
                except PWTimeout:
                    return True   # soft (AJAX) navigation; verify is authority
        form.press("Enter")
        page.wait_for_timeout(3000)
        return True
    # Legacy path: no form selected — first visible submit control on page.
    for sel in ("form button[type='submit']", "form input[type='submit']",
                "form button"):
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            try:
                btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
            except PWTimeout:
                return True
    first = page.query_selector("form")
    if first:
        first.press("Enter")
        page.wait_for_timeout(3000)
        return True
    return False


def _looks_successful_type(body: str) -> bool:
    """Pure success-text check, shared with tests and the page wrapper."""
    low = (body or "").lower()[:4000]
    return any(h in low for h in SUCCESS_HINTS)


def _looks_successful(page) -> bool:
    try:
        body = page.inner_text("body", timeout=5000)
        return _looks_successful_type(body)
    except Exception:
        return False


def _rejection_reason(page) -> str:
    """Copy that proves the form refused the submission (empty when unsure).

    A rejected submit usually re-renders the same page with an inline error at
    HTTP 200 — the exact shape that made HTTP-only "submitted" verdicts lie.
    Reuses probe.copy_verdict so one keyword set governs both channels.
    """
    try:
        body = page.content()
    except Exception:
        return ""
    from .probe import copy_verdict        # local import: probe stays optional
    verdict = copy_verdict(body)
    if verdict.get("success") or verdict.get("already"):
        return ""
    return (verdict.get("failure") or "")[:120]


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

    form = _pick_form(page)
    if not _fill_form(page, ctx, entry, form=form):
        return {"state": "human", "note": "no fillable form fields",
                "secs": round(time.time() - started, 1)}

    # Required radio groups (funding, budget, "which of these describes you")
    # block the submit until answered. Answer only the honest option; groups
    # without one stay blank and are named in the note so a rejection is
    # diagnosable instead of looking like a mystery.
    radios_picked, radios_left = _fill_radios(page, form=form)
    radio_note = (f"radio groups left blank: {','.join(radios_left)}"
                  if radios_left else "")

    if not _submit(page, form=form):
        return {"state": "human", "note": "no submit control found",
                "secs": round(time.time() - started, 1)}

    page.wait_for_timeout(2500)
    ok = _looks_successful(page)
    end_url = page.url
    # A validation error re-renders at HTTP 200. Calling that `submitted` is
    # how silent rejections become fake ledger rows, so a detected rejection
    # goes to `human` with the reason attached.
    rejected = "" if ok else _rejection_reason(page)
    if rejected:
        return {"state": "human",
                "note": f"form rejected: {rejected}" +
                        (f" ({radio_note})" if radio_note else ""),
                "success_marker": False,
                "landing_url": end_url, "captcha_action": action,
                "secs": round(time.time() - started, 1)}
    # No success copy and no rejection copy: still `submitted` — verify is the
    # authority on whether the link landed. Record the marker honestly so a
    # false success stays diagnosable instead of silently trusted.
    return {"state": "submitted",
            "note": ("success page detected" if ok
                     else "submitted (no success marker; verify manually)") +
                    (f" [{radio_note}]" if radio_note else ""),
            "success_marker": bool(ok),
            "landing_url": end_url,
            "captcha_action": action,
            "radios_picked": radios_picked,
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
