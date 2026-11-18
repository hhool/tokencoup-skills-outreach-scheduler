"""osched CLI — import / probe / stats / scan.

Subcommands:
  import   Run all configured import sources into the pool
  probe    HTTP-probe unprobed pool entries (DESIGN.md §4/§4c):
           discover submit endpoints -> pool columns + probe_cache
  stats    Pool summary: totals by source/status/probe_state
  scan     Private-information scan over tracked files (pre-commit gate)

Usage:
  python3 bin/osched.py import [--config config/global.yaml]
  python3 bin/osched.py probe [--limit 20] [--domain example.com]
  python3 bin/osched.py stats
  python3 tools/private_scan.py            # same as scan
中文：import=导入全部配置源入池；probe=探测未探测条目（发现提交入口并缓存复用）；
stats=池统计；scan=私有信息扫描（提交前必跑）。
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
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osched")
    ap.add_argument("--config", default=None, help="path to global YAML")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="run import sources into the pool")
    p_probe = sub.add_parser("probe", help="probe unprobed pool entries")
    p_probe.add_argument("--limit", type=int, default=None,
                         help="max entries this run (default from config)")
    p_probe.add_argument("--domain", default=None,
                         help="restrict to one domain")
    sub.add_parser("stats", help="pool summary")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.cmd == "import":
        return cmd_import(cfg)
    if args.cmd == "probe":
        limit = args.limit or int(cfg.get("probe", {}).get("per_run_limit", 20))
        return cmd_probe(cfg, limit, args.domain)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
