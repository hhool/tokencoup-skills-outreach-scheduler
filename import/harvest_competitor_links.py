#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harvest competitor backlink prospects from the WDC 2012 hyperlink graph.

Reads the Web Data Commons PLD (pay-level-domain) graph — every edge means
"at least one page of host A linked to host B somewhere in the Common Crawl
corpus" — and emits every host that links TO one of the center domain's
competitors. A host that linked to a competitor in 2012 is niche-adjacent by
evidence, not by keyword. Every emitted row is re-validated live at analyze
time, so the graph's age only costs yield, not correctness.

Files (one-off download, cached in --workdir):
  https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/pld-index.gz  (~297 MB)
  https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/pld-arc.gz    (~2.8 GB)

Format (verified against the live files):
  index: one node per line, "<name>\\t<index>"
  arc:   one edge per line, "<src_index>\\t<dst_index>"  (numeric node ids)

Usage:
  python3 tools/harvest_competitor_links.py --center soundbuttons.cc
  python3 tools/harvest_competitor_links.py --center soundbuttons.cc \\
      --competitors a.com,b.com --min-competitors 2 --limit 20000
  python3 tools/harvest_competitor_links.py --selftest

Output (default data/competitor-links-<center>.json; the analyze loader reads
data/ before seeds/): {"meta": {...}, "rows": [{"url": "http://host/",
"via_competitors": ["comp", ...], "inbound": n}]} — the shape
load_competitor_backlinks() expects.
"""
import argparse
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import store as st  # noqa: E402

BASE_URL = "https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/"
FILES = {"pld": ("pld-index.gz", "pld-arc.gz"),
         "sd": ("sd-index.gz", "sd-arc.gz")}
PROGRESS_EVERY = 20_000_000


def log(msg):
    sys.stderr.write("[harvest] %s\n" % msg)
    sys.stderr.flush()


def _stream_lines(path):
    """Yield lines of a (possibly partial) .gz file; tolerates a truncated tail."""
    dec = gzip.GzipFile(fileobj=open(path, "rb"), mode="rb")
    buf = b""
    while True:
        chunk = dec.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            yield ln.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def _pld_of(domain):
    """Reduce a host to its pay-level domain (what this graph is keyed on).

    Deliberately shallow (last two labels, common second-level TLDs kept
    whole) — it only needs to be as smart as the graph, which stores bare
    PLD names like 'myinstants.com'.
    """
    host = st._bare_domain(domain)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) >= 3:
        second_level = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne"}
        if labels[-2] in second_level and len(labels[-1]) == 2:
            return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def ensure_files(graph, workdir):
    """Verify graph files exist locally; returns (index_path, arc_path).

    Downloading is NOT done here: the 3.1GB fetch belongs to an external
    resumable process (see data/wdc/dl.sh). This function refuses to start
    with partial files rather than silently harvesting half a graph.
    """
    idx_name, arc_name = FILES[graph]
    paths = []
    for name in (idx_name, arc_name):
        p = os.path.join(workdir, name)
        if not os.path.isfile(p) or os.path.getsize(p) < 1_000_000:
            raise SystemExit(
                "missing %s — download it first, e.g.:\n"
                "  curl -C - -o %s %s/%s" % (p, p, BASE_URL, name))
        paths.append(p)
    return paths


def harvest(index_path, arc_path, competitors, min_competitors=1, limit=0,
            exclude=(), progress=PROGRESS_EVERY):
    """Three streaming passes; memory holds only ids we care about.

    Returns (rows, stats). rows: [{"url", "via_competitors", "inbound"}]
    sorted by inbound desc then host asc.
    """
    comps = {c for c in (_pld_of(x) for x in competitors) if c}
    if not comps:
        raise ValueError("no usable competitor domains after normalization")
    exclude = {c for c in (_pld_of(x) for x in exclude) if c} - comps

    # pass 1: node ids of the competitors themselves
    comp_ids = {}
    t0 = time.time()
    for n, line in enumerate(_stream_lines(index_path), 1):
        name, _, idx = line.rstrip("\r").rpartition("\t")
        if name in comps:
            try:
                comp_ids[int(idx)] = name
            except ValueError:
                continue
        if progress and n % progress == 0:
            log("index pass 1: %dM lines (%.0fs)" % (n // 1_000_000, time.time() - t0))
    missing = comps - set(comp_ids.values())
    if not comp_ids:
        raise ValueError("none of the competitors found in the index: %s" % sorted(comps))
    if missing:
        log("not in graph (ok, post-2012 domains): %s" % sorted(missing))

    # pass 2: hosts that link INTO a competitor
    src_counts = {}          # src_idx -> {comp_name: count}
    t0 = time.time()
    for n, line in enumerate(_stream_lines(arc_path), 1):
        s, _, t = line.rstrip("\r").partition("\t")
        name = comp_ids.get(int(t) if t.isdigit() else -1)
        if name is None:
            continue
        if not s.isdigit():
            continue
        bucket = src_counts.setdefault(int(s), {})
        bucket[name] = bucket.get(name, 0) + 1
        if progress and n % progress == 0:
            log("arc pass 2: %dM lines (%.0fs)" % (n // 1_000_000, time.time() - t0))

    # pass 3: resolve src ids back to names
    needed = set(src_counts)
    names = {}
    t0 = time.time()
    for n, line in enumerate(_stream_lines(index_path), 1):
        name, _, idx = line.rstrip("\r").rpartition("\t")
        if idx.isdigit() and int(idx) in needed:
            names[int(idx)] = name
        if progress and n % progress == 0:
            log("index pass 3: %dM lines (%.0fs)" % (n // 1_000_000, time.time() - t0))

    rows = []
    for src, bucket in src_counts.items():
        host = names.get(src)
        if not host or host in exclude or host in comps:
            continue
        if len(bucket) < min_competitors:
            continue
        rows.append({"url": "http://%s/" % host,
                     "via_competitors": sorted(bucket, key=lambda c: -bucket[c]),
                     "inbound": sum(bucket.values())})
    rows.sort(key=lambda r: (-r["inbound"], r["url"]))
    if limit:
        rows = rows[:limit]
    stats = {"competitors": sorted(comps), "missing": sorted(missing),
             "src_hosts": len(src_counts), "emitted": len(rows)}
    return rows, stats


def _selftest():
    """Tiny fixture graph: writes a fake index/arc pair and runs harvest."""
    import tempfile
    d = tempfile.mkdtemp(prefix="harvest-selftest-")
    idx = os.path.join(d, "pld-index.gz")
    arc = os.path.join(d, "pld-arc.gz")
    nodes = ["myinstants.com", "voicemod.net", "fan-site-a.com",
             "fan-site-b.com", "unrelated.org", "spam-link-farm.com"]
    with gzip.open(idx, "wt") as f:
        for i, n in enumerate(nodes):
            f.write("%s\t%d\n" % (n, i))
    # edges: a and b and farm -> myinstants; b -> voicemod; unrelated -> nothing
    edges = [(2, 0), (3, 0), (5, 0), (3, 1), (4, 4)]
    with gzip.open(arc, "wt") as f:
        for s, t in edges:
            f.write("%d\t%d\n" % (s, t))
    rows, stats = harvest(idx, arc, ["myinstants.com", "voicemod.net"],
                          min_competitors=1)
    urls = {r["url"] for r in rows}
    assert "http://fan-site-a.com/" in urls, rows
    assert "http://fan-site-b.com/" in urls, rows
    assert "http://unrelated.org/" not in urls, rows
    b = next(r for r in rows if "fan-site-b.com" in r["url"])
    assert set(b["via_competitors"]) == {"myinstants.com", "voicemod.net"}, b
    assert b["inbound"] == 2, b
    rows2, _ = harvest(idx, arc, ["myinstants.com"], min_competitors=2)
    assert rows2 == [], rows2          # no host links to 2 comps when only 1 target
    rows3, _ = harvest(idx, arc, ["myinstants.com"], exclude=["spam-link-farm.com"])
    assert "http://spam-link-farm.com/" not in {r["url"] for r in rows3}, rows3
    print("harvest selftest: all assertions passed (%d rows)" % len(rows))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--center", help="center domain (profile owner)")
    ap.add_argument("--competitors", help="comma-separated; default = profile competitors")
    ap.add_argument("--min-competitors", type=int, default=1,
                    help="keep hosts linking to >= N competitors (2 = strongest)")
    ap.add_argument("--limit", type=int, default=0, help="cap emitted rows")
    ap.add_argument("--graph", choices=sorted(FILES), default="pld")
    ap.add_argument("--workdir", default=os.path.join(ROOT, "data", "wdc"))
    ap.add_argument("--out", help="output path; default data/competitor-links-<center>.json")
    ap.add_argument("--exclude", default="", help="comma-separated hosts to drop")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    center = args.center
    if not center:
        raise SystemExit("--center is required (or use --selftest)")
    center = center.strip().lower()

    if args.competitors:
        comps = [c.strip() for c in args.competitors.split(",") if c.strip()]
    else:
        prof = st.load_profile(center)
        comps = [c for c in (prof.get("competitors") or []) if str(c).strip()]
    if not comps:
        raise SystemExit("no competitors: pass --competitors or fill the profile")

    idx_path, arc_path = ensure_files(args.graph, args.workdir)
    rows, stats = harvest(idx_path, arc_path, comps,
                          min_competitors=args.min_competitors,
                          limit=args.limit,
                          exclude=[x for x in args.exclude.split(",") if x.strip()])

    out = args.out or os.path.join(ROOT, "data", "competitor-links-%s.json" % center)
    payload = {
        "meta": {
            "center": center,
            "competitors": comps,
            "graph": args.graph,
            "source": BASE_URL,
            "min_competitors": args.min_competitors,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": "WDC PLD graph hosts linking to competitors; re-validated live at analyze time",
            "stats": stats,
        },
        "rows": rows,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    log("wrote %d rows -> %s" % (len(rows), out))
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
