"""osched CLI — import / probe / project / match / queue / stats / scan.

Subcommands:
  import   Run all configured import sources into the pool
  probe    HTTP-probe unprobed pool entries (DESIGN.md §4/§4c)
  project  Register a project: DB row + YAML template under data_root/projects/
  match    Score pool x project -> pending-confirmation queue (§5)
  queue    List pending matches for a project (the human gate)
  confirm  Confirm pending matches by id (or --top N by score)
  reject   Reject pending matches by id
  schedule Fill future dates from confirmed matches (§5/§7)
  plan     Show queued plans due for execution
  execute  Run due plan rows — form POST / CUA routing / SMTP (§7/§7.1)
  stats    Pool summary
  scan     Private-information scan over tracked files (pre-commit gate)

Usage:
  python3 bin/osched.py import
  python3 bin/osched.py probe [--limit 20] [--domain example.com]
  python3 bin/osched.py project add <domain> --tags gaming,english
  python3 bin/osched.py match <domain>
  python3 bin/osched.py queue <domain> [--limit 20]
  python3 bin/osched.py confirm <domain> --ids 12,13  |  --top 5
  python3 bin/osched.py reject <domain> --ids 14
  python3 bin/osched.py stats
中文：project=注册项目（DB行+YAML模板）；match=打分生成待确认队列；
queue/confirm/reject=人工闸；只有 confirmed 状态才会进入后续排期。
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "src")

from osched.adapters import build_adapters
from osched.config import load_config, data_root
from osched.db import connect
from osched.pool import import_source
from osched import probe as probe_mod
from osched import matching
from osched import classify as classify_mod
from osched import scheduler as sched_mod
from osched import executor as exec_mod

PROJECT_YAML_TEMPLATE = """\
# Project config for {domain} — lives under data_root/projects/ (gitignored).
domain: {domain}
tags: {tags}
daily_quota: 10
channels: [form, email]
# Level-1/2 landing URLs, manual input only; newest entry wins (freshness).
pages: []
templates: [dir-submit, outreach-mail]
scoring:
  min_score: 0.55
  weights: {{category: 0.35, geo: 0.15, wave: 0.20, dr: 0.15, via_competitors: 0.15}}
exclusions: []
sender: {{}}        # optional per-project sender identity
report: {{}}        # optional per-project client-report recipient
"""


def _projects_dir(cfg: dict):
    d = data_root(cfg) / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_project(cfg: dict, conn, domain: str) -> dict:
    project = matching.load_project(_projects_dir(cfg), domain)
    row = conn.execute("SELECT id FROM project WHERE domain=?",
                       (domain,)).fetchone()
    if row is None:
        raise matching.ProjectError(
            f"project '{domain}' not registered — run: osched project add {domain}")
    project["_id"] = row["id"]
    return project


def cmd_import(cfg: dict) -> int:
    adapters = build_adapters(cfg["sources"])
    root = data_root(cfg)
    conn = connect(root)
    total_inserted = 0
    for adapter in adapters:
        stats = import_source(conn, adapter)
        total_inserted += stats["inserted"]
        compact = {k: v for k, v in stats.items() if k != "issues"}
        print(json.dumps(compact, ensure_ascii=False))
        for issue in stats["issues"]:
            print(f"  [invalid] {issue}", file=sys.stderr)
    print(f"pool: {total_inserted} new rows total")
    return 0


def cmd_probe(cfg: dict, limit: int, domain: str | None) -> int:
    root = data_root(cfg)
    conn = connect(root)
    pcfg = cfg.get("probe", {})
    timeout = float(pcfg.get("timeout_seconds", 12.0))
    max_candidates = int(pcfg.get("candidates_per_site", 3))

    sql = """
        SELECT id, url, domain FROM pool
        WHERE probe_state='unprobed' AND status='active'
    """
    params: list = []
    if domain:
        sql += " AND domain=?"
        params.append(domain)
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("probe: nothing to do (no unprobed active entries)")
        return 0

    summary = {"probed": 0, "unreachable": 0, "with_form": 0,
               "cache_inserted": 0, "cache_changed": 0}
    for row in rows:
        url, dom = row["url"], row["domain"]
        res = probe_mod.probe_site(
            url, timeout=timeout, max_candidates=max_candidates)
        action = probe_mod.record_result(conn, row["id"], url, dom, res)
        summary["probed" if res.state == "probed" else "unreachable"] += 1
        summary["with_form"] += int(res.has_submit_form)
        if action == "inserted":
            summary["cache_inserted"] += 1
        elif action == "changed":
            summary["cache_changed"] += 1
        flag = "FORM" if res.has_submit_form else "    "
        print(f"  [{res.state:<11}] {flag} http={res.http_status:<4} "
              f"captcha={res.captcha_type:<12} cache={action:<9} {dom}")
        if res.entry_url:
            print(f"      entry: {res.entry_url}")

    print("probe summary: " + json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_project_add(cfg: dict, domain: str, tags: str) -> int:
    conn = connect(data_root(cfg))
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    conn.execute(
        "INSERT INTO project(domain, tags_json) VALUES(?,?) "
        "ON CONFLICT(domain) DO UPDATE SET tags_json=excluded.tags_json",
        (domain, json.dumps(tag_list, ensure_ascii=False)),
    )
    conn.commit()
    path = _projects_dir(cfg) / f"{domain}.yaml"
    if not path.exists():
        path.write_text(PROJECT_YAML_TEMPLATE.format(
            domain=domain, tags=json.dumps(tag_list)), encoding="utf-8")
    print(f"project ready: {domain} (tags={tag_list})")
    print(f"  edit config: {path}")
    return 0


def cmd_match(cfg: dict, domain: str) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    stats = matching.run_match(conn, project)
    print(f"match[{domain}]: " + json.dumps(stats, ensure_ascii=False))
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM match WHERE project_id=? AND state='pending'",
        (project["_id"],)).fetchone()["n"]
    print(f"  pending queue: {pending}")
    return 0


def cmd_queue(cfg: dict, domain: str, limit: int) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    rows = conn.execute(
        """
        SELECT m.id, m.score, m.score_json, p.url, p.domain, p.captcha_type,
               p.has_submit_form
        FROM match m JOIN pool p ON p.id = m.pool_id
        WHERE m.project_id=? AND m.state='pending'
        ORDER BY m.score DESC LIMIT ?
        """,
        (project["_id"], limit),
    ).fetchall()
    print(f"queue[{domain}] (pending, score desc):")
    for r in rows:
        form = "FORM" if r["has_submit_form"] else "    "
        print(f"  id={r['id']:<6} score={r['score']:<7} {form} "
              f"captcha={r['captcha_type']:<12} {r['domain']}")
    if not rows:
        print("  (empty — run 'match' or lower min_score)")
    return 0


def _ids_arg(ids: str) -> list[int]:
    return [int(x) for x in ids.split(",") if x.strip()]


def cmd_confirm(cfg: dict, domain: str, ids: list[int], top: int) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    if top:
        rows = conn.execute(
            "SELECT id FROM match WHERE project_id=? AND state='pending' "
            "ORDER BY score DESC LIMIT ?", (project["_id"], top)).fetchall()
        ids = [r["id"] for r in rows]
    if not ids:
        print("confirm: nothing selected (use --ids or --top)")
        return 1
    cur = conn.execute(
        "UPDATE match SET state='confirmed', confirmed_at=datetime('now') "
        "WHERE project_id=? AND state='pending' AND id IN "
        f"({','.join('?' * len(ids))})",
        [project["_id"], *ids],
    )
    conn.commit()
    conn.execute(
        "INSERT INTO run_log(action, target, result, note) "
        "VALUES('confirm', ?, ?, ?)",
        (domain, f"n={cur.rowcount}", json.dumps(ids)),
    )
    conn.commit()
    print(f"confirmed {cur.rowcount} match(es) for {domain}")
    return 0


def cmd_reject(cfg: dict, domain: str, ids: list[int]) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    cur = conn.execute(
        "UPDATE match SET state='rejected' "
        "WHERE project_id=? AND state='pending' AND id IN "
        f"({','.join('?' * len(ids))})",
        [project["_id"], *ids],
    )
    conn.commit()
    print(f"rejected {cur.rowcount} match(es) for {domain}")
    return 0


def cmd_classify(cfg: dict, limit: int) -> int:
    conn = connect(data_root(cfg))
    stats = classify_mod.run_classify(conn, limit=limit)
    print("classify: " + json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_dr(cfg: dict, limit: int) -> int:
    conn = connect(data_root(cfg))
    stats = classify_mod.refresh_dr(conn, cfg, limit=limit)
    print("dr: " + json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_schedule(cfg: dict, domain: str, days: int) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    stats = sched_mod.generate_plan(conn, project, days=days)
    print(f"schedule[{domain}]: " + json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_plan(cfg: dict, domain: str) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    rows = sched_mod.due_today(conn, project["_id"])
    print(f"plan[{domain}] (queued, due): {len(rows)}")
    for r in rows:
        note = json.loads(r["result_note"] or "{}")
        print(f"  plan_id={r['id']:<5} {r['scheduled_date']} "
              f"{r['channel']:<6} {r['pool_domain']:<28} "
              f"landing={note.get('landing', '')}")
    return 0


def cmd_execute(cfg: dict, domain: str, run_date: str | None,
                dry_run: bool, limit: int) -> int:
    conn = connect(data_root(cfg))
    project = _get_project(cfg, conn, domain)
    stats = exec_mod.run_execution(
        conn, cfg, project, run_date=run_date,
        dry_run=dry_run, limit=limit)
    tag = " [DRY-RUN]" if dry_run else ""
    print(f"execute[{domain}]{tag}: " + json.dumps(stats, ensure_ascii=False))
    for r in stats.get("rows", []):
        print(f"  plan_id={r['plan_id']:<5} {r['channel']:<6} "
              f"{r['state']:<9} {r['domain']:<28} {r['note']}")
    return 0


def cmd_stats(cfg: dict) -> int:
    conn = connect(data_root(cfg))
    rows = conn.execute(
        """
        SELECT source_kind, status, probe_state, COUNT(*) AS n
        FROM pool GROUP BY source_kind, status, probe_state
        ORDER BY n DESC
        """
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM pool").fetchone()["n"]
    domains = conn.execute(
        "SELECT COUNT(DISTINCT domain) AS n FROM pool"
    ).fetchone()["n"]
    print(f"pool total: {total} rows, {domains} domains")
    for r in rows:
        print(f"  {r['source_kind']:<28} status={r['status']:<10} "
              f"probe={r['probe_state']:<10} n={r['n']}")
    cache = conn.execute(
        "SELECT state, COUNT(*) AS n FROM probe_cache GROUP BY state"
    ).fetchall()
    if cache:
        print("probe_cache:")
        for r in cache:
            print(f"  state={r['state']:<10} n={r['n']}")
    matches = conn.execute(
        """SELECT pr.domain, m.state, COUNT(*) AS n
           FROM match m JOIN project pr ON pr.id=m.project_id
           GROUP BY pr.domain, m.state""").fetchall()
    if matches:
        print("matches:")
        for r in matches:
            print(f"  {r['domain']:<30} {r['state']:<10} n={r['n']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osched")
    ap.add_argument("--config", default=None, help="path to global YAML")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="run import sources into the pool")
    p_probe = sub.add_parser("probe", help="probe unprobed pool entries")
    p_probe.add_argument("--limit", type=int, default=None)
    p_probe.add_argument("--domain", default=None)
    p_proj = sub.add_parser("project", help="register a project")
    p_proj.add_argument("action", choices=["add"])
    p_proj.add_argument("domain")
    p_proj.add_argument("--tags", default="")
    p_match = sub.add_parser("match", help="score pool x project -> queue")
    p_match.add_argument("domain")
    p_queue = sub.add_parser("queue", help="list pending matches")
    p_queue.add_argument("domain")
    p_queue.add_argument("--limit", type=int, default=20)
    p_conf = sub.add_parser("confirm", help="confirm pending matches")
    p_conf.add_argument("domain")
    p_conf.add_argument("--ids", default="")
    p_conf.add_argument("--top", type=int, default=0)
    p_rej = sub.add_parser("reject", help="reject pending matches")
    p_rej.add_argument("domain")
    p_rej.add_argument("--ids", default="")
    p_cls = sub.add_parser("classify", help="auto-classify probed rows (§4)")
    p_cls.add_argument("--limit", type=int, default=0)
    p_dr = sub.add_parser("dr", help="DR enrichment via Open PageRank")
    p_dr.add_argument("--limit", type=int, default=100)
    p_sch = sub.add_parser("schedule", help="fill future dates from confirmed matches")
    p_sch.add_argument("domain")
    p_sch.add_argument("--days", type=int, default=7)
    p_plan = sub.add_parser("plan", help="show queued plans due for execution")
    p_plan.add_argument("domain")
    p_exec = sub.add_parser("execute", help="run due plan rows (§7)")
    p_exec.add_argument("domain")
    p_exec.add_argument("--date", default=None,
                        help="override today (ISO date)")
    p_exec.add_argument("--dry-run", action="store_true")
    p_exec.add_argument("--limit", type=int, default=0)
    sub.add_parser("stats", help="pool summary")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.cmd == "import":
        return cmd_import(cfg)
    if args.cmd == "probe":
        limit = args.limit or int(cfg.get("probe", {}).get("per_run_limit", 20))
        return cmd_probe(cfg, limit, args.domain)
    if args.cmd == "project":
        return cmd_project_add(cfg, args.domain, args.tags)
    if args.cmd == "match":
        return cmd_match(cfg, args.domain)
    if args.cmd == "queue":
        return cmd_queue(cfg, args.domain, args.limit)
    if args.cmd == "confirm":
        return cmd_confirm(cfg, args.domain, _ids_arg(args.ids), args.top)
    if args.cmd == "reject":
        return cmd_reject(cfg, args.domain, _ids_arg(args.ids))
    if args.cmd == "classify":
        return cmd_classify(cfg, args.limit)
    if args.cmd == "dr":
        return cmd_dr(cfg, args.limit)
    if args.cmd == "schedule":
        return cmd_schedule(cfg, args.domain, args.days)
    if args.cmd == "plan":
        return cmd_plan(cfg, args.domain)
    if args.cmd == "execute":
        return cmd_execute(cfg, args.domain, args.date, args.dry_run,
                           args.limit)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

