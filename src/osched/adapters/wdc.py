"""WDC competitor-links adapter.

Input: files produced by import/harvest_competitor_links.py (baseline code
baseline code from the internal harvest repository):

    {"meta": {...}, "rows": [
        {"url": "http://host/", "via_competitors": ["comp", ...], "inbound": n}
    ]}

Mapping to SourceRecord:
    rows[].url              -> url
    rows[].via_competitors  -> evidence.via_competitors
    rows[].inbound          -> evidence.inbound
    meta.corpus / filename  -> evidence.corpus
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import SourceAdapter, SourceFile
from ..models import Evidence, SourceRecord, SourceRef


class WdcCompetitorLinksAdapter(SourceAdapter):
    """Adapter for the `wdc_competitor_links` source (glob of JSON files)."""

    kind = "wdc"

    def __init__(self, glob_pattern: str):
        self._glob = Path(glob_pattern).expanduser()

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

    def parse(self, file: SourceFile):
        with open(file.path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        corpus = ""
        meta = doc.get("meta") or {}
        corpus = str(meta.get("corpus") or meta.get("graph") or "")
        for row_no, row in enumerate(doc.get("rows") or []):
            url = str(row.get("url") or "").strip()
            rec = SourceRecord(
                url=url,
                source=SourceRef(kind=self.kind, file=str(file.path), row_no=row_no),
                evidence=Evidence(
                    via_competitors=[str(c) for c in row.get("via_competitors") or []],
                    inbound=int(row.get("inbound") or 0),
                    corpus=corpus,
                ),
                hints={},
                raw=row,
            )
            rec.derive_domain()
            yield rec
