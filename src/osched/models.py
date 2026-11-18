"""Unified import-layer data model (see DESIGN.md §4b).

Every SourceAdapter produces SourceRecord objects; downstream stages
(probe / classify / DR) only ever see this model. English primary.
中文：所有导入源适配器的统一输出模型，下游只认它。
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class SourceRef:
    """Provenance: traceable to source kind, file and row."""

    kind: str          # "wdc" / "similar:<name>"
    file: str          # source file path (as given, not resolved)
    row_no: int        # 0-based row index within the file


@dataclass
class Evidence:
    """Why this record is a plausible outreach candidate (may be empty).

    For WDC: hosts that linked to competitors in the crawl corpus.
    """

    via_competitors: list[str] = field(default_factory=list)
    inbound: int = 0
    corpus: str = ""   # e.g. "wdc-2012-08"


@dataclass
class SourceRecord:
    """Unified output of all import-source adapters (DESIGN.md §4b)."""

    url: str
    source: SourceRef
    domain: str = ""                       # derived if empty (pay-level domain)
    evidence: Evidence = field(default_factory=Evidence)
    hints: dict = field(default_factory=dict)   # category/language hints; inputs only
    raw: dict = field(default_factory=dict)     # original row, never lost

    def derive_domain(self) -> str:
        """Derive domain from url (host lowercased). Idempotent."""
        if not self.domain and self.url:
            host = urlparse(self.url).netloc.lower()
            # strip port and leading www. for pool-level dedupe keys
            host = host.split(":", 1)[0]
            if host.startswith("www."):
                host = host[4:]
            self.domain = host
        return self.domain

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)


def validate(rec: SourceRecord) -> list[str]:
    """Record-level validation. Returns a list of issues (empty = valid)."""
    issues: list[str] = []
    if not rec.url or not rec.url.strip():
        issues.append("empty url")
    else:
        parsed = urlparse(rec.url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            issues.append(f"invalid url: {rec.url!r}")
    if not rec.source.kind:
        issues.append("missing source.kind")
    return issues
