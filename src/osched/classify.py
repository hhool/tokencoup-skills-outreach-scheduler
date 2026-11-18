"""Auto-classification pipeline (DESIGN.md §4).

Runs after probe on probed pool rows, all automatic:
  1. site_type     — URL/entry signatures (directory / bookmark / blog / …)
  2. submit_type   — probe evidence (form found -> auto; entry only ->
                     register; email only -> pitch; nothing -> unknown)
  3. wave          — derived from primary submit_type (wave1 one-shot,
                     wave2 multi-step, wave3 unknown); explicit value wins
  4. category      — keyword rule table (gaming / parenting / tools / …)
  5. DR enrichment — Open PageRank batch query, cached in pool.dr
                     (never re-queried); fail-open without a key

中文：分类流水线全部自动；DR 用 Open PageRank 免费源，无 key 时跳过不阻塞。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .config import require_env_key

# --- keyword rule tables (extensible via config later; order matters) ---

CATEGORY_RULES: list[tuple[str, str]] = [
    ("gaming", r"game|gaming|arcade|juego|spel|oyo|pokigrames"),
    ("parenting", r"toddler|kids?|child|baby|parent|family|mama|mom"),
    ("tools", r"tool|software|sound|audio|converter|generator|"
              r"calculator|seo|download|font|pdf"),
    ("education", r"edu|school|learn|course|study"),
    ("news", r"news|magazine|daily|press"),
]

SITE_TYPE_RULES: list[tuple[str, str]] = [
    ("directory", r"directory|submit|add[-_]?link|add[-_]?url|list"),
    ("bookmark", r"bookmark|favorit"),
    ("launchpad", r"launch|startup|alternativ"),
    ("blog", r"blog|wordpress|blogger"),
    ("forum", r"forum|community|board"),
    ("web-portal", r"portal|startpagina|start"),
]

# wave derivation per §4 / project convention:
# wave1 = one-shot submissions, wave2 = multi-step interactions, wave3 unknown
SUBMIT_WAVE = {
    "auto": 1, "register": 1, "free": 1, "listing": 1,
    "posting": 1, "answer": 1, "publish": 1, "pr": 1,
    "guest_post": 2, "pitch": 2, "apply": 2, "exchange": 2, "paid": 2,
    "unknown": 3,
}


def _match_rules(rules: list[tuple[str, str]], text: str) -> str:
    low = (text or "").lower()
    for label, pattern in rules:
        if pattern and __import__("re").search(pattern, low):
            return label
    return ""


def classify_row(row) -> dict:
    """Derive site_type / submit_type / wave / category for one pool row."""
    haystack = " ".join(filter(None, [
        row["url"], row["domain"], row["entry_url"] or ""]))

    site_type = (_match_rules(SITE_TYPE_RULES, haystack)
                 or _match_rules(SITE_TYPE_RULES, row["url"] or "")
                 or "website")

    if row["has_submit_form"]:
        submit_type = "auto"
    elif row["entry_url"]:
        submit_type = "register"
    elif row["contact_email"]:
        submit_type = "pitch"
    else:
        submit_type = "unknown"
    wave = SUBMIT_WAVE.get(submit_type, 3)

    # category: prefer explicit hints from the source, then URL keywords
    category = ""
    try:
        hints = json.loads(row["hints_json"] or "{}")
        category = (hints.get("category") or "").lower()
    except json.JSONDecodeError:
        pass
    if not category:
        category = _match_rules(CATEGORY_RULES, haystack) or "general"

    return {"site_type": site_type, "submit_type": submit_type,
            "wave": wave, "category": category}


def run_classify(conn, *, limit: int = 0) -> dict:
    """Classify probed rows that lack a wave yet. Returns summary."""
    sql = ("SELECT * FROM pool WHERE probe_state='probed' AND wave IS NULL")
    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]
    counts: dict[str, int] = {}
    for row in rows:
        c = classify_row(row)
        conn.execute(
            """UPDATE pool SET site_type=?, submit_type=?, wave=?,
               category=? WHERE id=?""",
            (c["site_type"], c["submit_type"], c["wave"], c["category"],
             row["id"]),
        )
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('classify', 'pool', 'ok', ?)",
        (json.dumps({"classified": len(rows), "by_category": counts}),
        ),
    )
    conn.commit()
    return {"classified": len(rows), "by_category": counts}


# --------------------------------------------------------------------------
# DR enrichment — Open PageRank (free tier), batch + cached in pool.dr
# --------------------------------------------------------------------------

def refresh_dr(conn, cfg: dict, *, limit: int = 100) -> dict:
    """Batch-fill pool.dr for probed rows. Fail-open without an API key."""
    dcfg = cfg.get("dr", {})
    base_url = dcfg.get("base_url",
                        "https://openpagerank.keywordseverywhere.com/api/v1.0")
    env_file = dcfg.get("env_file", "config/secrets/opr.env")
    key_name = dcfg.get("api_key_env", "OPENPAGERANK_API_KEY")
    batch_size = int(dcfg.get("batch_size", 100))

    try:
        key = require_env_key(env_file, key_name)
    except Exception as e:  # fail-open: DR is a scoring prior, not a blocker
        return {"skipped": f"no DR key ({e})"}

    rows = conn.execute(
        """SELECT DISTINCT domain FROM pool
           WHERE probe_state='probed' AND dr IS NULL LIMIT ?""",
        (limit,),
    ).fetchall()
    domains = [r["domain"] for r in rows]
    if not domains:
        return {"updated": 0}

    updated, failed = 0, 0
    for i in range(0, len(domains), batch_size):
        chunk = domains[i:i + batch_size]
        qs = "&".join(f"domains[{j}]={urllib.parse.quote(d)}"
                      for j, d in enumerate(chunk))
        req = urllib.request.Request(
            f"{base_url}/getPageRank?{qs}",
            headers={"API-OPR": key,   # moved endpoint expects this header
                     "User-Agent": "outreach-scheduler/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"error": str(e), "updated": updated, "failed": failed}
        by_domain = {
            item.get("domain", "").lower(): item
            for item in data.get("response", [])
        }
        for dom in chunk:
            item = by_domain.get(dom.lower())
            if not item:
                failed += 1
                continue
            rank = item.get("page_rank")
            if isinstance(rank, (int, float)):
                conn.execute(
                    "UPDATE pool SET dr=? WHERE domain=? AND dr IS NULL",
                    (float(rank), dom))
                updated += 1
            else:
                failed += 1
    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('dr', 'pool', 'ok', ?)",
        (json.dumps({"updated": updated, "failed": failed}),
        ),
    )
    conn.commit()
    return {"updated": updated, "failed": failed}
