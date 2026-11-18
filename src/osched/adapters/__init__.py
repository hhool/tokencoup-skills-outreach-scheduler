"""Adapter registry: YAML source blocks -> SourceAdapter instances.

Adding a new source = one adapter class + one registry entry + one YAML block.
中文：新增导入源只需一个适配器类 + 一段 YAML，下游零改动。
"""
from __future__ import annotations

from .base import SourceAdapter, SourceFile, Issue
from .wdc import WdcCompetitorLinksAdapter

__all__ = ["SourceAdapter", "SourceFile", "Issue", "build_adapters"]


def build_adapters(sources_cfg: dict) -> list[SourceAdapter]:
    """Build adapters from the `sources:` config block (excluding 'exclude')."""
    adapters: list[SourceAdapter] = []
    for name, block in sources_cfg.items():
        if name == "exclude" or not isinstance(block, dict):
            continue
        if name == "wdc_competitor_links":
            glob_pattern = block.get("glob")
            if not glob_pattern:
                raise ValueError("sources.wdc_competitor_links.glob is required")
            adapters.append(WdcCompetitorLinksAdapter(glob_pattern))
        else:
            # Unknown source kinds are rejected loudly — fail fast at startup.
            raise ValueError(f"unknown source kind: {name!r} (no adapter registered)")
    return adapters
