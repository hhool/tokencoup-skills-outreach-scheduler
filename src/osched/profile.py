"""Submittability profile (business.md B4) — read-only aggregation layer.

Quota is finite (per-domain address caps); spend it where submission
actually passes. This module aggregates execution history into a
per-directory-domain profile and turns it into actionable suggestions:

  - attempts / submitted / live counts per pool domain (from plan + pool)
  - failure-reason breakdown parsed from result_note (bot-blocked,
    captcha-escalated, no-form, 4xx, unreachable)
  - a recommendation: preferred | keep | bot-blocked(keep) | exclude-candidate

Template attribution: wave2 send rows carry `template` in result_note once
they accumulate; the aggregator reports per-template approval rates when
present (gracefully absent before the first wave2 runs).

中文：可投递性画像（B4）——把执行失败聚合为每域名画像与处置建议
（preferred/keep/bot-blocked/exclude），wave2 模板通过率归因在数据积累后
自动出现。纯只读，不改执行链。
"""
from __future__ import annotations

import json
from collections import defaultdict

# failure-reason keywords, first match wins
FAILURE_KEYS = [
    ("bot-blocked", ("bot-blocked", "bot_blocked")),
    ("captcha-escalated", ("captcha", "escalated")),
    ("no-form", ("no fillable form", "no submit control", "no usable",
                 "no available submit")),
    ("4xx", ("4xx", "http 4", " status=4")),
    ("unreachable", ("unreachable", "network", "timed out", "timeout",
                     "connection")),
]


def _safe_note(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def classify_failure(note_text: str) -> str:
    low = note_text.lower()
    for label, keys in FAILURE_KEYS:
        if any(k in low for k in keys):
            return label
    return "other"


def _domain_rows(conn, pid: int | None) -> list:
    sql = """
        SELECT p.domain AS pool_domain, p.captcha_type, p.probe_state,
               pl.state AS plan_state, pl.result_note, pl.channel
        FROM plan pl
        JOIN match m ON m.id = pl.match_id
        JOIN pool p ON p.id = m.pool_id
    """
    args: list = []
    if pid:
        sql += " WHERE pl.project_id=?"
        args.append(pid)
    return conn.execute(sql, args).fetchall()


def build_profiles(conn, pid: int | None = None) -> dict[str, dict]:
    """Aggregate plan history into per-domain profiles."""
    prof: dict[str, dict] = defaultdict(lambda: {
        "attempts": 0, "submitted": 0, "live": 0, "sent": 0,
        "failures": defaultdict(int), "captcha_type": "", "probe_state": "",
        "templates": defaultdict(lambda: [0, 0]),  # name -> [sent, ok]
    })
    for r in _domain_rows(conn, pid):
        d = prof[r["pool_domain"]]
        note = _safe_note(r["result_note"])
        note_text = json.dumps(note, ensure_ascii=False).lower()
        if r["probe_state"]:
            d["probe_state"] = r["probe_state"]
        if r["captcha_type"]:
            d["captcha_type"] = r["captcha_type"]
        # queued rows are not attempts
        if r["plan_state"] == "queued":
            continue
        d["attempts"] += 1
        if r["plan_state"] == "live":
            d["live"] += 1
            d["submitted"] += 1
        elif r["plan_state"] in ("submitted", "sent"):
            d["submitted" if r["plan_state"] == "submitted" else "sent"] += 1
        elif r["plan_state"] in ("human", "failed"):
            reason = classify_failure(note_text)
            d["failures"][reason] += 1
        # template attribution: wave2 rows record the template used
        tpl = note.get("template")
        if tpl and r["channel"] == "email":
            t = d["templates"][tpl]
            t[0] += 1
            t[1] += 1 if r["plan_state"] in ("sent", "live") else 0
    return dict(prof)


def recommend(p: dict) -> str:
    """Actionable label for one domain profile."""
    if p["live"] > 0:
        return "preferred"
    fails = sum(p["failures"].values())
    attempts = p["attempts"] or 1
    if p["probe_state"] in ("dead",) or attempts >= 2 and \
            p["failures"].get("no-form", 0) == fails and fails >= 2:
        return "exclude-candidate"
    if p["failures"].get("bot-blocked", 0) / attempts >= 0.5:
        return "bot-blocked (keep; human/CUA only)"
    if p["submitted"] + p["sent"] > 0:
        return "keep"
    return "watch"


def render(conn, pid: int | None = None, limit: int = 0) -> str:
    profs = build_profiles(conn, pid)
    rows = sorted(profs.items(),
                  key=lambda kv: (kv[1]["live"], kv[1]["submitted"],
                                  -sum(kv[1]["failures"].values())),
                  reverse=True)
    if limit:
        rows = rows[:limit]
    lines = [f"{'domain':<30} {'att':>4} {'sub':>4} {'sent':>5} {'live':>5} "
             f"{'fails':>6}  recommendation / failure mix"]
    for dom, p in rows:
        fails = sum(p["failures"].values())
        mix = ", ".join(f"{k}={v}" for k, v in
                        sorted(p["failures"].items(),
                               key=lambda kv: -kv[1])) or "-"
        lines.append(f"{dom:<30.30} {p['attempts']:>4} {p['submitted']:>4} "
                     f"{p['sent']:>5} {p['live']:>5} {fails:>6}  "
                     f"{recommend(p)} ({mix})")
        if p["templates"]:
            for name, (sent, ok) in p["templates"].items():
                lines.append(f"    template {name}: {ok}/{sent} approved")
    if not lines[1:]:
        lines.append("  (no execution history yet — run execute/cua first)")
    return "\n".join(lines)


def exclusion_candidates(conn, pid: int | None = None) -> list[str]:
    """Domains worth excluding: >=2 attempts, zero success."""
    return [dom for dom, p in build_profiles(conn, pid).items()
            if recommend(p) == "exclude-candidate"]
