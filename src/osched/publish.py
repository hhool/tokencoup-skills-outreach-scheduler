"""Register channel stage 2: publish a link from an active account.

The chain: signup (signup.py) -> IMAP verification (accounts.fetch_verification_mails)
-> activation (accounts.run_activation) -> **this module** -> verify (verify.py).

A successful publish MUST create a plan row with state='submitted' and a
result_note carrying the landing URL — otherwise the existing verify chain
(due_verify -> link_check -> evidence) cannot observe the link at all.

中文：带链发文。账号 active 后，CUA 登录平台、找到可放 URL 的表单、嵌入落地页链接
并提交；成功必须落 plan(submitted)，否则验活/证据链路断掉。
"""
from __future__ import annotations

import json
import re

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from . import probe as probe_mod
from .cua import _launch

REPEAT_FAIL_LIMIT = 3

POST_SUCCESS_RE = re.compile(
    r"thank|success|submitted|published|added|created|saved|your (?:post|"
    r"listing|article|link)|is now live|awaiting (?:review|moderation)", re.I)


# --------------------------------------------------------------------------
# form recognition (pure helpers, testable without a browser)
# --------------------------------------------------------------------------

def find_login_form(html: str) -> tuple[bool, list[str], str]:
    """A credential form: password + email/username identity, 2+ fields."""
    for block in re.findall(r"<form\b.*?</form>", html, re.I | re.S):
        names = re.findall(r'name="([^"]+)"', block)
        low = [n.lower() for n in names]
        if not low or len(low) < 2:
            continue
        has_pw = any("pass" in n for n in low)
        has_id = any(k in n for n in low
                     for k in ("mail", "user", "login", "nick", "account"))
        if has_pw and has_id:
            open_tag = block[: block.find(">") + 1]
            m = re.search(r'action="([^"]*)"', open_tag, re.I)
            return True, names, (m.group(1) if m else "")
    return False, [], ""


def looks_published(html: str, had_form: bool) -> bool:
    return had_form and bool(POST_SUCCESS_RE.search(html))


def publish_candidates(conn, project_id: int) -> list:
    """Confirmed register matches that have an active account and no plan."""
    return conn.execute(
        """
        SELECT m.id AS match_id, a.id AS account_id, a.email, a.username,
               a.password, p.id AS pool_id, p.domain AS pool_domain,
               p.url, p.entry_url
        FROM match m
        JOIN account a
          ON a.project_id=m.project_id AND a.dir_domain=
             (SELECT domain FROM pool WHERE id=m.pool_id)
        JOIN pool p ON p.id=m.pool_id
        WHERE m.project_id=? AND m.state='confirmed'
          AND a.status='active'
          AND NOT EXISTS (SELECT 1 FROM plan pl
                          WHERE pl.match_id=m.id AND pl.channel='register')
        ORDER BY p.domain
        """, (project_id,)).fetchall()


# --------------------------------------------------------------------------
# CUA publish flow
# --------------------------------------------------------------------------

def _fill(page, ctx: dict, password: str, *, login: bool = False) -> list[str]:
    """Fill visible fields. In login mode only credential fields are touched;
    in publish mode only the link-bearing content fields are."""
    filled: list[str] = []
    for el in page.query_selector_all("input, textarea, select"):
        try:
            name = (el.get_attribute("name") or el.get_attribute("id")
                    or "").lower()
            typ = (el.get_attribute("type") or "text").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if not el.is_visible():
                continue
            if login and not any(k in name for k in
                                 ("pass", "mail", "user", "login", "nick")):
                continue
            if not login and any(k in name for k in ("pass", "mail")):
                continue          # never touch credentials in publish mode
            if "pass" in name:
                el.fill(password)
                filled.append(name)
            elif "mail" in name:
                el.fill(ctx["email"])
                filled.append(name)
            elif any(k in name for k in ("user", "login", "nick", "account")):
                el.fill(ctx["username"])
                filled.append(name)
            elif any(k in name for k in ("url", "link", "site", "web")):
                el.fill(ctx["url"])
                filled.append(name)
            elif any(k in name for k in ("title", "name")):
                el.fill(ctx["title"])
                filled.append(name)
            elif any(k in name for k in ("desc", "summary", "text", "comment")):
                el.fill(ctx["description"][:500])
                filled.append(name)
        except Exception:  # noqa: BLE001 — skip individual bad fields
            continue
    return filled


def run_publish(conn, cfg: dict, project: dict, *, headless: bool = False,
                limit: int = 0) -> dict:
    """Publish on all ready accounts in one real browser (serial)."""
    if sync_playwright is None:
        raise RuntimeError("playwright not installed — pip install playwright")

    from .executor import build_context
    rows = publish_candidates(conn, project["_id"])
    rows = rows[:limit] if limit else rows
    stats = {"due": len(rows), "published": 0, "human": 0, "failed": 0,
             "rows": []}
    if not rows:
        return stats

    with sync_playwright() as pw:
        browser = _launch(pw, headless)
        page = browser.new_page()
        try:
            for row in rows:
                dom = row["pool_domain"]
                ctx = build_context(conn, project, row)
                ctx["email"] = row["email"]
                ctx["username"] = row["username"] or row["email"].split("@")[0]
                outcome = {"state": "human", "note": "not attempted"}
                try:
                    page.goto(row["entry_url"] or row["url"], timeout=30000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    html = page.content()
                    password = row["password"] or ""
                    ok_login, _, _ = find_login_form(html)
                    if ok_login:
                        # account exists but session is expired: log in first
                        _fill(page, ctx, password, login=True)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(3000)
                        html = page.content()
                    ok_form, fields, _ = probe_mod.find_submit_form(html)
                    if not ok_form:
                        outcome = {"state": "human",
                                   "note": "no link-bearing form after login"}
                    else:
                        _fill(page, ctx, password)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(4000)
                        html2 = page.content()
                        if not looks_published(html2, True):
                            outcome = {"state": "human",
                                       "note": "no publish success marker; "
                                               "needs manual check"}
                        else:
                            # plan row is mandatory: verify/evidence join on it
                            conn.execute(
                                """INSERT OR IGNORE INTO plan
                                   (project_id, match_id, scheduled_date,
                                    channel, state, executed_at, result_note)
                                   VALUES(?,?,date('now'),'register',
                                     'submitted', datetime('now'), ?)""",
                                (project["_id"], row["match_id"],
                                 json.dumps({"landing": ctx["url"],
                                             "channel": "register"},
                                            ensure_ascii=False)))
                            conn.execute(
                                """INSERT INTO run_log(action,target,result,note)
                                   VALUES('publish',?,?,?)""",
                                (f"{dom} match#{row['match_id']}", "ok",
                                 "link posted via CUA"))
                            conn.commit()
                            outcome = {"state": "published",
                                       "note": "link posted, plan submitted"}
                except Exception as e:  # noqa: BLE001 — one site never stops
                    outcome = {"state": "failed", "note": str(e)[:150]}
                stats[{"published": "published", "human": "human",
                       "failed": "failed"}.get(outcome["state"], "failed")] += 1
                stats["rows"].append({"domain": dom, "state": outcome["state"],
                                      "note": outcome["note"]})
        finally:
            browser.close()
    return stats
