#!/usr/bin/env python3
"""Prepare a versioned WDC discovery snapshot for the import adapter.

The graph workset produces a discovery product (``colink_cross_validated.json``
and its versioned copies). This helper validates that product and writes a
copy under this project's gitignored ``data/sources/`` using a neutral,
versioned filename. Committed config never references the workset path.

Usage:
    python3 import/prepare_discovery_snapshot.py \
        --input <path-to-colink_cross_validated.json> --version 0.3

The adapter reads ``data/sources/wdc-discovery-<version>.json`` via the
``wdc_discovery_snapshot`` YAML block. 中文：把图谱产物校验后复制到本工程
data/sources/，配置只认这个中性文件名，不引用工作台路径。
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

_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*$")
_META_KEYS = (
    "sources", "formula", "outlink_backtest", "params", "candidates",
    "deliverable_after_filter", "junk_filtered", "with_outlink_profile",
    "outlink_used", "dual_signal_agreement", "by_industry_top100",
)


def validate_doc(doc) -> list[dict]:
    """Return the scored rows, or raise ValueError with a precise reason."""
    if not isinstance(doc, dict):
        raise ValueError("snapshot root must be a JSON object")
    rows = doc.get("all_scored")
    if not isinstance(rows, list) or not rows:
        raise ValueError("snapshot must contain a non-empty 'all_scored' list")
    bad = [i for i, r in enumerate(rows)
           if not isinstance(r, dict) or not str(r.get("domain") or "").strip()]
    if bad:
        raise ValueError(f"rows with missing domain: {bad[:5]} (total {len(bad)})")
    return rows


def prepare(input_path: str, version: str, repo_root: str = ".", out: str = "") -> dict:
    """Validate, stamp and atomically copy a discovery snapshot into data/."""
    version = version.strip()
    if not _VERSION_RE.match(version):
        raise ValueError(f"invalid --version {version!r} (expected e.g. 0.3)")
    version = version.lstrip("v")
    root = pathlib.Path(repo_root).resolve()
    target = pathlib.Path(out) if out else pathlib.Path("data/sources") / (
        f"wdc-discovery-v{version}.json"
    )
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    sources_dir = (root / "data" / "sources").resolve()
    if sources_dir not in target.parents:
        raise ValueError(f"output must stay under {sources_dir}")

    src = pathlib.Path(input_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"input snapshot not found: {src}")
    with open(src, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = validate_doc(doc)

    stamped = {k: doc[k] for k in _META_KEYS if k in doc}
    stamped["snapshot_version"] = f"wdc-v{version}"
    stamped["prepared_by"] = "import/prepare_discovery_snapshot.py"
    stamped["all_scored"] = rows

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wdc-discovery-", suffix=".json",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(stamped, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    industries: dict[str, int] = {}
    for row in rows:
        ind = str(row.get("industry") or "unknown")
        industries[ind] = industries.get(ind, 0) + 1
    return {
        "output": str(target), "rows": len(rows), "sha256": digest,
        "snapshot_version": f"wdc-v{version}",
        "industries": dict(sorted(industries.items())),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, help="WDC discovery product JSON")
    ap.add_argument("--version", default="0.3", help="snapshot version, e.g. 0.3")
    ap.add_argument("--repo-root", default=".", help="project root (default: cwd)")
    ap.add_argument("--out", default="", help="override output path under data/sources/")
    args = ap.parse_args(argv)
    try:
        info = prepare(args.input, args.version, args.repo_root, args.out)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
