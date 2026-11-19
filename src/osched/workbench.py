"""Workbench ledger -> pool bridge (one-way, read-only at the source end).

`~/seo/<site>/targets.jsonl` is the curation ledger kept by the SEO
backlink workbench: hand-vetted directories with a submit type, a wave,
a how-to note and a fit score. The pool in this repo is built from raw
harvested crawls (WDC), which yields *web-2.0 property roots* — sites where
you must register and publish, not directories you post a URL to. That is
why a harvested pool alone produces waves of "submit_type unknown".

This module imports the curated ledger as pool rows so the executor has
something real to submit to. Rules:

1. One-way snapshot. The workbench stays the ledger of record (status,
   link_state, evidence); we never write back. Re-import is idempotent.
2. Dedupe by pay-level domain: an existing pool row for the domain is
   upgraded in place (its precise `submit_url` becomes `entry_url`) rather
   than inserted twice — two rows for one site would double-submit.
3. `dropped` targets are never imported; `done`/`working`/`pending` are.
4. The curated `how` note is preserved in `hints_json` so the human queue
   has instructions, and `submit_type`/`wave` seed classification so the
   rows are matchable immediately (no probe round-trip needed).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

SKIP_STATUS = {"dropped", "banned", "do_not_use"}
# Submit types whose channel is email: nothing to submit via HTTP form.
EMAIL_TYPES = {"pitch", "guest_post", "apply", "exchange", "paid"}
FORM_TYPES = {"auto", "listing", "register", "free", "posting", "answer",
              "publish", "pr", "form"}


def _domain(url: str, fallback: str = "") -> str:
    host = ""
    if url:
        host = urlparse(url if "://" in url else "https://" + url).netloc
    host = (host or fallback).lower().split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def import_targets(conn: sqlite3.Connection, path: str | Path, *,
                   landings: list[str] | None = None,
                   min_fit: int = 0) -> dict:
    """Upsert one workbench ledger into the pool. Returns stats."""
    p = Path(path).expanduser()
    stats = {"path": str(p), "rows": 0, "inserted": 0, "upgraded": 0,
             "skipped_status": 0, "skipped_fit": 0, "skipped_invalid": 0,
             "skipped_downgrade": 0, "dropped": 0}
    if not p.is_file():
        raise FileNotFoundError(str(p))

    with p.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            stats["rows"] += 1
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                stats["skipped_invalid"] += 1
                continue

            status = (t.get("status") or "").lower()
            if status in SKIP_STATUS:
                # A drop in the ledger IS a decision: the pool must stop
                # delivering to that domain. Skipping the row outright left
                # the pool holding `active` rows the operator had already
                # rejected (pcastuces, smoothdirectory: contact pages labelled
                # as listings kept queueing submissions that could never land).
                dom = _domain(t.get("url") or "", t.get("domain") or "")
                if dom:
                    stats["dropped"] += conn.execute(
                        "UPDATE pool SET status='dropped', last_seen=datetime('now') "
                        "WHERE domain=? AND status!='dropped'", (dom,)).rowcount
                stats["skipped_status"] += 1
                continue
            fit = t.get("fit_score") or 0
            try:
                fit = int(fit)
            except (TypeError, ValueError):
                fit = 0
            if min_fit and fit < min_fit:
                stats["skipped_fit"] += 1
                continue

            url = (t.get("url") or t.get("check_url") or "").strip()
            domain = _domain(url, t.get("domain") or "")
            if not url or not domain:
                stats["skipped_invalid"] += 1
                continue

            submit_type = (t.get("submit_type") or "unknown").lower()
            if submit_type not in FORM_TYPES | EMAIL_TYPES | {"unknown"}:
                submit_type = "unknown"
            wave = t.get("wave")
            try:
                wave = int(wave) if wave is not None else None
            except (TypeError, ValueError):
                wave = None
            entry_url = (t.get("submit_url") or "").strip()
            # A curated `submit_url` may be relative or a bare host.
            if entry_url and "://" not in entry_url:
                entry_url = "https://" + entry_url
            hints = {"workbench_id": t.get("id"), "how": t.get("how") or "",
                     "verdict": t.get("verdict"), "fit_score": fit,
                     "authority_grade": t.get("authority_grade"),
                     "target_page": t.get("target_page") or "",
                     "landings": landings or []}

            row = conn.execute("SELECT id, entry_url, submit_type FROM pool "
                               "WHERE domain=? ORDER BY id LIMIT 1",
                               (domain,)).fetchone()
            if row:
                # Never downgrade: the ledger legitimately has `unknown` rows,
                # and losing a probed/curated type would send the plan back to
                # the wave-3 human queue for no reason.
                if submit_type == "unknown" and \
                        (row["submit_type"] or "unknown") not in ("unknown", ""):
                    stats["skipped_downgrade"] += 1
                    continue
                conn.execute(
                    """UPDATE pool SET submit_type=?, wave=COALESCE(?, wave),
                         category=COALESCE(?, category),
                         geo=COALESCE(?, geo),
                         language=COALESCE(?, language),
                         entry_url=CASE WHEN (entry_url IS NULL OR entry_url='')
                                           AND ?!='' THEN ? ELSE entry_url END,
                         has_submit_form=CASE WHEN ? THEN 1
                                              ELSE has_submit_form END,
                         captcha_type=CASE WHEN captcha_type='unknown'
                                           THEN 'none' ELSE captcha_type END,
                         probe_state='probed',
                         hints_json=?, last_seen=datetime('now')
                       WHERE id=?""",
                    (submit_type, wave, t.get("category"), t.get("geo"),
                     t.get("language"), entry_url, entry_url,
                     1 if submit_type in FORM_TYPES else 0,
                     json.dumps(hints, ensure_ascii=False), row["id"]))
                stats["upgraded"] += 1
            else:
                conn.execute(
                    """INSERT INTO pool(url, domain, source_kind, source_file,
                         source_row, hints_json, raw_json, entry_url,
                         has_submit_form, captcha_type, probe_state,
                         submit_type, wave, category, geo, language, status)
                       VALUES(?,?,'workbench',?,?,?,'{}',?,?,'none','probed',
                              ?,?,?,?,?,'active')""",
                    (url, domain, str(p), i, json.dumps(hints, ensure_ascii=False),
                     entry_url, 1 if submit_type in FORM_TYPES else 0,
                     submit_type, wave, t.get("category"), t.get("geo"),
                     t.get("language")))
                stats["inserted"] += 1

    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) VALUES"
        "('import', 'workbench', 'ok', ?)",
        (json.dumps(stats, ensure_ascii=False),))
    conn.commit()
    return stats
