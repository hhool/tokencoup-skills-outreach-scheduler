"""osched CLI — import / stats (Phase 1).

Subcommands:
  import   Run all configured import sources into the pool
  stats    Pool summary: totals by source/status/probe_state
  scan     Private-information scan over tracked files (pre-commit gate)

Usage:
  python3 bin/osched.py import [--config config/global.yaml]
  python3 bin/osched.py stats
  python3 tools/private_scan.py            # same as scan
中文：import=导入全部配置源入池；stats=池统计；scan=私有信息扫描（提交前必跑）。
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
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osched")
    ap.add_argument("--config", default=None, help="path to global YAML")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="run import sources into the pool")
    sub.add_parser("stats", help="pool summary")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.cmd == "import":
        return cmd_import(cfg)
    if args.cmd == "stats":
        return cmd_stats(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
