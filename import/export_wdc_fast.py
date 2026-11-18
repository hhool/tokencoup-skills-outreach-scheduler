# -*- coding: utf-8 -*-
"""Fast path: export WDC competitor-link rows to workbench candidates WITHOUT
re-running HTTP validation.

Why: analyze.py validates every row with only 24 workers x 9s timeout, so a
41k-row WDC set needs ~4h — too slow. The WDC graph already carries strong
evidence (multiple competitors linking the same source); liveness is a
*separate* concern that workbench's `scan` command handles afterwards.

Field contract: same as export_to_workbench.py §3; link_state stays unknown.
Usage:
    python3 tools/export_wdc_fast.py --center balancebiketoddler.com \
      --keywords data/keywords-balancebiketoddler.com.json \
      --min-via 2 --out /tmp/bbt-fast.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from export_to_workbench import load_relevance  # noqa: E402

# 平台/聚合站：没有实际外链价值（与 soundbuttons v2 结论一致）
PLATFORM_RE = re.compile(r"(?i)(^|\.)(blogspot\.|wordpress\.|tumblr\.|livejournal\.|"
                         r"weebly\.|wixsite\.|github\.io|reddit\.|facebook\.|"
                         r"twitter\.|x\.com|instagram\.|youtube\.|pinterest\.|"
                         r"pearltrees\.|digg\.|medium\.com|substack\.com)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--center", required=True)
    ap.add_argument("--keywords", help="关键词 JSON（品类相关性过滤）")
    ap.add_argument("--min-via", type=int, default=2, help="至少几个竞品共同反链（默认2）")
    ap.add_argument("--max", type=int, default=0, help="最多输出多少条（0=不限）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    src = ROOT / "data" / ("competitor-links-%s.json" % args.center)
    data = json.loads(src.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    rel = load_relevance(args.keywords) if args.keywords else None

    out, skipped = [], {"platform": 0, "via": 0, "irrelevant": 0}
    for r in rows:
        url = r.get("url") or ""
        via = r.get("via_competitors") or []
        dom = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if dom.startswith("www."):
            dom = dom[4:]   # lstrip("www.") 是字符集剔除，会把 wordpress→ordpress
        if len(via) < args.min_via:
            skipped["via"] += 1
            continue
        if PLATFORM_RE.search(dom):
            skipped["platform"] += 1
            continue
        if rel and not rel({"domain": dom, "via_competitors": via}):
            skipped["irrelevant"] += 1
            continue
        out.append({
            "url": url,
            "category": "resource",
            "submit_type": "unknown",        # 未验证提交路径 → wave3，人工单条确认
            "title": "%s 资源页（%d 竞品反链）" % (dom, len(via)),
            "note": "WDC 竞品反链 | via=%d | %s | HTTP 待 scan 校验" % (
                len(via), ",".join(via[:6])),
            "tags": ["wdc-fast", "via>=%d" % args.min_via],
            "source": "prospector:competitor_backlink",
            "found_by": "prospector:competitor_backlink",
        })
        if args.max and len(out) >= args.max:
            break

    Path(args.out).write_text(json.dumps({"candidates": out}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print("source rows: %d" % len(rows))
    print("skipped:", skipped)
    print("exported: %d → %s" % (len(out), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
