"""WDC discovery-graph snapshot adapter (v0.3+).

Input: the versioned discovery snapshot produced by the WDC co-link/outlink
pipeline (``colink_cross_validated.json`` and its versioned copies)::

    {"sources": ["colink_final_filtered.json", "outlink_results.json"],
     "formula": "...", "candidates": 12133,
     "all_scored": [
       {"domain": "example.com", "industry": "ai-data",
        "inlink_support": 94, "dir_llr": 62.818, "dir_prob": 1.0,
        "outlink_l1": "ai-data", "outlink_similarity": 0.0508,
        "outlink_used": true, "dual_signal_agree": true,
        "composite": 1.0, "vertex_id": 71799825}
     ]}

This is a **discovery-layer** source. Rows are industry candidates inferred
from the WDC graph; they are neither competitor backlinks nor verified
submittable platforms. Mapping discipline (no fabricated signals):

    domain          -> url = https://<domain>/ and SourceRecord.domain
    inlink_support  -> Evidence.inbound (same-industry seed co-link support,
                       NOT "linked to our competitors")
    via_competitors -> [] always left empty
    corpus          -> snapshot version (YAML ``snapshot`` or file name)
    every other field -> hints["discovery"] + raw unchanged

Downstream probe/classify still decide whether a row is submittable. 中文：
发现层候选不是竞品反链，也不是已验证可投递平台；保留全部图谱字段，
不伪造 via_competitors，可投递性仍交给探测阶段。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .base import SourceAdapter, SourceFile, Issue
from ..models import Evidence, SourceRecord, SourceRef

# Strict enough to reject URLs/paths, permissive enough for punycode and IDN
# labels already normalised to ASCII by the upstream graph.
_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_VERSION_RE = re.compile(r"(v\d+(?:\.\d+)*)", re.I)

# L1 industry ids used by the discovery graph (taxonomy.md v0.3, 9 delivered).
# Unknown ids are kept as hints with a warning — never silently relabelled.
KNOWN_INDUSTRIES = {
    "saas-software", "ecommerce", "professional-services", "manufacturing",
    "ai-data", "developer-tools", "design-creative", "startup-business",
    "marketing",
}

DISCOVERY_FIELDS = (
    "dir_llr", "dir_prob", "outlink_l1", "outlink_similarity",
    "outlink_shared_targets", "outlink_degree", "dual_signal_agree",
    "outlink_used", "composite", "vertex_id",
)


def clean_domain(value) -> str:
    """Normalise a graph domain to a bare hostname, or '' when unusable."""
    raw = str(value or "").strip().lower().rstrip(".")
    if "://" in raw:
        raw = (urlparse(raw).netloc or "").lower()
    raw = raw.split("/", 1)[0].split(":", 1)[0].split("?", 1)[0]
    return raw if _HOST_RE.match(raw) else ""


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class WdcDiscoveryAdapter(SourceAdapter):
    """Adapter for `wdc_discovery_snapshot` sources (glob of snapshot JSON)."""

    kind = "wdc_discovery"

    def __init__(self, glob_pattern: str, snapshot: str = ""):
        self._glob = Path(glob_pattern).expanduser()
        self._snapshot = str(snapshot or "").strip()

    def discover(self) -> list[SourceFile]:
        parent = self._glob.parent
        if not parent.exists():
            return []
        files = []
        for path in sorted(parent.glob(self._glob.name)):
            if not path.is_file():
                continue
            st = path.stat()
            files.append(SourceFile(path=path, mtime=st.st_mtime, size=st.st_size))
        return files

    def _version(self, file: SourceFile) -> str:
        if self._snapshot:
            return self._snapshot
        match = _VERSION_RE.search(file.path.name)
        return f"wdc-{match.group(1).lower()}" if match else "wdc-unversioned"

    def parse(self, file: SourceFile):
        with open(file.path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError(f"{file.path.name}: snapshot root must be an object")
        rows = doc.get("all_scored")
        if not isinstance(rows, list):
            raise ValueError(f"{file.path.name}: missing 'all_scored' list")
        version = self._version(file)
        lineage = {
            "sources": list(doc.get("sources") or []),
            "formula": str(doc.get("formula") or ""),
            "candidates_meta": _as_int(doc.get("candidates")),
        }

        for row_no, row in enumerate(rows):
            if not isinstance(row, dict):
                row = {}
            domain = clean_domain(row.get("domain"))
            rec = SourceRecord(
                url=f"https://{domain}/" if domain else "",
                source=SourceRef(kind=self.kind, file=str(file.path), row_no=row_no),
                evidence=Evidence(
                    # Co-link support is an inbound-like graph signal. It is
                    # NOT competitor evidence; via_competitors stays empty.
                    via_competitors=[],
                    inbound=_as_int(row.get("inlink_support")),
                    corpus=version,
                ),
                hints={
                    "industry": row.get("industry") or "",
                    "snapshot": version,
                    "layer": "discovery",
                    "feedback_layer": "not-implemented",
                    "lineage": lineage,
                    "discovery": {k: row.get(k) for k in DISCOVERY_FIELDS},
                },
                raw=row,
            )
            rec.derive_domain()
            yield rec

    def validate(self, rec: SourceRecord) -> list[Issue]:
        issues = super().validate(rec)
        ind = (rec.hints.get("industry") or "").strip()
        if ind and ind not in KNOWN_INDUSTRIES:
            issues.append(Issue("warning", f"unknown discovery industry: {ind!r}"))
        if rec.raw.get("inlink_support") is None:
            issues.append(Issue("warning", "snapshot row has no inlink_support"))
        return issues
