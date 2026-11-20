"""Matching & scoring (DESIGN.md §5) + project config (client-intake §1).

A project takes one domain plus a YAML under data_root/projects/. Scoring
combines: category match, language/geo match, wave priority, DR, and the
competitor-evidence prior from the pool. Exclusions and min_score gate the
pending-confirmation queue; a human gate (confirm/reject) sits between
matching and scheduling — the executor only ever consumes confirmed items.

中文：匹配打分（§5）+ 项目配置。产出进待确认队列，人工闸后才排期。
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import yaml

WAVE_PRIORITY = {1: 1.0, 2: 0.6, 3: 0.0}

DEFAULT_WEIGHTS = {
    "category": 0.35,
    "geo": 0.15,
    "wave": 0.20,
    "dr": 0.15,
    "via_competitors": 0.15,
}


class ProjectError(Exception):
    pass


def load_project(projects_dir: pathlib.Path, domain: str) -> dict[str, Any]:
    """Load and validate one project YAML from data_root/projects/."""
    path = projects_dir / f"{domain}.yaml"
    if not path.exists():
        raise ProjectError(
            f"project config not found: {path} "
            "(create it from the template in docs/client-intake.md)")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if doc.get("domain") != domain:
        raise ProjectError(f"{path}: 'domain' field must be '{domain}'")
    doc.setdefault("tags", [])
    doc.setdefault("daily_quota", 10)
    doc.setdefault("channels", ["form", "email"])
    doc.setdefault("exclusions", [])
    scoring = doc.setdefault("scoring", {})
    scoring.setdefault("min_score", 0.55)
    weights = {**DEFAULT_WEIGHTS, **scoring.get("weights", {})}
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        raise ProjectError(f"{path}: scoring weights sum to {total}, expected 1.0")
    scoring["weights"] = weights
    doc.setdefault("pages", [])   # level-1/2 landing URLs, newest last
    doc.setdefault("sender", {})
    doc.setdefault("report", {})
    return doc


def competitor_prior(evidence_json: str) -> float:
    """Evidence prior 0..1 from the WDC-style harvest evidence.

    via_competitors count is the strong signal (linked to 3+ competitors
    = 1.0), inbound backlinks add up to +0.3 on a log-ish scale.
    """
    try:
        ev = json.loads(evidence_json or "{}")
    except json.JSONDecodeError:
        return 0.0
    via = len(ev.get("via_competitors") or [])
    inbound = ev.get("inbound") or 0
    score = min(1.0, via / 3.0)
    if inbound:
        score = min(1.0, score + min(inbound, 10) / 10 * 0.3)
    return score


def discovery_prior(hints_json: str) -> float:
    """Discovery-layer prior 0..0.75 from WDC graph hints.

    Only rows stamped by the discovery adapter (``hints.layer ==
    'discovery'``) are considered. Combines directory probability, LLR,
    outlink similarity and dual-signal agreement. Capped well below 1.0 so
    graph evidence alone never triggers the strong-evidence freshness boost,
    and never implies competitor backlinks or submittability.
    中文：发现层图谱先验有上限，不能单独触发强证据加成，也不代表可投递。
    """
    try:
        h = json.loads(hints_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0.0
    if not isinstance(h, dict) or h.get("layer") != "discovery":
        return 0.0
    d = h.get("discovery")
    if not isinstance(d, dict):
        return 0.0

    def _nonneg(value) -> float:
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    prob = min(1.0, _nonneg(d.get("dir_prob")))
    llr = _nonneg(d.get("dir_llr"))
    llr_score = llr / (llr + 10.0) if llr else 0.0
    sim = min(1.0, _nonneg(d.get("outlink_similarity")) * 8.0)
    agree = 1.0 if d.get("dual_signal_agree") else 0.0
    raw = 0.4 * prob + 0.25 * llr_score + 0.2 * sim + 0.1 * agree
    return round(min(0.75, raw), 4)


def curated_fit(hints_json: str) -> int:
    """Curated fit 0..100 from the workbench bridge, 0 when absent."""
    try:
        h = json.loads(hints_json or "{}")
    except json.JSONDecodeError:
        return 0
    if not h.get("workbench_id"):
        return 0
    try:
        return max(0, min(100, int(h.get("fit_score") or 0)))
    except (TypeError, ValueError):
        return 0


def score_row(row, project: dict[str, Any]) -> tuple[float, dict]:
    """Score one pool row against the project. Returns (score, detail)."""
    w = project["scoring"]["weights"]
    tags = [t.lower() for t in project["tags"]]

    category = 1.0 if (row["category"] or "").lower() in tags else 0.0
    geo = 1.0 if (row["geo"] or "").lower() in tags else 0.0
    wave = WAVE_PRIORITY.get(row["wave"], 0.0)
    dr = (row["dr"] or 0.0) / 10.0
    prior = competitor_prior(row["evidence_json"])
    # A curated ledger row carries a human fit score, and a discovery row
    # carries WDC graph signals; both answer the same question ("why is this
    # a candidate"). max() — never sum — prevents double counting when a
    # domain happens to appear in more than one source.
    fit = curated_fit(row["hints_json"])
    disc = discovery_prior(row["hints_json"])
    prior = max(prior, fit / 100.0, disc)

    score = (w["category"] * category + w["geo"] * geo + w["wave"] * wave
             + w["dr"] * dr + w["via_competitors"] * prior)

    # Freshness boost: a row whose domain matches a page asset (or whose
    # evidence is very strong) gets a small recency bonus (§5, ratified).
    boost = 0.0
    if prior >= 1.0:
        boost = 0.05

    detail = {
        "category": round(category, 2), "geo": round(geo, 2),
        "wave": row["wave"], "dr": round(dr, 2),
        "prior": round(prior, 2), "boost": boost,
        "discovery_prior": round(disc, 2),
    }
    return round(score + boost, 4), detail


def run_match(conn, project: dict[str, Any], *, limit: int = 0) -> dict:
    """Score all eligible pool rows, insert pending matches above min_score.

    Eligible = active, probed (or probe-capable later), not excluded,
    not already matched to this project.
    """
    min_score = float(project["scoring"]["min_score"])
    exclusions = [e.lower() for e in project["exclusions"]]

    rows = conn.execute(
        """
        SELECT * FROM pool
        WHERE status='active' AND probe_state='probed'
        """
    ).fetchall()

    inserted = skipped_excluded = below_threshold = 0
    trust_curated = bool(project["scoring"].get("curated_trust", True))
    for row in rows:
        dom = (row["domain"] or "").lower()
        if any(ex in dom for ex in exclusions):
            skipped_excluded += 1
            continue
        score, detail = score_row(row, project)
        # The min_score gate exists to keep noisy crawl rows out of the human
        # queue. A curated ledger row already passed human review, so it enters
        # the queue on its own fit — the confirm gate still guards delivery.
        curated = curated_fit(row["hints_json"]) > 0
        detail["curated"] = curated
        if score < min_score and not (curated and trust_curated):
            below_threshold += 1
            continue
        conn.execute(
            """
            INSERT INTO match(project_id, pool_id, score, score_json, state)
            SELECT ?, ?, ?, ?, 'pending'
            WHERE NOT EXISTS (
              SELECT 1 FROM match WHERE project_id=? AND pool_id=?
            )
            """,
            (project["_id"], row["id"], score,
             json.dumps(detail, ensure_ascii=False),
             project["_id"], row["id"]),
        )
        inserted += 1
        if limit and inserted >= limit:
            break
    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('match', ?, 'ok', ?)",
        (project["domain"], json.dumps(
            {"pending": inserted, "excluded": skipped_excluded,
             "below": below_threshold}, ensure_ascii=False)),
    )
    conn.commit()
    return {"pending": inserted, "excluded": skipped_excluded,
            "below_threshold": below_threshold}


def landing_url(project: dict[str, Any], category: str | None) -> str:
    """Pick the landing URL for a submission (client-intake §5a).

    Freshness boost (§5): the newest page asset wins; homepage fallback.
    """
    pages = [p for p in project.get("pages", []) if p]
    return pages[-1] if pages else f"https://{project['domain']}/"
