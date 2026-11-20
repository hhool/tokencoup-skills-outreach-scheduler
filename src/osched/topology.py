"""Read-only view of the versioned WDC discovery topology manifest.

The manifest is produced by ``import/prepare_discovery_topology.py`` from the
graph workset and stored under the gitignored data root. This module only
validates and renders it — no network, no writes, no claims beyond recorded
fields. 中文：只读校验并展示 WDC 发现层拓扑清单，不联网、不写盘、不编造。
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

SCHEMA_VERSION = "1.0"
_STAGE_FIELDS = ("id", "label", "status", "outputs")
_EDGE_FIELDS = ("from", "to")


class TopologyError(ValueError):
    """Raised for a structurally invalid topology manifest."""


def load_topology(path: str | pathlib.Path) -> dict[str, Any]:
    """Load and validate a topology manifest; fail loudly on bad structure."""
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"topology manifest not found: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TopologyError(f"{p.name}: invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TopologyError(f"{p.name}: root must be an object")
    for key in ("schema_version", "snapshot_version", "stages", "edges"):
        if key not in doc:
            raise TopologyError(f"{p.name}: missing {key!r}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise TopologyError(
            f"{p.name}: schema {doc['schema_version']!r}, expected {SCHEMA_VERSION!r}"
        )
    if not isinstance(doc["stages"], list) or not doc["stages"]:
        raise TopologyError(f"{p.name}: 'stages' must be a non-empty list")
    ids: set[str] = set()
    for st in doc["stages"]:
        if not isinstance(st, dict):
            raise TopologyError(f"{p.name}: stage must be an object")
        for key in _STAGE_FIELDS:
            if key not in st:
                raise TopologyError(f"{p.name}: stage missing {key!r}")
        if st["id"] in ids:
            raise TopologyError(f"{p.name}: duplicate stage id {st['id']!r}")
        ids.add(str(st["id"]))
        if not isinstance(st["outputs"], list):
            raise TopologyError(f"{p.name}: stage {st['id']} outputs must be a list")
    if not isinstance(doc["edges"], list):
        raise TopologyError(f"{p.name}: 'edges' must be a list")
    for edge in doc["edges"]:
        if not isinstance(edge, dict) or any(k not in edge for k in _EDGE_FIELDS):
            raise TopologyError(f"{p.name}: edge must have 'from' and 'to'")
        for key in _EDGE_FIELDS:
            if str(edge[key]) not in ids:
                raise TopologyError(
                    f"{p.name}: edge references unknown stage {edge[key]!r}"
                )
    return doc


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """Compact counters for health checks and CLI output."""
    stages = doc["stages"]
    artifacts = [a for st in stages for a in st["outputs"] if isinstance(a, dict)]
    hashed = [a for a in artifacts if a.get("sha256")]
    statuses: dict[str, int] = {}
    for st in stages:
        state = str(st.get("status") or "unknown")
        statuses[state] = statuses.get(state, 0) + 1
    deliverable = next(
        (a.get("records") for a in artifacts
         if a.get("name") == "colink_cross_validated.json"),
        None,
    )
    review = next(
        (st.get("status") for st in stages if st.get("id") == "review-loop"),
        "not-recorded",
    )
    return {
        "snapshot_version": doc["snapshot_version"],
        "stages": len(stages),
        "edges": len(doc["edges"]),
        "artifacts": len(artifacts),
        "hashed_artifacts": len(hashed),
        "total_bytes": sum(int(a.get("bytes") or 0) for a in artifacts),
        "deliverable_rows": deliverable,
        "review_status": review,
        "status_counts": statuses,
    }


def render_text(doc: dict[str, Any]) -> str:
    """Human-readable stage table + dependency edges for the CLI."""
    summary = summarize(doc)
    lines = [
        f"WDC discovery topology · {summary['snapshot_version']} · "
        f"{summary['stages']} stages / {summary['edges']} edges",
        "",
    ]
    for st in doc["stages"]:
        def _label(a: dict) -> str:
            name = a.get("name", "?")
            if not a.get("present", True):
                return f"{name} (absent)"
            return f"{name} ({a.get('records', a.get('bytes', '?'))})"

        outputs = ", ".join(
            _label(a) for a in st["outputs"] if isinstance(a, dict)
        ) or "-"
        lines.append(f"{st['id']:>11}  {st['label']}  [{st['status']}]")
        lines.append(f"             {outputs}")
    lines.append("")
    for edge in doc["edges"]:
        lines.append(f"{edge['from']} -> {edge['to']}")
    lines.append("")
    lines.append(
        "boundary: discovery layer only — no submittability or link-survival "
        "signal; human quality gate unchanged"
    )
    return "\n".join(lines)
