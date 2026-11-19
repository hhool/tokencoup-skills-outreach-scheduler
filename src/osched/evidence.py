"""Public, tamper-evident link receipts (business.md B1 evidence layer).

The client is told a link is live; the receipt is how they can *check* it
without logging in and forward it to whoever approves the invoice. Each
receipt lives at /evidence/<plan_id>.<hmac> — an unguessable URL bound to
one plan, no session, no account data.

The signing secret comes from EVIDENCE_SECRET when set; otherwise a
persistent random secret is generated once under data_root (mode 0600) so
URLs survive restarts without any manual setup.

中文：证据页 = 发票凭证。客户拿到一个免登录的固定 URL，能看到 listing、
锚文本、rel 属性、历次复检时序和页面指纹；不 live 不计费因此可核对。
"""
from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import os
import pathlib
import secrets

from . import config

_SECRET_FILE = "evidence_secret"
_MAC_LEN = 24


def _secret(cfg: dict) -> str:
    env = (os.environ.get("EVIDENCE_SECRET") or "").strip()
    if env:
        return env
    path = config.data_root(cfg) / _SECRET_FILE
    if path.exists():
        return path.read_text().strip()
    tok = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def _mac(cfg: dict, plan_id: int) -> str:
    return hmac.new(_secret(cfg).encode(), f"plan:{plan_id}".encode(),
                    hashlib.sha256).hexdigest()[:_MAC_LEN]


def token_for(cfg: dict, plan_id: int) -> str:
    return f"{plan_id}.{_mac(cfg, plan_id)}"


def parse_token(cfg: dict, token: str) -> int | None:
    """Plan id when the mac verifies, else None (constant-time compare)."""
    try:
        plan_s, mac = token.strip().split(".", 1)
        plan_id = int(plan_s)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, _mac(cfg, plan_id)):
        return None
    return plan_id


def url_for(cfg: dict, plan_id: int) -> str:
    base = (os.environ.get("PORTAL_BASE_URL")
            or (cfg.get("portal", {}) or {}).get("base_url") or "").rstrip("/")
    path = f"/evidence/{token_for(cfg, plan_id)}"
    return base + path if base else path


def receipt(conn, cfg: dict, plan_id: int) -> dict | None:
    """All evidence fields for one plan (None if the plan does not exist)."""
    row = conn.execute(
        """SELECT pl.id, pl.state, pl.executed_at, pl.result_note,
                  p.domain AS placement, p.entry_url, p.url,
                  pr.domain AS project
           FROM plan pl
           JOIN match m ON m.id = pl.match_id
           JOIN pool p ON p.id = m.pool_id
           JOIN project pr ON pr.id = pl.project_id
           WHERE pl.id=?""", (plan_id,)).fetchone()
    if not row:
        return None
    landing = ""
    try:
        import json
        landing = (json.loads(row["result_note"] or "{}")
                   .get("landing") or "")
    except Exception:  # noqa: BLE001 — receipt never crashes on a bad note
        pass
    history = conn.execute(
        """SELECT checked_at, result, http_status, rel, anchor_text,
                  anchor_href, page_sha256
           FROM link_check WHERE plan_id=?
           ORDER BY checked_at DESC, id DESC LIMIT 12""",
        (plan_id,)).fetchall()
    first_live = conn.execute(
        """SELECT MIN(checked_at) AS t FROM link_check
           WHERE plan_id=? AND result='live'""", (plan_id,)).fetchone()["t"]
    checks = conn.execute(
        "SELECT COUNT(*) AS n FROM link_check WHERE plan_id=?",
        (plan_id,)).fetchone()["n"]
    latest = history[0] if history else None
    return {
        "plan_id": plan_id,
        "project": row["project"],
        "placement": row["placement"],
        "entry_url": row["entry_url"] or row["url"],
        "landing": landing,
        "state": row["state"],
        "executed_at": row["executed_at"],
        "first_live": first_live,
        "checks": checks,
        "history": [dict(h) for h in history],
        "rel": latest["rel"] if latest else "",
        "anchor_text": latest["anchor_text"] if latest else "",
        "page_sha256": latest["page_sha256"] if latest else "",
        "url": url_for(cfg, plan_id),
    }


def render_page(conn, cfg: dict, plan_id: int) -> str | None:
    """HTML body for the public receipt page (None -> caller shows 404)."""
    r = receipt(conn, cfg, plan_id)
    if not r:
        return None
    e = html_mod.escape
    live = r["state"] == "live"
    verdict = ("VERIFIED LIVE" if live else
               r["state"].replace("_", " ").upper())
    rel = r["rel"] or "not recorded"
    rel_note = ("dofollow" if "nofollow" not in rel.lower() and live
                else "nofollow/sponsored" if live else "")
    rows = "".join(
        f"<tr><td>{e(h['checked_at'])}</td><td>{e(h['result'])}</td>"
        f"<td>{h['http_status'] if h['http_status'] is not None else '—'}</td>"
        f"<td>{e(h['rel'] or '—')}</td></tr>"
        for h in r["history"]) or \
        "<tr><td colspan='4' class='muted'>no checks recorded yet</td></tr>"
    sha = r["page_sha256"]
    sha_show = f"{sha[:16]}…" if sha else "—"
    landing = e(r["landing"]) if r["landing"] else "—"
    entry = e(r["entry_url"]) if r["entry_url"] else "—"
    body = f"""
    <div class='card'>
      <h2>Link receipt — plan #{r['plan_id']}</h2>
      <p>Verdict: <strong>{e(verdict)}</strong>
       {'<span class="muted">(only verified-live links are billed)</span>'
        if live else ''}</p>
      <table>
        <tr><th>field</th><th>value</th></tr>
        <tr><td>client site</td><td>{e(r['project'])}</td></tr>
        <tr><td>placement</td><td>{e(r['placement'])}</td></tr>
        <tr><td>listing page</td><td><a href='{entry}'>{entry}</a></td></tr>
        <tr><td>our link</td><td>{landing}</td></tr>
        <tr><td>anchor text</td><td>{e(r['anchor_text'] or '—')}</td></tr>
        <tr><td>rel attribute</td><td>{e(rel)}
          {f'<span class="muted">({rel_note})</span>' if rel_note else ''}</td></tr>
        <tr><td>first seen live</td><td>{e(r['first_live'] or '—')}</td></tr>
        <tr><td>page hash at check</td><td><code>{e(sha_show)}</code></td></tr>
      </table>
      <h3>Check history ({r['checks']} checks)</h3>
      <table><tr><th>checked at</th><th>result</th><th>http</th>
        <th>rel</th></tr>{rows}</table>
      <p class='muted'>Verify it yourself: open the listing page above and
      look for our link. This receipt is regenerated from the check log at
      request time — the page hash is what the checker actually saw.
      A link that never reaches live is never billed.</p>
    </div>"""
    return body
