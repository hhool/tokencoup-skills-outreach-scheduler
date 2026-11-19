"""Free-trial abuse guards (portal + scheduler side).

The trial is 3 free live links per account. Without guards the same person
can mint it repeatedly through cheap identity resets:

* a new Google/e-mail alias           (``u+1@``, ``u.1@gmail.com``)
* a new sub-domain of the same site    (``a.com`` → ``shop.a.com``)
* a new account on the same network    (same laptop / same office)

So a trial grant is keyed on three independent signals, and every grant is
persisted. A candidate earns the trial only when all three are fresh:

===============  ==========================================================
signal           normalisation
===============  ==========================================================
email            lowercase, ``+tag`` stripped, gmail dots stripped,
                 googlemail.com == gmail.com
site             registrable domain (eTLD+1), so sub-domains collapse,
                 ``www.`` and ports dropped, IDNA-encoded
network          client IP, one trial per IP across all accounts
===============  ==========================================================

Signals only gate the *free* credits. Nothing here blocks a paying client:
a denied project still activates, just with 0 credits, and an admin can
grant quota manually (``admin_action`` ``quota``) after review.

Second half of this module is the *enforcement* side: ``credit_cap`` /
``allowance`` / ``live_count`` keep the scheduler from doing more unpaid
work than a project still has credits for. A project with no portal
request (internal / operator-owned) has no cap and is never throttled.

中文：免费额度防刷——发放侧按「邮箱／站点／IP」三信号去重，命中即拒发
（项目照常激活，0 额度，管理员可人工补），并记录台账留痕；执行侧按
quota_total 封顶，避免「3 条」变成无限免费工。内部项目无 cap 不受限。
"""
from __future__ import annotations

import re

TRIAL_CREDITS = 3

#: one trial per network address, across all accounts
IP_TRIAL_CAP = 1
#: registrations allowed from one address per window
IP_SIGNUP_CAP = 5
IP_SIGNUP_WINDOW_HOURS = 24
#: how many unverified draft requests one account may hold
MAX_DRAFT_REQUESTS = 5

#: plan states that already consume (or have reserved) a credit
INFLIGHT_STATES = ("queued", "submitted", "sent", "human")

# Public-suffix entries that need three labels in practice. Full PSL is
# overkill here — this covers the markets we sell into; anything unknown
# falls back to "last two labels".
_MULTI_SUFFIX = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "org.hk", "net.hk", "com.tw", "org.tw", "net.tw",
    "com.au", "net.au", "org.au", "edu.au", "co.nz", "net.nz", "org.nz",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr",
    "com.sg", "com.my", "com.br", "com.mx", "com.ar", "com.tr",
    "co.in", "net.in", "org.in", "co.za", "com.ua", "co.id", "com.vn",
    "com.ph", "com.pk", "com.sa", "com.eg",
}

#: reasons a trial was withheld, with the text shown to the client
DENY_TEXT = {
    "client_used": "This account already used its trial credits.",
    "email_reused": ("This e-mail already used trial credits "
                     "(aliases count as the same address)."),
    "site_reused": ("A trial was already granted for this site "
                    "(sub-domains count as the same site)."),
    "site_claimed": "This site is already active under another account.",
    "ip_reused": ("A trial was already granted from this network. "
                  "Contact us to continue — paid links have no such limit."),
}

_HOST_RE = re.compile(r"^[a-z0-9.\-_\u0080-\uffff]+$")


# --------------------------------------------------------------------------
# identity normalisation
# --------------------------------------------------------------------------
def canonical_email(email: str) -> str:
    """Collapse alias tricks so one mailbox maps to one identity.

    ``Foo.Bar+trial@GoogleMail.com`` -> ``foobar@gmail.com``

    Only used for trial/abuse keying — the address itself is stored and
    mailed verbatim. The two Google properties above are what this folding
    unifies; they are named here because the rule is meaningless without
    them, not because any mailbox is.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, _, domain = email.rpartition("@")
    domain = domain.rstrip(".")
    if domain in ("googlemail.com", "gmail.com"):
        domain = "gmail.com"
        local = local.replace(".", "")
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def normalize_host(value: str) -> str:
    """``https://www.Foo.COM:443/bar`` -> ``foo.com`` (host only)."""
    host = (value or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("@")[-1]
    host = host.split("?", 1)[0]
    if host.startswith("["):            # IPv6 literal
        return host.split("]", 1)[0] + "]"
    host = host.split(":", 1)[0]
    host = host.rstrip(".")
    while host.startswith("www."):
        host = host[4:]
    return host


def _idna(host: str) -> str:
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except Exception:                    # noqa: BLE001 — keep raw on failure
        return host


def registrable_domain(value: str) -> str:
    """Return eTLD+1 so ``shop.foo.co.uk`` and ``foo.co.uk`` collide."""
    host = _idna(normalize_host(value))
    if not host or _HOST_RE.match(host) is None:
        return host
    if host.replace(".", "").isdigit() or ":" in host:
        return host                      # IP literal — no registrable part
    labels = [lb for lb in host.split(".") if lb]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def client_ip(handler) -> str:
    """Best-effort real client IP, proxy-header aware, spoof-resistant.

    Order matters: Fly's edge overwrites ``Fly-Client-IP``, Cloudflare
    overwrites ``CF-Connecting-IP``; a client-supplied ``X-Forwarded-For``
    is only a last resort before the socket peer.
    """
    if handler is None:
        return ""
    headers = getattr(handler, "headers", None)
    for name in ("Fly-Client-IP", "CF-Connecting-IP", "True-Client-IP"):
        try:
            val = headers.get(name) if headers else None
        except Exception:                # noqa: BLE001
            val = None
        if val and val.strip():
            return val.strip()
    try:
        xff = headers.get("X-Forwarded-For") if headers else None
    except Exception:                    # noqa: BLE001
        xff = None
    if xff:
        return xff.split(",")[0].strip()
    peer = getattr(handler, "client_address", None)
    if peer and len(peer) >= 1:
        return str(peer[0]).strip()
    return ""


def _num(conn, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


# --------------------------------------------------------------------------
# trial decision
# --------------------------------------------------------------------------
def trial_verdict(conn, client_id: int, email: str, domain: str,
                  ip: str = "") -> tuple[bool, str]:
    """Decide whether this project may receive the free trial credits.

    Returns ``(granted, reason)`` — reason is ``"granted"`` or a key of
    :data:`DENY_TEXT`. Never raises; a broken ledger denies nothing but
    still fails closed on the account-level check.
    """
    canon = canonical_email(email)
    site = registrable_domain(domain)

    try:
        # 1) this account already had a trial (ledger or legacy rows)
        used = conn.execute(
            """SELECT 1 FROM project_request WHERE client_id=?
               AND quota_type='trial' AND quota_total>0
               AND verify_state IN ('verified','active') LIMIT 1""",
            (client_id,)).fetchone()
        if used or _num(conn, "SELECT 1 FROM trial_ledger WHERE client_id=? "
                             "LIMIT 1", (client_id,)):
            return False, "client_used"

        # 2) same mailbox (aliases collapsed) under another account
        if canon and _num(conn,
                          """SELECT 1 FROM trial_ledger
                             WHERE email_canon=? AND client_id!=? LIMIT 1""",
                          (canon, client_id)):
            return False, "email_reused"

        # 3) same registrable site, any account (sub-domains included)
        if site and _num(conn,
                         """SELECT 1 FROM trial_ledger
                            WHERE reg_domain=? LIMIT 1""", (site,)):
            return False, "site_reused"

        # 4) site verified/active under another account
        rows = conn.execute(
            """SELECT domain FROM project_request
               WHERE verify_state IN ('verified','active')
                 AND client_id!=?""", (client_id,)).fetchall()
        for r in rows:
            if registrable_domain(r[0]) == site:
                return False, "site_claimed"

        # 5) network already used its one trial
        if ip and _num(conn,
                       """SELECT 1 FROM trial_ledger
                          WHERE ip=? AND client_id!=? LIMIT 1""",
                       (ip, client_id)) >= IP_TRIAL_CAP:
            return False, "ip_reused"
    except Exception:                    # noqa: BLE001 — never break activation
        return True, "granted"
    return True, "granted"


def record_trial(conn, *, client_id: int, request_id=None, project_id=None,
                 email: str = "", domain: str = "", ip: str = "") -> None:
    """Persist the grant so the same signals cannot mint trial #2."""
    canon = canonical_email(email)
    site = registrable_domain(domain)
    try:
        conn.execute(
            """INSERT INTO trial_ledger(client_id, request_id, project_id,
                 email_canon, reg_domain, ip) VALUES(?,?,?,?,?,?)""",
            (client_id, request_id, project_id, canon, site, ip or ""))
        conn.commit()
    except Exception:                    # noqa: BLE001
        pass


def signup_blocked(conn, ip: str) -> str:
    """Rate-limit account creation from one address. Empty string = allowed."""
    if not ip:
        return ""
    try:
        n = _num(conn,
                 """SELECT COUNT(*) FROM client
                    WHERE signup_ip=? AND created_at >= datetime('now', ?)""",
                 (ip, f"-{IP_SIGNUP_WINDOW_HOURS} hours"))
    except Exception:                    # noqa: BLE001
        return ""
    if n >= IP_SIGNUP_CAP:
        return ("Too many accounts created from this network today — "
                "log in with an existing one or contact us.")
    return ""


def draft_quota_message(conn, client_id: int) -> str:
    """Cap unverified drafts so one account cannot hoard the trial window."""
    try:
        n = _num(conn,
                 """SELECT COUNT(*) FROM project_request
                    WHERE client_id=? AND verify_state='draft'""",
                 (client_id,))
    except Exception:                    # noqa: BLE001
        return ""
    if n >= MAX_DRAFT_REQUESTS:
        return (f"Too many unverified projects ({MAX_DRAFT_REQUESTS}) — "
                "verify or remove one before adding another.")
    return ""


# --------------------------------------------------------------------------
# credit enforcement (scheduler side)
# --------------------------------------------------------------------------
def credit_cap(conn, project_id: int):
    """Credit ceiling for a project, or ``None`` when uncapped.

    Only client-owned projects are capped. ``quota_type='internal'`` marks
    our own sites (operator delivery, demos) — same escape hatch as having
    no portal request at all, so internal work is never throttled by a
    customer-facing credit count.
    """
    try:
        row = conn.execute(
            """SELECT quota_type, quota_total FROM project_request
               WHERE project_id=? AND verify_state='active'
               ORDER BY id DESC LIMIT 1""", (project_id,)).fetchone()
    except Exception:                    # noqa: BLE001
        return None
    if row is None:
        return None
    if (row[0] or "").strip().lower() == "internal":
        return None
    return max(0, int(row[1] or 0))


def _state_count(conn, project_id: int, states: tuple) -> int:
    marks = ",".join("?" * len(states))
    return _num(conn, f"SELECT COUNT(*) FROM plan WHERE project_id=? "
                      f"AND state IN ({marks})", (project_id,) + states)


def live_count(conn, project_id: int) -> int:
    """Delivered links = the thing the client actually pays for."""
    return _state_count(conn, project_id, ("live",))


def allowance(conn, project_id: int, *, include_reserved: bool = False):
    """Remaining credits, optionally netting out already-scheduled work.

    ``include_reserved=False`` -> headroom for *scheduling* new plans
    (live + everything already in flight is subtracted).
    ``include_reserved=True``  -> headroom for *executing* today's queue
    (queued rows are about to run, so only live + submitted/sent count).
    """
    cap = credit_cap(conn, project_id)
    if cap is None:
        return None
    spent = live_count(conn, project_id)
    reserved = (("queued", "submitted", "sent", "human") if not include_reserved
                else ("submitted", "sent", "human"))
    spent += _state_count(conn, project_id, reserved)
    return max(0, cap - spent)


def sync_quota_used(conn, project_id: int) -> None:
    """Mirror delivered links into project_request.quota_used for display."""
    try:
        conn.execute(
            """UPDATE project_request SET quota_used=
                 (SELECT COUNT(*) FROM plan WHERE project_id=?
                    AND state='live')
               WHERE project_id=?""", (project_id, project_id))
        conn.commit()
    except Exception:                    # noqa: BLE001
        pass
