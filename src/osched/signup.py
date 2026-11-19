"""Register channel stage 1: automated account signup (CUA).

The 138 wave1 `register` rows are web2.0 platforms: an account is required
before any link can be placed. This module closes the first segment of the
register chain — open the platform, find the signup form (not login!), fill
identity fields from the project assets, submit, and record the account as
`pending-activation`. The existing chain then takes over:

  fetch_verification_mails()  (IMAP — reads the verification mail)
  run_activation()            (CUA — clicks the activation link)
  ...posting with the link is stage 2 (post-publish channel)

Passwords are generated per account and stored in the account ledger only
(masked in every output path via accounts._mask).

中文：register 通道第一段 = 自动开户。找到注册表单（排除登录表单）、
填身份字段、提交、落 pending-activation；后续激活/发文走既有链路。
"""
from __future__ import annotations

import json
import re
import secrets
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:      # CUA channel unavailable — same contract as cua.py
    sync_playwright = None

from . import accounts as accounts_mod
from .cua import _launch, CHROME_LAUNCH_ARGS

REPEAT_FAIL_LIMIT = 3    # per-domain signup failures before downgrade (30d)

# Common signup paths tried when the entry page itself has no signup form.
SIGNUP_PATH_CANDIDATES = ("/register", "/signup", "/sign-up", "/join")


# --------------------------------------------------------------------------
# signup form recognition — the mirror image of probe.find_submit_form
# --------------------------------------------------------------------------

LOGIN_ACTION_RE = re.compile(
    r"login|signin|sign-in|log-in|logon|session|auth", re.I)


def find_signup_form(html: str) -> tuple[bool, list[str], str]:
    """First registration form: has a password field + an identity field
    (email or username), and is not a login form. (ok, fields, action)."""
    forms = re.findall(r"<form\b.*?</form>", html, re.I | re.S)
    for block in forms:
        names = re.findall(r'name="([^"]+)"', block)
        low = [n.lower() for n in names]
        if not low:
            continue
        has_pw = any("pass" in n for n in low)
        has_identity = any(
            k in n for n in low
            for k in ("mail", "user", "login", "account", "nick"))
        if not (has_pw and has_identity):
            continue
        open_tag = block[: block.find(">") + 1]
        m = re.search(r'action="([^"]*)"', open_tag, re.I)
        action = m.group(1) if m else ""
        if action and LOGIN_ACTION_RE.search(action):
            continue                     # credential endpoint
        # a login form typically has a *single* password + username pair and
        # often a "remember"; a signup form asks for the password twice or
        # has an email confirm. Require >= 3 named fields as a tiebreak.
        if len(low) < 3:
            continue
        return True, names, action
    return False, [], ""


def signup_captcha_hint(html: str) -> str:
    low = html.lower()
    if "recaptcha" in low or "g-recaptcha" in low:
        return "recaptcha"
    if "hcaptcha" in low:
        return "hcaptcha"
    if "turnstile" in low or "cf-turnstile" in low:
        return "turnstile"
    return "none"


def gen_password() -> str:
    # url-safe, 16 chars: strong enough, no quoting issues in web forms
    return secrets.token_urlsafe(12)


def site_root(url: str) -> str:
    """scheme://netloc for a URL — signup paths hang off this, not a deep entry.

    The workbench often stores a deep entry_url (e.g. alternativeto's
    /manage/add-app). Appending /register to that path produces a 404; every
    platform exposes signup at the site root. Normalising first is what lets
    the CUA walk reach a real signup form.
    """
    p = urlsplit(url if "://" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}"


def signup_candidate_urls(entry_url: str, discovered: str = "") -> list[str]:
    """Ordered URLs the CUA walks to find a signup form.

    A confirmed `discovered` page wins; otherwise try the stored entry then
    the common signup paths on the SITE ROOT (never on a deep entry path).
    """
    root = site_root(entry_url)
    root_index = root + "/"
    if discovered and discovered != root_index:
        return [discovered]
    return [entry_url.rstrip("/")] + [root + c for c in SIGNUP_PATH_CANDIDATES]


def cap_after_handoff(solved: bool, html: str) -> str:
    """Captcha verdict after the operator's .go marker.

    A solved turnstile leaves its widget markup in the DOM, so re-detecting
    from HTML would re-gate forever. The marker is the human attestation:
    once solved, report 'none' and let the flow find the form and fill it.
    """
    return "none" if solved else signup_captcha_hint(html)


# --------------------------------------------------------------------------
# candidate selection — confirmed register-type matches without an account
# --------------------------------------------------------------------------

def signup_candidates(conn, project_id: int) -> list:
    return conn.execute(
        """
        SELECT m.id AS match_id, p.id AS pool_id, p.domain AS pool_domain,
               p.url, p.entry_url, p.captcha_type
        FROM match m
        JOIN pool p ON p.id = m.pool_id
        WHERE m.project_id=? AND m.state='confirmed'
              AND p.submit_type='register'
          AND NOT EXISTS (SELECT 1 FROM account a
                          WHERE a.project_id=m.project_id
                            AND a.dir_domain=p.domain)
        ORDER BY p.domain
        """, (project_id,)).fetchall()


def _discover_signup_url(entry: str, timeout: float = 10.0) -> str:
    """Static-first signup-page discovery: entry page, then common paths.

    Cheap HTTP fetches (no browser); the winner is re-opened in CUA where
    JS rendering and real typing happen. Returns the best-known URL.
    """
    from . import probe as probe_mod
    base = entry.rstrip("/")
    urls = [entry] + [base + c for c in SIGNUP_PATH_CANDIDATES]
    fallback = entry
    for u in urls:
        try:
            st, _, html = probe_mod._fetch(u, timeout)
        except Exception:  # noqa: BLE001 — discovery is best-effort
            continue
        if st != 200 or not html:
            continue
        ok, _, _ = find_signup_form(html)
        if ok:
            return u
    return fallback


def _recent_signup_failures(conn, domain: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) AS n FROM run_log
           WHERE action='signup' AND target LIKE ? AND result='failed'
             AND ts >= datetime('now', '-30 days')""",
        (f"%{domain}%",)).fetchone()["n"]


# --------------------------------------------------------------------------
# CUA signup flow
# --------------------------------------------------------------------------

def _fill_signup(page, ctx: dict, password: str) -> list[str]:
    """Fill visible signup fields by semantic heuristics. Returns names set."""
    filled: list[str] = []
    handles = page.query_selector_all("form input, form textarea")
    for el in handles:
        try:
            name = (el.get_attribute("name") or el.get_attribute("id")
                    or "").lower()
            typ = (el.get_attribute("type") or "text").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if not el.is_visible():
                continue
            if "pass" in name:
                el.fill(password)
                filled.append(name)
            elif "mail" in name:
                el.fill(ctx["email"])
                filled.append(name)
            elif any(k in name for k in ("user", "login", "nick", "account",
                                         "username")):
                el.fill(ctx["username"])
                filled.append(name)
            elif any(k in name for k in ("site", "url", "web")):
                el.fill(ctx["url"])
                filled.append(name)
            elif any(k in name for k in ("name", "title")):
                el.fill(ctx["site_name"])
                filled.append(name)
        except Exception:  # noqa: BLE001 — one bad field must not kill signup
            continue
    return filled


_SUCCESS_RE = re.compile(
    r"thank|success|confirm(?:ation)?(?:\s+(?:mail|e-?mail|sent|link))?"
    r"|check your (?:mail|e-?mail|inbox)|verify your account|activate"
    r"|almost done|welcome|registered|created", re.I)


def _looks_successful(html: str, had_pw_form: bool) -> bool:
    if not had_pw_form:
        return False
    return bool(_SUCCESS_RE.search(html))


def run_signup(conn, cfg: dict, project: dict, *, headless: bool = False,
               limit: int = 0, assist: bool = False,
               handoff_timeout: float = 300) -> dict:
    """Signup on all pending register candidates for one project. Serial.

    `assist` requires a visible browser: on captcha it pauses, writes a
    handoff status file, and waits for the operator's `.go` marker before
    continuing on the same page. No captcha solving is attempted here.
    """
    if sync_playwright is None:
        raise RuntimeError("playwright not installed — pip install playwright"
                           " && playwright install chromium")

    from .executor import build_context
    rows = signup_candidates(conn, project["_id"])
    rows = rows[:limit] if limit else rows
    stats = {"due": len(rows), "signed": 0, "human": 0, "failed": 0,
             "rows": []}
    if not rows:
        return stats

    with sync_playwright() as pw:
        browser = _launch(pw, headless)
        page = browser.new_page()
        try:
            for row in rows:
                dom = row["pool_domain"]
                if _recent_signup_failures(conn, dom) >= REPEAT_FAIL_LIMIT:
                    stats["skipped"] = stats.get("skipped", 0) + 1
                    stats["rows"].append({"domain": dom, "state": "human",
                                          "note": "repeat-fail downgrade"})
                    continue
                ctx = build_context(conn, project, row)
                ctx["username"] = (ctx.get("sender_name") or
                                   project["domain"].split(".")[0])
                base_url = (row["entry_url"] or row["url"]).rstrip("/")
                # static discovery first (cheap); when it can't confirm a form
                # (bot-blocked/JS-only) the CUA walks entry + site-root signup
                # paths until one renders a form.
                root = site_root(base_url)
                discovered = _discover_signup_url(root + "/")
                candidates = signup_candidate_urls(base_url, discovered)
                outcome = {"state": "human", "note": "not attempted"}
                try:
                    html, ok, cap, solved = "", False, "none", False
                    for u in candidates:
                        page.goto(u, timeout=30000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                        html = page.content()
                        cap = signup_captcha_hint(html)
                        if cap != "none":
                            break
                        ok, _, _ = find_signup_form(html)
                        if ok:
                            entry = u
                            break
                    if cap != "none":
                        if assist and not headless:
                            from . import handoff as handoff_mod
                            handoff_mod.request_handoff(
                                cfg, dom, url=page.url, note=f"captcha={cap}")
                            print(f"[handoff] {dom}: captcha={cap} — "
                                  f"solve it in the browser window, then "
                                  f"touch: {handoff_mod.go_path(cfg, dom)}",
                                  flush=True)
                            if handoff_mod.wait_for_go(
                                    cfg, dom, timeout=handoff_timeout):
                                page.wait_for_timeout(1000)
                                html = page.content()
                                # A solved turnstile leaves its widget markup
                                # in the DOM — the .go marker is the human
                                # attestation, not HTML re-detection.
                                solved = True
                                cap = cap_after_handoff(solved, html)
                                ok, _, _ = find_signup_form(html)
                            else:
                                outcome = {"state": "human",
                                           "note": f"captcha={cap}; handoff "
                                                   "timed out"}
                        if not solved and cap != "none":
                            outcome = {"state": "human",
                                       "note": f"captcha={cap} -> manual signup"}
                    if cap == "none" and not ok:
                        outcome = {"state": "human",
                                   "note": "no signup form found "
                                           "(entry + common paths)"}
                    elif cap != "none" and not solved:
                        pass   # captcha block above already set the human outcome
                    else:
                        pw_form = gen_password()
                        filled = _fill_signup(page, ctx, pw_form)
                        if not any("pass" in f for f in filled):
                            outcome = {"state": "human",
                                       "note": "password field not fillable"}
                        else:
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(4000)
                            html2 = page.content()
                            if _looks_successful(html2, True):
                                accounts_mod.add_account(
                                    conn, project["_id"], dom,
                                    ctx["email"],
                                    username=ctx["username"],
                                    password=pw_form,
                                    status="pending-activation",
                                    note="auto signup (CUA)")
                                outcome = {"state": "signed",
                                           "note": "signup accepted, "
                                                   "pending activation"}
                            else:
                                outcome = {"state": "human",
                                           "note": "no success marker "
                                                   "after submit; needs "
                                                   "manual check"}
                except Exception as e:  # noqa: BLE001 — one site must not
                    outcome = {"state": "failed",   # kill the batch
                               "note": str(e)[:150]}
                state = outcome["state"]
                stats[{"signed": "signed", "human": "human",
                       "failed": "failed"}.get(state, "failed")] += 1
                conn.execute(
                    """INSERT INTO run_log(action, target, result, note)
                       VALUES('signup', ?, ?, ?)""",
                    (f"{dom} match#{row['match_id']}",
                     "ok" if state == "signed" else state,
                     outcome["note"]))
                conn.commit()
                stats["rows"].append({"domain": dom, "state": state,
                                      "note": outcome["note"]})
        finally:
            browser.close()
    return stats
