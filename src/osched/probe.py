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

# Contact / feedback / newsletter handlers are NOT submission endpoints even
# when they happen to carry a `URL` text field (pcastuces /soumettre.htm has
# Nom+E-mail+URL+commentaire but posts to erreur404.asp = contact handler;
# smoothdirectory posts cdt_contact/cdt_subscribe to admin-post.php). Treating
# these as listing forms produced a wave of false `submitted` plans.
CONTACT_ACTION_RE = re.compile(
    r"(contact|kontakt|erreur|merci|feedback|complain|newsletter|"
    r"subscribe|mailto)", re.I)
CONTACT_HIDDEN_RE = re.compile(
    r"name\s*=\s*['\"](?:action|task|op)['\"][^>]*value\s*=\s*['\"]"
    r"(?:cdt_contact|cdt_subscribe|contact|subscribe|feedback)", re.I)

# A site-search box can never publish a listing, yet Google CSE / WordPress
# search widgets carry hidden `siteurl` + `ref` fields that look exactly like a
# destination. jayisgames.com/game-submit was flagged as a submission page for
# this reason. Detect the search box and skip the whole form.
SEARCH_FIELD_NAMES = {"q", "s", "query", "search", "keyword", "keywords",
                      "searchterm", "search_query", "searchterm"}
INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.I)
INPUT_NAME_RE = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']", re.I)
INPUT_TYPE_RE = re.compile(r"type\s*=\s*[\"']([^\"']+)[\"']", re.I)
NON_ENTRY_TYPES = {"hidden", "submit", "button", "image", "reset", "checkbox",
                   "radio", "file"}


def input_fields(block: str) -> list[tuple[str, str]]:
    """(name, type) for every named input in a form block; type defaults text."""
    out: list[tuple[str, str]] = []
    for tag in INPUT_TAG_RE.findall(block):
        name_m = INPUT_NAME_RE.search(tag)
        if not name_m:
            continue
        type_m = INPUT_TYPE_RE.search(tag)
        out.append((name_m.group(1), (type_m.group(1) if type_m else "text").lower()))
    return out


def is_search_form(fields: list[tuple[str, str]]) -> bool:
    """True for a keyword search box (visible `q`/`search` text input)."""
    return any(name.lower() in SEARCH_FIELD_NAMES and kind not in NON_ENTRY_TYPES
               for name, kind in fields)


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


def state_fields(html: str) -> dict[str, str]:
    """Hidden / anti-CSRF inputs and the values the page shipped with them.

    ASP.NET (`__VIEWSTATE`, `__EVENTVALIDATION`), Rails (`authenticity_token`)
    and nonce fields must be posted back unchanged; sending them empty is an
    automatic server-side rejection.
    """
    out: dict[str, str] = {}
    for tag in re.finditer(r"<input\b[^>]*>", html, re.I):
        t = tag.group(0)
        if not re.search(r"""type\s*=\s*["']hidden["']""", t, re.I):
            if not re.search(r"""name\s*=\s*["'](__|\w*(csrf|token|nonce)\w*)["']""",
                             t, re.I):
                continue
        nm = re.search(r"""name\s*=\s*["']([^"']+)["']""", t, re.I)
        vm = re.search(r"""value\s*=\s*["']([^"']*)["']""", t, re.I)
        if nm:
            out[nm.group(1)] = vm.group(1) if vm else ""
    return out


def find_submit_form(html: str) -> tuple[bool, list[str], str]:
    """First form that accepts a destination URL: (ok, fields, action)."""
    for form_m in re.finditer(r"<form\b.*?</form>", html, re.I | re.S):
        block = form_m.group(0)
        fields = FORM_FIELD_RE.findall(block)
        names = [f.lower() for f in fields]
        if not names:
            continue
        typed = input_fields(block)
        if is_search_form(typed):
            continue        # keyword search box, not a submission form
        if any(CREDENTIAL_FIELD_RE.search(n) for n in names):
            continue        # login / signup / password reset
        open_tag = block[: block.find(">") + 1]
        action_m = FORM_ACTION_RE.search(open_tag)
        action = action_m.group(1) if action_m else ""
        if action and CREDENTIAL_ACTION_RE.search(action):
            continue        # credential endpoint behind a plain form
        if action and CONTACT_ACTION_RE.search(action):
            continue        # contact/feedback handler, not a listing submit
        if CONTACT_HIDDEN_RE.search(block):
            continue        # hidden action=cdt_contact / subscribe
        # The destination field must be something we can actually type into:
        # a hidden `siteurl` is search/referrer plumbing, not a listing field.
        if not any(k in name.lower() and kind not in NON_ENTRY_TYPES
                   for name, kind in typed for k in URL_FIELD_KEYS):
            continue        # search box, newsletter box, contact form
        return True, fields, action
    return False, [], ""


def _looks_like_submit_form(html: str) -> tuple[bool, list[str]]:
    """A form whose inputs accept a destination URL (not a login/signup)."""
    ok, fields, _ = find_submit_form(html)
    return ok, fields


# --------------------------------------------------------------------------
# POST response copy recognition — a 2xx alone proves nothing: form plugins
# re-render the same page with an inline error at HTTP 200.
# --------------------------------------------------------------------------

SUCCESS_COPY_RE = re.compile(
    r"thank(?:s| you)[^<]{0,60}(submission|submit|listing|link|adding)"
    r"|succes{2}fully? (?:submitted|added|received|listed|published)"
    r"|(?:link|site|url|listing|submission) (?:was|has been|is) "
    r"(?:submitted|added|received|approved|published|accepted|scheduled)"
    r"|pending (?:review|approval)|will be (?:reviewed|approved|listed)"
    r"|await(?:ing)? (?:review|approval)|under review"
    r"|submitted (?:successfully|for review)|added (?:successfully|to (?:our|the) )"
    r"|提交成功|已提交|感谢(?:您的?|你的)?(?:提交|投稿)",
    re.I)

FAILURE_COPY_RE = re.compile(
    r"\berror\b|invalid|incorrect|not a valid|(?:is|are) required"
    r"|required field|please (?:enter|fill|provide|select|fix|check|complete)"
    r"|must (?:be|contain|include)|cannot be (?:empty|blank)|field is empty"
    r"|verification (?:failed|code)|captcha (?:error|required|failed|invalid)"
    r"|submission (?:failed|rejected)|could not (?:be )?(?:added|submit|save)"
    r"|something went wrong|failed to (?:submit|add|save)"
    r"|请填写|必填|验证码|提交失败|请输入",
    re.I)

ALREADY_COPY_RE = re.compile(
    r"already (?:submitted|exists|listed|in (?:our|the) (?:directory|database|list))"
    r"|duplicate (?:submission|entry|url|listing)"
    r"|already in our|重复提交|已存在",
    re.I)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b.*?</\1\s*>", re.I | re.S)


def copy_verdict(body: str) -> dict[str, str]:
    """Scan a POST response body for success/failure/already-listed copy.

    Script and style blocks are stripped first: JS error strings in page
    chrome must not read as failure copy. Returns {success, failure, already}
    with the first matched snippet (trimmed) or "" per key.
    """
    out = {"success": "", "failure": "", "already": ""}
    if not body:
        return out
    text = _SCRIPT_STYLE_RE.sub(" ", body)
    for key, rex in (("success", SUCCESS_COPY_RE),
                     ("failure", FAILURE_COPY_RE),
                     ("already", ALREADY_COPY_RE)):
        m = rex.search(text)
        if m:
            out[key] = " ".join(m.group(0).split())[:80]
    return out


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
