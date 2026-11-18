#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harvest "who links to my competitors" from the Web Data Commons host graph.

Why offline: the graph is a pre-computed inbound-edge table published by WDC
(derived from Common Crawl). It answers exactly one question — which hosts link
to host X — but the smallest useful release is several GB compressed, while the
service image is ~37 MB. So this runs on an operator machine and writes a
compact, reviewable JSON file that ships with the image (seeds/).

Input format (WDC hyperlink graph, tab separated, gzip or plain):

  *-domain-ranks.txt.gz / *-host-ranks.txt.gz   id <TAB> name <TAB> ...
  *-domain-edges.txt.gz / *-host-edges.txt.gz   from_id <TAB> to_id <TAB> count

Get a release at https://webdatamining.org/ (Hyperlink Graph). Download the
"domain ranks" + "domain edges" pair for the same crawl window.

Usage
-----
  python3 tools/competitor_backlinks.py \
      --center soundbuttons.click \
      --competitors myinstants.com,voicemod.net \
      --ranks  /data/wdc/cc-main-2025-may-aug-domain-ranks.txt.gz \
      --edges  /data/wdc/cc-main-2025-may-aug-domain-edges.txt.gz \
      --out    seeds/competitor-links-soundbuttons.click.json

  # already-extracted edge list (source,target[,count]) works too:
  python3 tools/competitor_backlinks.py --center ... --competitors ... \
      --from-csv /data/edges.csv --out seeds/competitor-links-....json

The output is an evidence file, not a verdict: rows are admitted by "this host
links to a site that already competes in our niche". Page type is guessed from
the hostname and flagged type_guess=true so an operator can reclassify it.

Python standard library only.
"""
import argparse
import csv as csvmod
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Never a prospect, no matter how many competitors it links to. Kept in sync
# with analyze.BLOCKED_DOMAINS + analyze.NOISE_DOMAINS (duplicated on purpose:
# this tool must run standalone on an operator box, without importing the app).
BLOCKED = {
    "pornhub.com", "youporn.com", "xvideos.com", "xhamster.com", "xnxx.com",
    "redtube.com", "tube8.com", "spankbang.com", "eporner.com", "fapello.com",
    "bestsecret.com", "klipads.co", "propellerads.com", "adcash.com",
    "popads.net", "hilltopads.net", "lomb.co", "blackhatworth.com",
}
NOISE = {
    "google.com", "bing.com", "microsoft.com", "msn.com", "yahoo.com",
    "wikipedia.org", "amazon.com", "facebook.com", "youtube.com",
    "pinterest.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "reddit.com", "quora.com", "tiktok.com", "baidu.com", "zhihu.com",
    "bilibili.com", "apple.com", "wikihow.com", "dictionary.com",
    "merriam-webster.com", "collinsdictionary.com", "bestbuy.com",
    "walmart.com", "ebay.com", "etsy.com", "yelp.com", "tripadvisor.com",
    "cloudflare.com", "wordpress.org", "blogspot.com", "wixsite.com",
    "googleusercontent.com", "archive.org", "amazonaws.com",
}

HOST_OK = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def norm_host(value):
    """Registrable-ish host: lowercase, no scheme/path/www, no trailing dot."""
    v = (value or "").strip().lower()
    if "//" in v:
        v = urllib.parse.urlsplit(v).hostname or ""
    v = v.split("/")[0].split("?")[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v


def open_text(path):
    """Open a .gz or plain text file for line streaming (UTF-8, replacement)."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="\n")
    return open(path, "rt", encoding="utf-8", errors="replace", newline="\n")


def _download(url, cache_dir):
    """Fetch a (large) graph file once and reuse it. Resumable by prefix match."""
    os.makedirs(cache_dir, exist_ok=True)
    name = os.path.basename(urllib.parse.urlsplit(url).path) or "graph.bin"
    dest = os.path.join(cache_dir, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        log("  cached: %s (%s bytes)" % (dest, os.path.getsize(dest)))
        return dest
    tmp = dest + ".part"
    log("  downloading %s -> %s" % (url, dest))
    req = urllib.request.Request(url, headers={"User-Agent": "backlink-prospector/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else 0
        done = 0
        next_tick = 64 << 20
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total and done >= next_tick:
                next_tick += 64 << 20
                log("    %5.1f%% of %s" % (100.0 * done / total, human(total)))
    os.replace(tmp, dest)
    return dest


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def resolve_input(value, cache_dir):
    """Accept a local path or an http(s) URL; download URLs into the cache."""
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return _download(value, cache_dir)
    if not os.path.isfile(value):
        raise SystemExit("file not found: %s" % value)
    return value


def matches_competitor(host, competitors):
    """Competitor host itself or any subdomain of it (blog.x.com -> x.com)."""
    if host in competitors:
        return host
    for c in competitors:
        if host.endswith("." + c):
            return c
    return None


def guess_type(host):
    """Cheap hostname heuristic; the graph carries no page semantics."""
    h = host
    if any(k in h for k in ("forum", "board", "community", "discuss", "stackexchange")):
        return "community"
    if any(k in h for k in ("wiki", "docs", "manual", "reference", "awesome")):
        return "resource"
    if any(k in h for k in ("news", "magazine", "review", "blog", "daily", "times", "post")):
        return "industry_media"
    return "resource"


# --------------------------------------------------------------------------- #
# graph passes
# --------------------------------------------------------------------------- #
def pass_ranks_find_competitors(path, competitors, want_ids=None):
    """Stream ranks once.

    With want_ids=None: return {id: competitor_host} for competitor hosts.
    With want_ids=set: return {id: host} restricted to those ids (cheap reverse
    lookup for the source hosts found in the edges pass).
    """
    out = {}
    scanned = 0
    with open_text(path) as fh:
        for line in fh:
            scanned += 1
            if scanned % 20000000 == 0:
                log("    ranks: %s lines scanned" % human_rowcount(scanned))
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            rid, name = parts[0], parts[1]
            if want_ids is not None:
                if rid in want_ids:
                    out[rid] = norm_host(name)
            else:
                host = norm_host(name)
                if host and matches_competitor(host, competitors):
                    out[rid] = host
    log("  ranks pass: %d lines, %d matched ids" % (scanned, len(out)))
    return out


def human_rowcount(n):
    return "{:,}".format(n)


def pass_edges(path, comp_ids, competitors, max_hits=5_000_000):
    """Stream edges; keep only edges pointing AT a competitor id.

    Returns {source_id: {competitor_host: link_count}} and the scan counters.
    """
    hits = {}
    scanned = 0
    matched = 0
    with open_text(path) as fh:
        for line in fh:
            scanned += 1
            if scanned % 50000000 == 0:
                log("    edges: %s rows, %d matched (memory %d sources)"
                    % (human_rowcount(scanned), matched, len(hits)))
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            src, dst = parts[0], parts[1]
            comp_host = comp_ids.get(dst)
            if not comp_host:
                continue
            if comp_ids.get(src):          # competitor -> competitor: ignore
                continue
            try:
                cnt = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 1
            except ValueError:
                cnt = 1
            bucket = hits.setdefault(src, {})
            bucket[comp_host] = bucket.get(comp_host, 0) + max(cnt, 1)
            matched += 1
            if len(hits) >= max_hits:
                log("  edges pass: source cap reached (%d), stopping early" % max_hits)
                break
    log("  edges pass: %d rows scanned, %d edges into competitors, %d distinct sources"
        % (scanned, matched, len(hits)))
    return hits, scanned, matched


def rows_from_hits(hits, id2host, center, competitors, graph_label, min_links, max_rows):
    """Turn {source_id: {comp: count}} into analysable prospect rows."""
    rows = []
    dropped = {"competitor": 0, "self": 0, "blocked": 0, "noise": 0, "bad_host": 0,
               "low_links": 0}
    for src_id, per_comp in hits.items():
        host = norm_host(id2host.get(src_id) or "")
        if not host or not HOST_OK.match(host):
            dropped["bad_host"] += 1
            continue
        if matches_competitor(host, competitors):
            dropped["competitor"] += 1
            continue
        if center and (host == center or host.endswith("." + center)):
            dropped["self"] += 1
            continue
        if host in BLOCKED or any(host.endswith("." + b) for b in BLOCKED):
            dropped["blocked"] += 1
            continue
        if host in NOISE or any(host.endswith("." + n) for n in NOISE):
            dropped["noise"] += 1
            continue
        total = sum(per_comp.values())
        if total < min_links:
            dropped["low_links"] += 1
            continue
        vias = sorted(per_comp.keys())
        t = guess_type(host)
        detail = ", ".join("%s×%d" % (c, n) for c, n in sorted(per_comp.items()))
        rows.append({
            "url": "https://" + host,
            "type": t,
            "type_guess": True,
            "tags": [],
            "authorityTier": None,
            "domains": [center] if center else [],
            "source": "competitor_backlink",
            "via_competitor": vias[0],
            "via_competitors": vias,
            "evidence": {"graph": graph_label, "edges": total,
                         "per_competitor": per_comp},
            "note": "竞品 %s 的外链站（%s 条链接）；类型按域名推测，待人工确认" % (vias[0], detail),
            "note_en": "Links out to %s (%s); type guessed from the hostname, needs review"
                       % (", ".join(vias), detail),
        })
    rows.sort(key=lambda r: (-r["evidence"]["edges"], -len(r["via_competitors"]), r["url"]))
    if max_rows and len(rows) > max_rows:
        dropped["over_max_rows"] = len(rows) - max_rows
        rows = rows[:max_rows]
    return rows, dropped


def rows_from_csv(path, center, competitors, graph_label, min_links, max_rows):
    """Fallback input: an already-extracted edge list, `source,target[,count]`."""
    hits = {}
    with open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        has_header = "source" in sample.lower() and "target" in sample.lower()
        reader = csvmod.reader(fh)
        if has_header:
            next(reader, None)
        for rec in reader:
            if len(rec) < 2:
                continue
            src = norm_host(rec[0])
            dst = norm_host(rec[1])
            comp = matches_competitor(dst, competitors)
            if not src or not comp:
                continue
            try:
                cnt = int(rec[2]) if len(rec) > 2 and rec[2].strip() else 1
            except ValueError:
                cnt = 1
            bucket = hits.setdefault(src, {})
            bucket[comp] = bucket.get(comp, 0) + max(cnt, 1)
    id2host = {k: k for k in hits}      # hostname-keyed already
    return rows_from_hits(hits, id2host, center, competitors, graph_label,
                          min_links, max_rows)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Harvest competitor-backlink prospects from the WDC host graph.")
    ap.add_argument("--center", required=True, help="center domain, e.g. soundbuttons.click")
    ap.add_argument("--competitors", required=True,
                    help="comma separated competitor domains, e.g. myinstants.com,voicemod.net")
    ap.add_argument("--ranks", help="*-ranks.txt[.gz] (id -> name)")
    ap.add_argument("--edges", help="*-edges.txt[.gz] (from -> to -> count)")
    ap.add_argument("--from-csv", dest="from_csv",
                    help="instead of a graph: edges as source,target[,count]")
    ap.add_argument("--out", help="output JSON (default seeds/competitor-links-<center>.json)")
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, "data", "wdc-cache"),
                    help="where http(s) graph URLs are downloaded (default data/wdc-cache)")
    ap.add_argument("--min-links", type=int, default=1,
                    help="minimum total links into competitors for a source host (default 1)")
    ap.add_argument("--max-rows", type=int, default=2000, help="cap on emitted rows (default 2000)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    center = norm_host(args.center)
    competitors = [norm_host(x) for x in re.split(r"[,\s]+", args.competitors or "") if x.strip()]
    competitors = sorted({c for c in competitors if c})
    if not center or not HOST_OK.match(center):
        raise SystemExit("invalid --center: %s" % args.center)
    if not competitors:
        raise SystemExit("--competitors is required")

    log("center      : %s" % center)
    log("competitors : %s" % ", ".join(competitors))
    for c in competitors:
        if c == center:
            raise SystemExit("a competitor cannot be the center domain")

    started = dt.datetime.now(dt.timezone.utc)
    if args.from_csv:
        label = "csv:%s" % os.path.basename(args.from_csv)
        log("input       : %s" % label)
        rows, dropped = rows_from_csv(args.from_csv, center, competitors, label,
                                      args.min_links, args.max_rows)
        counters = {}
    else:
        if not (args.ranks and args.edges):
            raise SystemExit(
                "--ranks and --edges are required (or use --from-csv).\n"
                "Download a WDC hyperlink-graph release (domain ranks + domain edges,\n"
                "same crawl window) from https://webdatamining.org/ and pass the paths\n"
                "or the https URLs.")
        ranks = resolve_input(args.ranks, args.cache_dir)
        edges = resolve_input(args.edges, args.cache_dir)
        label = os.path.basename(ranks)
        log("ranks       : %s" % ranks)
        log("edges       : %s" % edges)
        comp_ids = pass_ranks_find_competitors(ranks, competitors)
        if not comp_ids:
            raise SystemExit(
                "no id matched the competitor list in the ranks file. Check that the "
                "spelling matches the graph (it is registrable-domain level) and that "
                "ranks/edges come from the same release.")
        hits, scanned, matched = pass_edges(edges, comp_ids, competitors)
        if not hits:
            log("no inbound edges found — the competitors may be absent from this crawl")
        log("  resolving %d source hosts" % len(hits))
        id2host = pass_ranks_find_competitors(ranks, competitors, want_ids=set(hits.keys()))
        rows, dropped = rows_from_hits(hits, id2host, center, competitors, label,
                                       args.min_links, args.max_rows)
        counters = {"edge_rows_scanned": scanned, "matched_edges": matched,
                    "distinct_sources": len(hits)}

    out_path = args.out or os.path.join(ROOT, "seeds", "competitor-links-%s.json" % center)
    meta = {
        "schema": "competitor-links/1",
        "center_domain": center,
        "competitors": competitors,
        "graph": label,
        "graph_type": "csv" if args.from_csv else ("host" if "host-" in label else "domain"),
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "tools/competitor_backlinks.py",
        "min_links": args.min_links,
        "max_rows": args.max_rows,
        "rows": len(rows),
        "dropped": dropped,
        "counters": counters,
        "notes": [
            "Rows are admitted by evidence: the host links to a site that competes in this niche.",
            "Page type is guessed from the hostname (type_guess=true) and needs operator review.",
            "Only competitor hosts themselves are excluded from analysis; these sources are ordinary prospects.",
        ],
    }
    payload = {"meta": meta, "rows": rows}

    log("")
    log("result      : %d rows in %.1fs" % (len(rows), (dt.datetime.now(dt.timezone.utc) - started).total_seconds()))
    if dropped:
        log("dropped     : %s" % json.dumps(dropped, ensure_ascii=False))
    if counters:
        log("counters    : %s" % json.dumps(counters, ensure_ascii=False))
    for r in rows[:10]:
        log("  %-40s edges=%-5d via=%s" % (r["url"], r["evidence"]["edges"],
                                           ",".join(r["via_competitors"])))
    if len(rows) > 10:
        log("  ... %d more" % (len(rows) - 10))

    if args.dry_run:
        log("dry-run     : nothing written")
        return 0
    if not rows:
        log("nothing to write (0 rows)")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, out_path)
    log("written     : %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
