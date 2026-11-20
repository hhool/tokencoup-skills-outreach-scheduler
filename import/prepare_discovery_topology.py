#!/usr/bin/env python3
"""Export the WDC discovery graph topology as a versioned manifest.

Reads the stage artifacts from the graph workset (paths supplied as CLI
arguments — never embedded in the repo), records sizes / hashes / counts,
and writes a gitignored manifest under this project's data root. Large
artifacts (>200 MB) are recorded size-only; no private path is written into
the output.

Usage:
    python3 import/prepare_discovery_topology.py \
        --workset <wdc-run dir> --graph-root <wdc-graph dir> --version 0.3
    python3 bin/osched.py topology
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from osched.topology import SCHEMA_VERSION  # noqa: E402

_HASH_LIMIT = 200 * 1024 * 1024
_CHUNK = 1024 * 1024

# Stage id -> (label, [(artifact basename, source root)])
# "ws" = workset (wdc-run), "gr" = graph root (wdc-graph)
_STAGE_LAYOUT = (
    ("raw", "raw WDC graph download", (
        ("cc-main-2026-aug-sep-oct-domain-ranks.txt.gz", "gr/raw"),
        ("cc-main-2026-aug-sep-oct-domain-edges.txt.gz", "gr/edges2026"),
    )),
    ("ranks", "domain rank database", (("wdc_domains.db", "gr"),)),
    ("seeds", "taxonomy + seed labels", (
        ("taxonomy.md", "ws"), ("seeds.json", "ws"),
        ("seed_vertex_ids.json", "ws"),
    )),
    ("votes", "direction-B co-link votes", (
        ("votes_b.bin", "ws"), ("votes_b.meta.json", "ws"),
    )),
    ("colink", "balanced industry candidates", (
        ("colink_final_balanced.json", "ws"),
    )),
    ("directory", "directory-likelihood filter", (
        ("directory_scores.json", "ws"),
        ("colink_final_filtered.json", "ws"),
    )),
    ("outlink", "out-link industry similarity", (
        ("outlink_results.json", "ws"),
    )),
    ("deliverable", "three-way cross validation", (
        ("colink_cross_validated.json", "ws"),
    )),
    ("review-loop", "human review and override", (
        ("review_shortlist_v2.csv", "ws"),
        ("review_overrides.json", "ws"),
    )),
)

_EDGES = (
    ("raw", "ranks"), ("raw", "votes"), ("raw", "outlink"),
    ("ranks", "seeds"), ("seeds", "votes"), ("seeds", "colink"),
    ("seeds", "outlink"), ("votes", "colink"),
    ("colink", "directory"), ("directory", "deliverable"),
    ("outlink", "deliverable"), ("deliverable", "review-loop"),
)


def _artifact(path: pathlib.Path, name: str) -> dict:
    """Record one artifact without ever storing its absolute path."""
    if not path.is_file():
        return {"name": name, "present": False, "bytes": 0,
                "sha256": None, "hash_policy": "absent"}
    size = path.stat().st_size
    if size <= _HASH_LIMIT:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
        return {"name": name, "present": True, "bytes": size,
                "sha256": digest.hexdigest(), "hash_policy": "sha256"}
    return {"name": name, "present": True, "bytes": size,
            "sha256": None, "hash_policy": "size-only (>200MB)"}


def _json(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _count_csv_rows(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    with open(path, "rb") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def build(workset: str, graph_root: str, version: str) -> dict:
    """Assemble a topology manifest from the given artifact directories."""
    roots = {
        "ws": pathlib.Path(workset).expanduser().resolve(),
        "gr": pathlib.Path(graph_root).expanduser().resolve(),
    }
    for key, root in roots.items():
        if not root.is_dir():
            raise FileNotFoundError(f"{key} directory not found: {root}")

    outlink = _json(roots["ws"] / "outlink_results.json")
    closed_loop = roots["ws"] / "data-graph-closed-loop.md"
    nodes = None
    if closed_loop.is_file():
        match = re.search(r"nodes=(\d+)", closed_loop.read_text(encoding="utf-8"))
        if match:
            nodes = int(match.group(1))

    stages = []
    for stage_id, label, items in _STAGE_LAYOUT:
        outputs = []
        for name, root_key in items:
            key, *subdirs = root_key.split("/")
            art = _artifact(roots[key].joinpath(*subdirs) / name, name)
            if name == "seeds.json":
                art["records"] = _json(roots["ws"] / name).get("total")
            elif name == "votes_b.meta.json":
                art["records"] = _json(roots["ws"] / name).get("targets")
            elif name in ("colink_final_balanced.json", "colink_final_filtered.json"):
                art["records"] = len(_json(roots["ws"] / name).get("results") or [])
            elif name == "directory_scores.json":
                art["records"] = len(_json(roots["ws"] / name).get("scores") or {})
            elif name == "outlink_results.json":
                art["records"] = outlink.get("candidates_scored")
                art["edges_scanned"] = outlink.get("edges_scanned")
            elif name == "colink_cross_validated.json":
                art["records"] = _json(roots["ws"] / name).get("candidates")
            elif name == "review_shortlist_v2.csv":
                art["records"] = _count_csv_rows(roots["ws"] / name)
            outputs.append(art)

        present = [a for a in outputs if a["present"]]
        status = "complete" if len(present) == len(outputs) else (
            "partial" if present else "missing"
        )
        if stage_id == "review-loop":
            overrides = next(a for a in outputs if a["name"] == "review_overrides.json")
            if not overrides["present"]:
                status = "human-gate-pending"
        stages.append({"id": stage_id, "label": label,
                       "status": status, "outputs": outputs})

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_version": f"wdc-v{version.lstrip('v')}",
        "graph_stats": {
            "nodes_documented": nodes,
            "arcs_scanned": outlink.get("edges_scanned"),
            "note": "nodes from documented graph stats; arcs from outlink_results.json",
        },
        "stages": stages,
        "edges": [{"from": a, "to": b} for a, b in _EDGES],
    }


def export(workset: str, graph_root: str, version: str,
           repo_root: str = ".", out: str = "") -> dict:
    """Build and atomically write the manifest under the project data root."""
    version = version.strip().lstrip("v")
    if not re.match(r"^\d+(?:\.\d+)*$", version):
        raise ValueError(f"invalid --version {version!r} (expected e.g. 0.3)")
    root = pathlib.Path(repo_root).resolve()
    target = pathlib.Path(out) if out else pathlib.Path(
        "data/sources") / f"wdc-topology-v{version}.json"
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    sources_dir = (root / "data" / "sources").resolve()
    if sources_dir not in target.parents:
        raise ValueError(f"output must stay under {sources_dir}")
    doc = build(workset, graph_root, version)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wdc-topology-", suffix=".json",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"output": str(target), "snapshot_version": doc["snapshot_version"],
            "stages": len(doc["stages"]), "edges": len(doc["edges"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workset", required=True, help="WDC workset directory (wdc-run)")
    ap.add_argument("--graph-root", required=True, help="WDC graph data directory")
    ap.add_argument("--version", default="0.3")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    try:
        info = export(args.workset, args.graph_root, args.version,
                      args.repo_root, args.out)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
