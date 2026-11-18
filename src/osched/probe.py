"""HTTP probe stage (DESIGN.md §4 + §4c).

Decides "submittable or not" for pool entries: fetch the page, discover
submit/register endpoints, extract form fields, captcha type and contact
email. Every discovered endpoint persists to probe_cache — recorded, reused
across submissions and same-CMS sites, and kept fresh (pre-submit check,
failure re-probe, 30-day audit).

Stdlib-only HTTP (urllib) — no requests dependency (DESIGN.md §9.1).
中文：探测阶段判定「是否可投递」；发现物持久化进 probe_cache 记录/复用/保鲜。
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "outreach-scheduler/1.0 (+website-submission-discovery)"

# Link href/text patterns that hint at a submit/register endpoint.
# Multi-language (directory sites are not English-only).
SUBMIT_LINK_RE = re.compile(
    r"(?:submit|add[-_]?link|add[-_]?url|suggest[-_]?link|add[-_]?site|"
    r"submission|register|inserisci[-_]?link|anmelden"
    r"|toevoegen|link[-_]?toevoegen|aanmelden"          # nl
    r"|ajouter|soumettre"                                # fr
    r"|anadir|a\u00f1adir|alta"                          # es
    r"|aggiungi|inserisci"                               # it
    r"|hinzufuegen|hinzuf\u00fcgen|eintragen"            # de
    r"|melunak|ekle"                                     # tr
    r")",
    re.I,
)

# A "submission form" = a form containing at least one of these input names.
FORM_FIELD_RE = re.compile(
    r"<input[^>]+name\s*=\s*[\"']([^\"']+)[\"']", re.I
)
FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.I)
FORM_ACTION_RE = re.compile(r"action\s*=\s*[\"']([^\"']*)[\"']", re.I)

# Captcha markers, most specific first. Order matters: v3/render markers are
# checked before generic recaptcha strings.
CAPTCHA_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("recaptcha_v3", ("recaptcha/api.js?render=", "grecaptcha.execute")),
    ("recaptcha_v2", ("sitekey", "g-recaptcha", "recaptcha/api.js")),
    ("turnstile", ("challenges.cloudflare.com/turnstile", "cf-turnstile")),
    ("hcaptcha", ("h-captcha", "hcaptcha.com/1/api.js")),
    ("email_code", ("confirmation code", "verification code sent")),
]

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

# Emails on obvious placeholder/junk hosts are ignored.
EMAIL_SKIP_RE = re.compile(
    r"(example\.(com|org)|sentry\.io|wixpress|@2x|domain\.com|email\.com|"
    r"yourdomain|yoursite|test@test)",
    re.I,
)

BLOCK_STATUS = {403, 412, 429}  # Cloudflare anti-bot — keep, mark bot-blocked

# A *submission* form must carry a destination-ish field: that is what we
# post a landing URL into. "site_name"/"title" alone is a signup wizard.
URL_FIELD_KEYS = ("url", "link", "website", "homepage", "domain")
# Credential forms are never submission forms, even when a signup wizard
# asks for a "site" field on step one.
CREDENTIAL_FIELD_RE = re.compile(r"(pass|pwd|login|username)", re.I)
CREDENTIAL_ACTION_RE = re.compile(
    r"(login|log-in|signin|sign-in|sign_in|wp-login|auth/|session/new)", re.I)


@dataclass
class ProbeResult:
    """Outcome of probing one site (one pool row)."""

    state: str = "unprobed"            # probed | unreachable
    entry_url: str = ""                # best submit endpoint found
    has_submit_form: bool = False
    form_fields: list[str] = field(default_factory=list)
    captcha_type: str = "none"         # none|recaptcha_v2|...|unknown
    contact_email: str = ""
    http_status: int = 0
    note: str = ""

    def form_signature(self) -> str:
        """Hash of the form's field names — the reusable field mapping."""
        if not self.form_fields:
            return ""
        canon = ",".join(sorted(f.lower() for f in self.form_fields))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _fetch(url: str, timeout: float) -> tuple[int, str, str]:
    """GET a URL. Returns (http_status, final_url, body). status 0 = error."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read(512 * 1024).decode(charset, errors="replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except (urllib.error.URLError, socket.timeout, OSError, ValueError):
        return 0, url, ""


def _detect_captcha(html: str) -> str:
    low = html.lower()
    for kind, markers in CAPTCHA_MARKERS:
        if any(m.lower() in low for m in markers):
            return kind
    if re.search(r"<img[^>]+captcha", low):
        return "image"
    return "none"


def _extract_emails(html: str) -> list[str]:
    seen: list[str] = []
    for m in EMAIL_RE.finditer(html):
        addr = m.group(0).lower()
        if EMAIL_SKIP_RE.search(addr) or addr not in seen:
            if not EMAIL_SKIP_RE.search(addr):
                seen.append(addr)
    return seen


def _absolutize(href: str, base_url: str) -> str | None:
    try:
        parts = urllib.parse.urlparse(href)
        if parts.scheme in ("http", "https"):
            return href
        if href.startswith("//"):
            scheme = urllib.parse.urlparse(base_url).scheme or "http"
            return scheme + ":" + href
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None
        return urllib.parse.urljoin(base_url, href)
    except ValueError:
        return None


def find_entry_candidates(html: str, base_url: str, limit: int) -> list[str]:
    """Links on the homepage that look like submit/register endpoints."""
    out: list[str] = []
    for m in re.finditer(r"""<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
                         html, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if not (SUBMIT_LINK_RE.search(href) or SUBMIT_LINK_RE.search(text)):
            continue
        absu = _absolutize(href, base_url)
        if absu and absu not in out:
            out.append(absu)
        if len(out) >= limit:
            break
    return out


def _looks_like_submit_form(html: str) -> tuple[bool, list[str]]:
    """A form whose inputs accept a destination URL (not a login/signup)."""
    for form_m in re.finditer(r"<form\b.*?</form>", html, re.I | re.S):
        block = form_m.group(0)
        fields = FORM_FIELD_RE.findall(block)
        names = [f.lower() for f in fields]
        if not names:
            continue
        if any(CREDENTIAL_FIELD_RE.search(n) for n in names):
            continue        # login / signup / password reset
        open_tag = block[: block.find(">") + 1]
        action_m = FORM_ACTION_RE.search(open_tag)
        if action_m and CREDENTIAL_ACTION_RE.search(action_m.group(1)):
            continue        # credential endpoint behind a plain form
        if not any(k in n for n in names for k in URL_FIELD_KEYS):
            continue        # search box, newsletter box, contact form
        return True, fields
    return False, []


def probe_site(url: str, *, timeout: float = 12.0,
               max_candidates: int = 3) -> ProbeResult:
    """Probe one site: homepage first, then submit-endpoint candidates."""
    res = ProbeResult()
    status, final_url, html = _fetch(url, timeout)

    if status == 0:
        res.state, res.captcha_type = "unreachable", "unknown"
        res.note = "fetch error"
        return res
    if status in BLOCK_STATUS:
        res.state, res.captcha_type = "probed", "unknown"
        res.http_status = status
        res.note = "anti-bot blocked (kept, bot-blocked)"
        return res
    if status >= 400:
        res.state, res.captcha_type = "unreachable", "unknown"
        res.http_status = status
        res.note = f"http {status}"
        return res

    res.http_status = status
    emails = _extract_emails(html)
    candidates = find_entry_candidates(html, final_url, max_candidates)

    # The homepage itself may host the submission form.
    best_entry, best_fields, form_page_html = "", [], html
    if FORM_TAG_RE.search(html):
        ok, fields = _looks_like_submit_form(html)
        if ok:
            best_entry, best_fields = final_url, fields

    # Then try endpoint candidates.
    for cand in candidates:
        cstatus, cfinal, chtml = _fetch(cand, timeout)
        if cstatus != 200 or not chtml:
            continue
        emails.extend(_extract_emails(chtml))
        ok, fields = _looks_like_submit_form(chtml)
        if ok and not best_entry:
            best_entry, best_fields, form_page_html = cfinal, fields, chtml
        if best_entry and emails:
            break

    res.state = "probed"
    res.entry_url = best_entry
    res.has_submit_form = bool(best_entry)
    res.form_fields = best_fields
    res.captcha_type = _detect_captcha(form_page_html)
    res.contact_email = emails[0] if emails else ""
    if not res.has_submit_form:
        res.note = ("no submit form found" if not candidates
                    else "candidates had no usable form")
    return res


# --------------------------------------------------------------------------
# Persistence: pool update + probe_cache upsert (§4c record/reuse/freshness)
# --------------------------------------------------------------------------

def record_result(conn, pool_id: int, url: str, domain: str,
                  res: ProbeResult, *, source_kind: str = "") -> str:
    """Write a probe outcome to the pool row and probe_cache.

    Returns the cache action: "inserted" | "unchanged" | "changed".
    """
    fields_json = json.dumps(res.form_fields, ensure_ascii=False)

    conn.execute(
        """
        UPDATE pool SET
          entry_url=?, has_submit_form=?, captcha_type=?, contact_email=?,
          probe_state=?, status=CASE
            WHEN ? IN (403,412,429) THEN 'bot-blocked'
            WHEN ?=0 AND ?='unreachable' THEN 'dead'
            ELSE status END,
          last_seen=datetime('now')
        WHERE id=?
        """,
        (res.entry_url, int(res.has_submit_form), res.captcha_type,
         res.contact_email, res.state, res.http_status, res.http_status,
         res.state, pool_id),
    )

    action = "unchanged"
    prev = conn.execute(
        "SELECT id, entry_url, form_signature FROM probe_cache WHERE domain=?",
        (domain,),
    ).fetchone()

    if prev is None:
        conn.execute(
            """
            INSERT INTO probe_cache(domain, entry_url, form_signature,
                captcha_type, contact_email, state)
            VALUES(?,?,?,?,?,'valid')
            """,
            (domain, res.entry_url or url, res.form_signature(),
             res.captcha_type, res.contact_email),
        )
        action = "inserted"
    else:
        sig = res.form_signature()
        if (prev["entry_url"] != (res.entry_url or url)
                or (sig and prev["form_signature"] != sig)):
            hist = json.loads(conn.execute(
                "SELECT history_json FROM probe_cache WHERE id=?",
                (prev["id"],)).fetchone()["history_json"])
            hist.append({
                "ts_note": "entry changed",
                "old_entry": prev["entry_url"],
                "new_entry": res.entry_url or url,
                "reason": res.note or "re-probe",
            })
            conn.execute(
                """
                UPDATE probe_cache SET entry_url=?, form_signature=?,
                  captcha_type=?, contact_email=?, last_probed=datetime('now'),
                  state='valid', history_json=?
                WHERE id=?
                """,
                (res.entry_url or url, sig, res.captcha_type,
                 res.contact_email, json.dumps(hist, ensure_ascii=False),
                 prev["id"]),
            )
            action = "changed"
        else:
            conn.execute(
                """UPDATE probe_cache SET last_probed=datetime('now'),
                   captcha_type=?, contact_email=? WHERE id=?""",
                (res.captcha_type, res.contact_email, prev["id"]),
            )

    conn.execute(
        "INSERT INTO run_log(action, target, result, note) VALUES('probe',?,?,?)",
        (url, res.state, json.dumps(
            {"entry": res.entry_url, "captcha": res.captcha_type,
             "cache": action, "http": res.http_status},
            ensure_ascii=False)),
    )
    conn.commit()
    return action
